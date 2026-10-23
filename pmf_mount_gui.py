"""Optional mount controls, loaded only when the viewer's Mount Image button is used."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import webbrowser
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

from pmf_mount import control_mount, list_mounts, start_mount


class MountWindow(tk.Toplevel):
    def __init__(self, parent: tk.Tk | tk.Toplevel, image: Path, revision: int | None):
        super().__init__(parent)
        self.image = image
        self.revision = revision
        self.working = False
        self.title('Mount PMF image')
        self.geometry('820x450')
        self.transient(parent)
        box = ttk.Frame(self, padding=16)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text=image.name, font=('', 12, 'bold')).pack(anchor='w')
        text = ('Latest revision' if revision is None else f'Revision {revision} (read-only)')
        ttk.Label(box, text=text + ' • Windows drive-letter access').pack(anchor='w', pady=(4, 12))
        row = ttk.Frame(box)
        row.pack(fill='x')
        ttk.Label(row, text='Drive').pack(side='left')
        available = [f'{letter}:' for letter in 'PQRSTUVWXYZDEFGHIJKLMNO'
                     if not Path(f'{letter}:\\').exists()]
        self.drive = tk.StringVar(value=available[0] if available else '')
        self.letters = ttk.Combobox(row, textvariable=self.drive, values=available, width=5, state='readonly')
        self.letters.pack(side='left', padx=8)
        self.read_write = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(row, text='Read/write — save changes into this PMF', variable=self.read_write)
        check.pack(side='left', padx=8)
        if revision is not None:
            check.state(['disabled'])
        self.mount_button = ttk.Button(row, text='Mount', command=self.mount)
        self.mount_button.pack(side='right')
        self.table = ttk.Treeview(box, columns=('drive', 'mode', 'state', 'revision', 'pending'),
                                  show='headings', height=7, selectmode='browse')
        for name, title in [('drive', 'Drive'), ('mode', 'Mode'), ('state', 'State'),
                            ('revision', 'Revision'), ('pending', 'Pending')]:
            self.table.heading(name, text=title)
            self.table.column(name, width=110)
        self.table.pack(fill='both', expand=True, pady=12)
        controls = ttk.Frame(box)
        controls.pack(fill='x')
        self.flush_button = ttk.Button(controls, text='Flush changes', command=lambda: self.control('flush'))
        self.flush_button.pack(side='left')
        self.unmount_button = ttk.Button(controls, text='Unmount', command=lambda: self.control('unmount'))
        self.unmount_button.pack(side='left', padx=8)
        ttk.Button(controls, text='Refresh', command=self.refresh).pack(side='right')
        self.message = tk.StringVar(value='Read-only is the default. Close open files before unmounting.')
        ttk.Label(box, textvariable=self.message, wraplength=775).pack(anchor='w', pady=(12, 0))
        credit = ttk.Label(box, text='WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos',
                           cursor='hand2')
        credit.pack(anchor='w', pady=(8, 0))
        credit.bind('<Button-1>', lambda _event: webbrowser.open('https://github.com/winfsp/winfsp'))
        if os.name != 'nt':
            self.mount_button.state(['disabled'])
            self.message.set('Drive mounting requires Windows and the optional WinFsp dependency.')
        self.items: dict[str, dict] = {}
        self.refresh()
        self.after(2000, self.poll)

    def refresh(self):
        selected = self.table.selection()
        self.table.delete(*self.table.get_children())
        self.items = {}
        for item in list_mounts(self.image):
            if item['state'] == 'unmounted':
                continue
            key = str(item['workspace'])
            self.items[key] = item
            self.table.insert('', 'end', iid=key, values=(item['drive'],
                'Read-only' if item['read_only'] else 'Read/write', item['state'],
                item.get('revision', ''), item.get('pending', 0)))
            if item.get('error'):
                self.message.set(f"{item['drive']} {item['error']} — recovery folder: {key}")
        if selected and selected[0] in self.items:
            self.table.selection_set(selected[0])
        elif self.items:
            self.table.selection_set(next(iter(self.items)))

    def poll(self):
        if not self.winfo_exists():
            return
        if not self.working:
            self.refresh()
        self.after(2000, self.poll)

    def run(self, action: Callable[[], dict], text: str):
        if self.working:
            return
        self.working = True
        self.message.set(text)
        for button in (self.mount_button, self.flush_button, self.unmount_button):
            button.state(['disabled'])
        def complete(result: str, failed: bool):
            if not self.winfo_exists():
                return
            self.working = False
            for button in (self.mount_button, self.flush_button, self.unmount_button):
                button.state(['!disabled'])
            self.message.set(result)
            self.refresh()
            if failed:
                messagebox.showerror('PMF mount', result, parent=self)
        def worker():
            try:
                result = action()
                message = f"{result.get('drive', '')}: {result.get('state', 'saved')} — revision {result.get('revision', '')}"
                failed = False
            except Exception as exc:
                message, failed = str(exc), True
            try:
                self.after(0, lambda: complete(message, failed))
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def mount(self):
        drive, read_only = self.drive.get(), not self.read_write.get()
        self.run(lambda: start_mount(self.image, drive, read_only=read_only, revision=self.revision),
                 'Mounting image…')

    def control(self, action: str):
        selection = self.table.selection()
        if selection:
            drive = self.items[selection[0]]['drive']
            self.run(lambda: control_mount(drive, action, image_path=self.image),
                     'Saving pending changes…' if action == 'flush' else 'Flushing and unmounting…')
