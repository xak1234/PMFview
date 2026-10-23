#!/usr/bin/env python3
"""
PMF Viewer — professional dark-themed browser/extractor/creator for DiskGenius
.pmf partition backup images.

Usage:  python pmfview_gui.py [image.pmf]

Standard library only (tkinter). All format parsing and writing lives in pmfview.py.
The first open of an image builds an index cache (<image>.pmfindex); later
opens load in seconds.
"""
from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from pmf_update import update_pmf_image
from pmfview import Entry, PmfError, PmfImage, create_pmf_image, ft_str, human, extract_file, extraction_target, make_extraction_directory

# ---------------------------------------------------------------- theme ----

BG        = "#16181d"   # window background
PANEL     = "#1d2026"   # panels / table background
PANEL2    = "#232730"   # toolbar / inputs
BORDER    = "#2e333d"
FG        = "#d7dae0"
FG_DIM    = "#8b919d"
ACCENT    = "#4f9cf9"
SEL_BG    = "#274b6d"
HOVER     = "#262b34"
ROW_ALT   = "#20242c"
OK_GREEN  = "#5fb87a"
WARN_RED  = "#e06c75"

FOLDER_GLYPH = "\U0001F5C0"  # 🗀 outline folder
FILE_GLYPH   = ""


def apply_dark_titlebar(root: tk.Misc) -> None:
    """Windows 10/11 immersive dark title bar."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 pre-20H1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break
    except Exception:
        pass


def setup_style(root: tk.Tk) -> None:
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use("clam")

    base = ("Segoe UI", 10)
    bold = ("Segoe UI Semibold", 10)
    root.option_add("*Font", base)

    style.configure(".", background=BG, foreground=FG, bordercolor=BORDER,
                    darkcolor=BG, lightcolor=BG, troughcolor=PANEL,
                    fieldbackground=PANEL2, font=base, borderwidth=0,
                    focuscolor=ACCENT)

    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Toolbar.TFrame", background=PANEL2)

    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Toolbar.TLabel", background=PANEL2, foreground=FG_DIM)
    style.configure("Status.TLabel", background=PANEL2, foreground=FG_DIM,
                    font=("Segoe UI", 9))
    style.configure("Crumb.TLabel", background=BG, foreground=FG_DIM,
                    font=("Segoe UI", 10))

    style.configure("TButton", background=PANEL2, foreground=FG, padding=(12, 5),
                    borderwidth=1, relief="flat")
    style.map("TButton",
              background=[("pressed", SEL_BG), ("active", HOVER)],
              bordercolor=[("focus", ACCENT)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    font=bold)
    style.map("Accent.TButton",
              background=[("pressed", "#3d82d6"), ("active", "#63a9fa"),
                          ("disabled", PANEL2)],
              foreground=[("disabled", FG_DIM)])
    style.configure("Tool.TButton", background=PANEL2, padding=(8, 4))
    style.map("Tool.TButton", background=[("active", HOVER)])

    style.configure("TEntry", fieldbackground=PANEL2, foreground=FG,
                    insertcolor=FG, padding=6, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER)
    style.map("TEntry", bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])

    style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, rowheight=26, borderwidth=0, relief="flat")
    style.configure("Treeview.Heading", background=PANEL2, foreground=FG_DIM,
                    font=("Segoe UI Semibold", 9), relief="flat",
                    padding=(8, 6))
    style.map("Treeview.Heading", background=[("active", HOVER)])
    style.map("Treeview",
              background=[("selected", SEL_BG)],
              foreground=[("selected", "#ffffff")])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    style.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=PANEL,
                    bordercolor=PANEL, arrowcolor=FG_DIM, relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", BORDER)])
    style.configure("Horizontal.TScrollbar", background=PANEL2,
                    troughcolor=PANEL, bordercolor=PANEL, arrowcolor=FG_DIM)

    style.configure("TProgressbar", background=ACCENT, troughcolor=PANEL2,
                    borderwidth=0, thickness=3)

    style.configure("TPanedwindow", background=BG)
    style.configure("Sash", sashthickness=5, gripcount=0)


# ------------------------------------------------------------------ app ----


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("PMF Viewer")
        root.geometry("1280x780")
        root.minsize(900, 560)
        setup_style(root)
        apply_dark_titlebar(root)
        icon = Path(__file__).with_name("pmfview.png")
        if icon.exists():
            try:
                self._icon = tk.PhotoImage(file=str(icon))
                root.iconphoto(True, self._icon)
            except tk.TclError:
                pass

        self.img: PmfImage | None = None
        self.q: queue.Queue = queue.Queue()
        self.cwd: int | None = None            # current directory recno
        self.history: list[int] = []
        self.search_results: list[int] | None = None
        self.sort_key = "name"
        self.sort_desc = False
        self._dir_nodes: dict[str, int] = {}   # tree node id -> recno
        self._dir_loaded: set[str] = set()
        self._row_recno: dict[str, int] = {}   # table row id -> recno
        self._busy = False

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._bind_keys()
        root.after(80, self._poll)

    # ------------------------------------------------------------- layout

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(10, 8))
        bar.pack(fill="x")

        self.btn_open = ttk.Button(bar, text="Open Image", style="Accent.TButton",
                                   command=self.open_dialog)
        self.btn_open.pack(side="left")

        self.btn_create = ttk.Button(bar, text="Create Image…",
                                     command=self.create_dialog)
        self.btn_create.pack(side="left", padx=(8, 0))

        self.btn_extract = ttk.Button(bar, text="Extract Selected…",
                                      command=self.extract_selected, state="disabled")
        self.btn_extract.pack(side="left", padx=(8, 0))

        self.btn_up = ttk.Button(bar, text="↑ Up", style="Tool.TButton",
                                 command=self.go_up, state="disabled")
        self.btn_up.pack(side="left", padx=(16, 0))
        self.btn_back = ttk.Button(bar, text="← Back", style="Tool.TButton",
                                   command=self.go_back, state="disabled")
        self.btn_back.pack(side="left", padx=(4, 0))

        right = ttk.Frame(bar, style="Toolbar.TFrame")
        right.pack(side="right")
        self.search_var = tk.StringVar()
        se = ttk.Entry(right, textvariable=self.search_var, width=42)
        se.pack(side="left", ipady=1)
        se.insert(0, "")
        se.bind("<Return>", lambda e: self.do_search())
        se.bind("<Escape>", lambda e: self.clear_search())
        self._search_entry = se
        ttk.Button(right, text="Search", style="Tool.TButton",
                   command=self.do_search).pack(side="left", padx=(6, 0))
        ttk.Label(bar, text="name or glob — Enter to search, Esc to clear",
                  style="Toolbar.TLabel").pack(side="right", padx=(0, 10))

        revisions = ttk.Frame(self.root, style="Toolbar.TFrame", padding=(10, 0, 10, 8))
        revisions.pack(fill="x")
        self.btn_update = ttk.Button(revisions, text="Incremental Update…",
                                     command=self.update_dialog, state="disabled")
        self.btn_update.pack(side="left")
        self.btn_versions = ttk.Button(revisions, text="Versions…",
                                       command=self.versions_dialog, state="disabled")
        self.btn_versions.pack(side="left", padx=(8, 0))
        self.btn_refresh = ttk.Button(revisions, text="Refresh Latest",
                                      command=self.refresh_latest, state="disabled")
        self.btn_refresh.pack(side="left", padx=(8, 0))
        self.btn_mount = ttk.Button(revisions, text="Mount Image…",
                                    command=self.mount_dialog, state="disabled")
        self.btn_mount.pack(side="left", padx=(8, 0))
        ttk.Label(revisions, text="Updates stay in this PMF; earlier versions remain readable.",
                  style="Toolbar.TLabel").pack(side="left", padx=16)

    def _build_body(self):
        crumb_row = ttk.Frame(self.root, padding=(12, 8, 12, 4))
        crumb_row.pack(fill="x")
        self.crumb = ttk.Label(crumb_row, text="No image loaded — open a .pmf backup",
                               style="Crumb.TLabel", anchor="w")
        self.crumb.pack(fill="x")

        panes = ttk.Panedwindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        # left: folder tree
        left_wrap = ttk.Frame(panes, style="Panel.TFrame", padding=1)
        self.tree = ttk.Treeview(left_wrap, show="tree", selectmode="browse")
        tsb = ttk.Scrollbar(left_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        panes.add(left_wrap, weight=1)

        # right: file table
        right_wrap = ttk.Frame(panes, style="Panel.TFrame", padding=1)
        cols = ("size", "type", "modified", "path")
        self.table = ttk.Treeview(right_wrap, columns=cols, show="tree headings",
                                  selectmode="extended")
        self.table.heading("#0", text="Name",
                           command=lambda: self._sort_by("name"))
        self.table.heading("size", text="Size",
                           command=lambda: self._sort_by("size"))
        self.table.heading("type", text="Type",
                           command=lambda: self._sort_by("type"))
        self.table.heading("modified", text="Modified",
                           command=lambda: self._sort_by("mtime"))
        self.table.heading("path", text="Location")
        self.table.column("#0", width=430, stretch=True)
        self.table.column("size", width=110, anchor="e", stretch=False)
        self.table.column("type", width=90, anchor="w", stretch=False)
        self.table.column("modified", width=150, anchor="center", stretch=False)
        self.table.column("path", width=340, stretch=True)
        self.table.configure(displaycolumns=("size", "type", "modified"))
        fsb = ttk.Scrollbar(right_wrap, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=fsb.set)
        self.table.pack(side="left", fill="both", expand=True)
        fsb.pack(side="right", fill="y")
        self.table.tag_configure("odd", background=ROW_ALT)
        self.table.tag_configure("dir", foreground="#8fb8f2")
        self.table.bind("<Double-1>", self._on_table_double)
        self.table.bind("<Return>", self._on_table_double)
        self.table.bind("<<TreeviewSelect>>", lambda e: self._update_selection_status())
        self.table.bind("<Button-3>", self._on_context)
        panes.add(right_wrap, weight=3)

        self.menu = tk.Menu(self.root, tearoff=0, bg=PANEL2, fg=FG,
                            activebackground=SEL_BG, activeforeground="#ffffff",
                            bd=0, relief="flat")
        self.menu.add_command(label="Preview", command=self.preview_selected)
        self.menu.add_command(label="Extract…", command=self.extract_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Copy full path", command=self.copy_path)
        self.menu.add_command(label="SHA-256", command=self.hash_selected)

    def _build_statusbar(self):
        wrap = ttk.Frame(self.root, style="Toolbar.TFrame")
        wrap.pack(fill="x", side="bottom")
        self.pbar = ttk.Progressbar(wrap, mode="indeterminate")
        # (packed only while busy)
        inner = ttk.Frame(wrap, style="Toolbar.TFrame", padding=(12, 5))
        inner.pack(fill="x")
        self.status_l = ttk.Label(inner, text="Ready", style="Status.TLabel", anchor="w")
        self.status_l.pack(side="left")
        self.status_r = ttk.Label(inner, text="", style="Status.TLabel", anchor="e")
        self.status_r.pack(side="right")

    def _bind_keys(self):
        self.root.bind("<Control-o>", lambda e: self.open_dialog())
        self.root.bind("<Control-f>", lambda e: self._search_entry.focus_set())
        self.root.bind("<BackSpace>", self._maybe_up)
        self.root.bind("<Alt-Left>", lambda e: self.go_back())

    def _maybe_up(self, event):
        if event.widget in (self.table, self.tree):
            self.go_up()

    # -------------------------------------------------------------- busy

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        if busy:
            self.pbar.pack(fill="x", side="top")
            self.pbar.start(14)
            self.status_l.configure(text=text or "Working…", foreground=FG_DIM)
        else:
            self.pbar.stop()
            self.pbar.pack_forget()
        for b in (self.btn_open, self.btn_create, self.btn_extract, self.btn_update, self.btn_versions, self.btn_refresh, self.btn_mount):
            b.state(["disabled"] if busy else ["!disabled"])
        if not busy and self.img is not None and (self.img.requested_revision is not None
                or self.img.index_errors or self.img.index_issues or self.img.incremental_warning):
            self.btn_update.state(["disabled"])
        if not busy and self.img is None:
            for b in (self.btn_extract, self.btn_update, self.btn_versions, self.btn_refresh, self.btn_mount):
                b.state(["disabled"])

    # ------------------------------------------------------------ loading

    def mount_dialog(self):
        if self._busy or self.img is None:
            return
        from pmf_mount_gui import MountWindow
        MountWindow(self.root, self.img.path, self.img.requested_revision)

    def open_dialog(self):
        if self._busy:
            return
        p = filedialog.askopenfilename(
            title="Open DiskGenius image",
            filetypes=[("PMF images", "*.pmf"), ("All files", "*.*")])
        if p:
            self.open_image(Path(p))

    def create_dialog(self):
        if self._busy:
            return
        CreateImageWindow(self.root, self.create_image)

    def update_dialog(self):
        if self._busy or self.img is None:
            return
        CreateImageWindow(self.root, self.update_image, image_path=self.img.path)

    def update_image(self, path: Path, sources: list[Path], hot_image: bool = False,
                     image_root: bool = False, preview: bool = False, delete_missing: bool = True):
        if self._busy:
            return
        self._set_busy(True, "Comparing source folders…")
        def work():
            try:
                result = update_pmf_image(path, sources, hot_image=hot_image,
                    image_root=image_root, dry_run=preview, delete_missing=delete_missing,
                    progress=lambda text: self.q.put(("status", text)))
                self.q.put(("update_done", (path, result)))
            except Exception as exc:
                self.q.put(("error", f"Update cancelled: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def refresh_latest(self):
        if not self._busy and self.img is not None:
            self.open_image(self.img.path)

    def versions_dialog(self):
        if self._busy or self.img is None:
            return
        img = self.img
        window = tk.Toplevel(self.root, bg=BG)
        window.title("PMF Versions")
        window.geometry("780x360")
        window.transient(self.root)
        apply_dark_titlebar(window)
        listing = tk.Listbox(window, bg=PANEL, fg=FG, selectbackground=SEL_BG,
                             font=("Segoe UI", 10))
        listing.pack(fill="both", expand=True, padx=12, pady=12)
        rows = img.revisions or [{"revision": 0, "created": img.header.created,
                                 "summary": "Original image"}]
        for row in rows:
            listing.insert("end", f"{row['revision']}   {row['created']}   {row['summary']}")
        listing.selection_set(min(img.revision, len(rows) - 1))
        def choose():
            selected = listing.curselection()
            if selected:
                revision = int(rows[selected[0]]["revision"])
                window.destroy()
                self.open_image(img.path, revision=revision)
        ttk.Button(window, text="Open Selected Version", command=choose).pack(pady=(0, 12))
        listing.bind("<Double-1>", lambda _event: choose())

    def create_image(
        self, out_path: Path, sources: list[Path], hot_image: bool = False
    ):
        names = ", ".join(p.name for p in sources[:3])
        if len(sources) > 3:
            names += f", +{len(sources) - 3} more"
        self._set_busy(True, f"Creating {out_path.name} from {names}…")

        def progress(n, total, _entry):
            if n == total or n % 200 == 0:
                self.q.put(("status", f"Creating image… {n:,}/{total:,} entries"))

        def work():
            try:
                files, dirs, total = create_pmf_image(
                    out_path,
                    sources,
                    image_name=out_path.stem,
                    overwrite=True,
                    progress=progress,
                    hot_image=hot_image,
                )
                self.q.put(("create_done", (out_path, files, dirs, total)))
            except Exception as e:
                self.q.put(("error", f"Could not create image:\n{e}"))

        threading.Thread(target=work, daemon=True).start()

    def open_image(self, path: Path, revision: int | None = None):
        if self._busy:
            return
        self._set_busy(True, f"Opening {path.name} — building index "
                             f"(first time can take a minute)…")

        def work():
            img = None
            try:
                img = PmfImage(path, revision=revision)
                img.load_index(progress=False)
                self.q.put(("loaded", img))
            except Exception as e:
                if img is not None:
                    img.close()
                self.q.put(("error", f"Could not open image:\n{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "loaded":
                    self._set_busy(False)
                    if self.img is not None:
                        self.img.close()
                    self.img = payload
                    self._after_load()
                elif kind == "error":
                    self._set_busy(False)
                    self.status_l.configure(text=payload.replace("\n", " "),
                                            foreground=WARN_RED)
                elif kind == "status":
                    self.status_l.configure(text=payload)
                elif kind == "extract_done":
                    self._set_busy(False)
                    ok, fail, out = payload
                    msg = f"Extracted {ok:,} file(s)"
                    if fail:
                        msg += f" — {fail} FAILED"
                    self.status_l.configure(
                        text=f"{msg} → {out}",
                        foreground=WARN_RED if fail else OK_GREEN,
                    )
                elif kind == "create_done":
                    self._set_busy(False)
                    out_path, files, dirs, total = payload
                    msg = (f"Created {out_path.name}: {files:,} files, "
                           f"{dirs:,} folders, {human(total)}")
                    self.status_l.configure(text=msg, foreground=OK_GREEN)
                    self.open_image(out_path)
                elif kind == "update_done":
                    self._set_busy(False)
                    path, result = payload
                    text = (f"{result.added} added, {result.changed} changed, "
                            f"{result.deleted} deleted, {result.unchanged} unchanged.\n"
                            f"Revision {result.revision}; appended {human(result.written)}.")
                    messagebox.showinfo("Update Preview" if result.dry_run else "Incremental Update", text,
                                        parent=self.root)
                    if not result.dry_run:
                        self.open_image(path)
                elif kind == "hash_done":
                    self._set_busy(False)
                    name, digest = payload
                    self.root.clipboard_clear()
                    self.root.clipboard_append(digest)
                    self.status_l.configure(
                        text=f"SHA-256 copied for {name}: {digest}",
                        foreground=OK_GREEN,
                    )
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _after_load(self):
        img = self.img
        files = sum(1 for e in img.entries.values() if not e.is_dir)
        total = sum(e.size for e in img.entries.values() if not e.is_dir)
        self.root.title(f"PMF Viewer — {img.path.name} — revision {img.revision}")
        self.status_l.configure(
            text=f"{img.path.name}   ·   {files:,} files, "
                 f"{len(img.entries)-files:,} folders   ·   {human(total)}   ·   "
                 f"revision {img.revision}   ·   {img.incremental_warning or img.header.disk_model.strip()}")
        for button in (self.btn_extract, self.btn_update, self.btn_versions, self.btn_refresh, self.btn_mount):
            button.state(["!disabled"])
        self.btn_update.state(["disabled"] if img.requested_revision is not None else ["!disabled"])
        if img.incremental_warning or img.index_errors or img.index_issues:
            self.btn_update.state(["disabled"])
            self.status_l.configure(foreground=WARN_RED,
                text=f"{img.path.name} · revision {img.revision} · "
                     f"{img.incremental_warning or 'Metadata needs recovery; use CLI repair-mft for a report.'}")
        self.btn_up.state(["!disabled"])
        self.history.clear()
        self.search_results = None
        self._populate_tree()
        self._navigate(img.root_recno, push=False)

    # -------------------------------------------------------- folder tree

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._dir_nodes.clear()
        self._dir_loaded.clear()
        root_id = self.tree.insert("", "end", text=f"  {self.img.path.stem}",
                                   open=True)
        self._dir_nodes[root_id] = self.img.root_recno
        self._fill_tree(root_id, self.img.root_recno)
        self._dir_loaded.add(root_id)

    def _fill_tree(self, node: str, recno: int):
        img = self.img
        for r in img.children.get(recno, []):
            e = img.entries[r]
            if not e.is_dir:
                continue
            nid = self.tree.insert(node, "end", text=f"  {e.name}")
            self._dir_nodes[nid] = r
            if any(img.entries[c].is_dir for c in img.children.get(r, [])):
                self.tree.insert(nid, "end", text="…")

    def _on_tree_open(self, _e):
        node = self.tree.focus()
        if node in self._dir_loaded or node not in self._dir_nodes:
            return
        self.tree.delete(*self.tree.get_children(node))
        self._fill_tree(node, self._dir_nodes[node])
        self._dir_loaded.add(node)

    def _on_tree_select(self, _e):
        sel = self.tree.selection()
        if sel and sel[0] in self._dir_nodes:
            recno = self._dir_nodes[sel[0]]
            if recno != self.cwd:
                self._navigate(recno)

    # -------------------------------------------------------- navigation

    def _navigate(self, recno: int, push: bool = True):
        if self.img is None:
            return
        if push and self.cwd is not None and self.cwd != recno:
            self.history.append(self.cwd)
            self.btn_back.state(["!disabled"])
        self.cwd = recno
        self.search_results = None
        path = self.img.full_path(recno)
        self.crumb.configure(text="  " + (path or "\\") if path else "  \\  (root)")
        self._refresh_table()

    def go_up(self):
        if self.img is None or self.cwd is None:
            return
        e = self.img.entries.get(self.cwd)
        if self.cwd == self.img.root_recno:
            return
        parent = e.parent if e else self.img.root_recno
        if parent not in self.img.entries and parent != self.img.root_recno:
            parent = self.img.root_recno
        self._navigate(parent)

    def go_back(self):
        if self.history:
            recno = self.history.pop()
            if not self.history:
                self.btn_back.state(["disabled"])
            self._navigate(recno, push=False)

    # -------------------------------------------------------- file table

    def _current_rows(self) -> list[int]:
        img = self.img
        if self.search_results is not None:
            rows = self.search_results
        else:
            rows = img.children.get(self.cwd, [])
        key = self.sort_key

        def k(r):
            e = img.entries[r]
            if key == "size":
                return (not e.is_dir, e.size)
            if key == "mtime":
                return (not e.is_dir, e.mtime)
            if key == "type":
                ext = "" if e.is_dir else Path(e.name).suffix.lower()
                return (not e.is_dir, ext, e.name.lower())
            return (not e.is_dir, e.name.lower())

        return sorted(rows, key=k, reverse=self.sort_desc)

    def _refresh_table(self):
        img = self.img
        self.table.delete(*self.table.get_children())
        self._row_recno.clear()
        showing_search = self.search_results is not None
        self.table.configure(
            displaycolumns=("size", "type", "modified", "path")
            if showing_search else ("size", "type", "modified"))
        rows = self._current_rows()
        cap = 5000
        for i, r in enumerate(rows[:cap]):
            e = img.entries[r]
            if e.is_dir:
                size, typ = "", "folder"
                name = f"{FOLDER_GLYPH}  {e.name}"
                tags = ["dir"]
            else:
                size = human(e.size)
                typ = Path(e.name).suffix.lstrip(".").lower() or "file"
                name = f"     {e.name}"
                tags = []
            if i % 2:
                tags.append("odd")
            loc = ""
            if showing_search:
                full = img.full_path(r)
                loc = full.rsplit("\\", 1)[0] if "\\" in full else "\\"
            rid = self.table.insert("", "end", text=name,
                                    values=(size, typ, ft_str(e.mtime), loc),
                                    tags=tags)
            self._row_recno[rid] = r
        n = len(rows)
        note = f" (showing first {cap:,})" if n > cap else ""
        what = "result" if showing_search else "item"
        self.status_r.configure(text=f"{n:,} {what}{'s' if n != 1 else ''}{note}")

    def _sort_by(self, key: str):
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key, self.sort_desc = key, key in ("size", "mtime")
        self._refresh_table()

    def _update_selection_status(self):
        if self.img is None:
            return
        sel = [self._row_recno[r] for r in self.table.selection()
               if r in self._row_recno]
        if not sel:
            self._refresh_status_count()
            return
        total = sum(self.img.entries[r].size for r in sel
                    if not self.img.entries[r].is_dir)
        self.status_r.configure(
            text=f"{len(sel):,} selected   ·   {human(total)}")

    def _refresh_status_count(self):
        n = len(self.table.get_children())
        what = "result" if self.search_results is not None else "item"
        self.status_r.configure(text=f"{n:,} {what}{'s' if n != 1 else ''}")

    def _on_table_double(self, _e):
        rows = self.table.selection()
        if not rows:
            return
        recno = self._row_recno.get(rows[0])
        if recno is None:
            return
        e = self.img.entries[recno]
        if e.is_dir:
            self._navigate(recno)
        else:
            self.preview_selected()

    def _on_context(self, event):
        row = self.table.identify_row(event.y)
        if row and row not in self.table.selection():
            self.table.selection_set(row)
        if self.table.selection():
            self.menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------ search

    def do_search(self):
        if self._busy:
            return
        if self.img is None:
            return
        pat = self.search_var.get().strip().lower()
        if not pat:
            self.clear_search()
            return
        self._set_busy(True, f"Searching for “{pat}”…")

        def work():
            img = self.img
            is_glob = any(c in pat for c in "*?[")
            hits = []
            for e in img.entries.values():
                name = e.name.lower()
                if fnmatch.fnmatch(name, pat) if is_glob else pat in name:
                    hits.append(e.recno)
                    if len(hits) >= 20000:
                        break
            self.q.put(("status", f"Search: {len(hits):,} match(es) for “{pat}”"))
            self.search_results = hits
            self.root.after(0, self._refresh_table)
            self.root.after(0, lambda: self._set_busy(False))
            self.root.after(0, lambda: self.crumb.configure(
                text=f"  Search results — “{pat}”"))

        threading.Thread(target=work, daemon=True).start()

    def clear_search(self):
        self.search_var.set("")
        if self.img is not None and self.search_results is not None:
            self.search_results = None
            self._navigate(self.cwd or self.img.root_recno, push=False)

    # --------------------------------------------------------- actions

    def _selected_recnos(self) -> list[int]:
        recnos = [self._row_recno[r] for r in self.table.selection()
                  if r in self._row_recno]
        if not recnos:
            sel = self.tree.selection()
            if sel and sel[0] in self._dir_nodes:
                recnos = [self._dir_nodes[sel[0]]]
        return recnos

    def copy_path(self):
        recnos = self._selected_recnos()
        if recnos:
            paths = "\n".join("C:\\" + self.img.full_path(r) for r in recnos)
            self.root.clipboard_clear()
            self.root.clipboard_append(paths)
            self.status_l.configure(text=f"Copied {len(recnos)} path(s) to clipboard")

    def hash_selected(self):
        if self._busy:
            return
        recnos = self._selected_recnos()
        if not recnos:
            return
        e = self.img.entries[recnos[0]]
        if e.is_dir:
            self.status_l.configure(text="Select a file to calculate SHA-256.",
                                    foreground=WARN_RED)
            return
        self._set_busy(True, f"Hashing {e.name}…")

        def work():
            try:
                h = hashlib.sha256()
                for chunk in self.img.iter_file_data(e):
                    h.update(chunk)
                self.q.put(("hash_done", (e.name, h.hexdigest())))
            except Exception as ex:
                self.q.put(("error", f"Hash failed: {ex}"))

        threading.Thread(target=work, daemon=True).start()

    def preview_selected(self):
        if self._busy:
            return
        recnos = self._selected_recnos()
        if not recnos:
            return
        e = self.img.entries[recnos[0]]
        if e.is_dir:
            self._navigate(e.recno)
            return
        try:
            data = self.img.read_file(e, max_bytes=256 * 1024)
        except PmfError as ex:
            self.status_l.configure(text=f"Preview failed: {ex}",
                                    foreground=WARN_RED)
            return
        PreviewWindow(self.root, self.img.full_path(e.recno), e, data)

    def extract_selected(self):
        if self._busy:
            return
        if self.img is None:
            return
        recnos = self._selected_recnos()
        if not recnos:
            self.status_l.configure(
                text="Select files or folders to extract first.",
                foreground=WARN_RED,
            )
            return
        out = filedialog.askdirectory(title="Extract to folder")
        if not out:
            return
        out_dir = Path(out)
        self._set_busy(True, "Extracting…")

        def work():
            try:
                img = self.img
                ok = fail = 0
                for recno in recnos:
                    base_e = img.entries.get(recno)
                    if base_e is None:
                        continue
                    base_parent = (img.full_path(base_e.parent)
                                   if base_e.parent in img.entries else "")
                    for r in img.walk(recno):
                        e = img.entries[r]
                        full = img.full_path(r)
                        rel = (full[len(base_parent):].lstrip("\\")
                               if base_parent and full.startswith(base_parent) else full)
                        dest = extraction_target(out_dir, rel)
                        if e.is_dir:
                            make_extraction_directory(dest)
                            continue
                        try:
                            extract_file(img, e, dest)
                            ok += 1
                            if ok % 200 == 0:
                                self.q.put(("status", f"Extracting… {ok:,} files"))
                        except (PmfError, OSError):
                            fail += 1
                self.q.put(("extract_done", (ok, fail, str(out_dir))))
            except Exception as exc:
                self.q.put(("error", f"Extraction failed: {exc}"))

        threading.Thread(target=work, daemon=True).start()


class CreateImageWindow(tk.Toplevel):
    """Application-owned folder picker for building a PMF image."""

    def __init__(self, master: tk.Misc, create_callback, image_path: Path | None = None):
        super().__init__(master, bg=BG)
        self.create_callback = create_callback
        self.image_path = image_path
        title = "Incremental Update" if image_path else "Create PMF Image"
        self.sources: list[Path] = []
        self._node_paths: dict[str, Path] = {}
        self._loaded: set[str] = set()

        self.title(title)
        self.geometry("920x680")
        self.minsize(720, 520)
        self.transient(master)
        apply_dark_titlebar(self)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        header = ttk.Frame(self, style="Toolbar.TFrame", padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text=title,
                  style="Toolbar.TLabel",
                  font=("Segoe UI Semibold", 13),
                  foreground=FG).pack(side="left")
        ttk.Label(header, text="Select folders with Ctrl or Shift",
                  style="Toolbar.TLabel").pack(side="right")

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        ttk.Label(body, text="Folders").grid(row=0, column=0, sticky="w",
                                             pady=(0, 6))
        ttk.Label(body, text="Included in image").grid(row=0, column=1,
                                                       sticky="w",
                                                       padx=(14, 0),
                                                       pady=(0, 6))

        tree_wrap = ttk.Frame(body, style="Panel.TFrame", padding=1)
        tree_wrap.grid(row=1, column=0, sticky="nsew")
        self.folder_tree = ttk.Treeview(tree_wrap, show="tree",
                                        selectmode="extended")
        tree_scroll = ttk.Scrollbar(tree_wrap, orient="vertical",
                                    command=self.folder_tree.yview)
        self.folder_tree.configure(yscrollcommand=tree_scroll.set)
        self.folder_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.folder_tree.bind("<<TreeviewOpen>>", self._open_node)
        self.folder_tree.bind("<Double-1>", lambda _e: self._add_selected())
        self._populate_roots()

        selected_wrap = ttk.Frame(body, style="Panel.TFrame", padding=1)
        selected_wrap.grid(row=1, column=1, sticky="nsew", padx=(14, 0))
        self.selected_list = tk.Listbox(
            selected_wrap, bg=PANEL, fg=FG, selectbackground=SEL_BG,
            selectforeground="#ffffff", bd=0, highlightthickness=0,
            activestyle="none", font=("Segoe UI", 10),
        )
        selected_scroll = ttk.Scrollbar(selected_wrap, orient="vertical",
                                        command=self.selected_list.yview)
        self.selected_list.configure(yscrollcommand=selected_scroll.set)
        self.selected_list.pack(side="left", fill="both", expand=True)
        selected_scroll.pack(side="right", fill="y")

        actions = ttk.Frame(body)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 16))
        ttk.Button(actions, text="Add Selected Folders",
                   command=self._add_selected).pack(side="left")
        ttk.Button(actions, text="Remove",
                   command=self._remove_selected).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Clear",
                   command=self._clear_sources).pack(side="left", padx=(8, 0))

        output = ttk.Frame(body)
        output.grid(row=3, column=0, columnspan=2, sticky="ew")
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="Save in").grid(row=0, column=0, sticky="w",
                                               padx=(0, 10), pady=4)
        self.output_dir = tk.StringVar(value=str(image_path.parent if image_path else Path.home()))
        ttk.Entry(output, textvariable=self.output_dir, state="readonly" if image_path else "normal").grid(
            row=0, column=1, sticky="ew", pady=4)
        ttk.Button(output, text="Use Selected Folder",
                   command=self._use_selected_output, state="disabled" if image_path else "normal").grid(
            row=0, column=2, padx=(8, 0), pady=4)

        ttk.Label(output, text="Image name").grid(row=1, column=0, sticky="w",
                                                  padx=(0, 10), pady=4)
        self.image_name = tk.StringVar(value=image_path.name if image_path else "backup.pmf")
        name_entry = ttk.Entry(output, textvariable=self.image_name, state="readonly" if image_path else "normal")
        name_entry.grid(row=1, column=1, sticky="ew", pady=4)

        self.hot_image = tk.BooleanVar(value=os.name == "nt" and image_path is None)
        ttk.Checkbutton(
            output,
            text="Hot image (Volume Shadow Copy)",
            variable=self.hot_image,
            state="normal" if os.name == "nt" else "disabled",
        ).grid(row=2, column=1, sticky="w", pady=(8, 2))

        self.image_root = tk.BooleanVar(value=False)
        self.preview = tk.BooleanVar(value=False)
        self.delete_missing = tk.BooleanVar(value=True)
        if image_path:
            self.geometry("920x780")
            ttk.Checkbutton(output, text="Source contents match image root (one folder/drive)",
                            variable=self.image_root).grid(row=3, column=1, sticky="w")
            ttk.Checkbutton(output, text="Mirror deletions in selected folders (kept in older versions)",
                            variable=self.delete_missing).grid(row=4, column=1, sticky="w")
            ttk.Checkbutton(output, text="Preview changes only — do not write",
                            variable=self.preview).grid(row=5, column=1, sticky="w")

        footer = ttk.Frame(self, style="Toolbar.TFrame", padding=(16, 10))
        footer.pack(fill="x", side="bottom")
        self.feedback = ttk.Label(footer, text="", style="Toolbar.TLabel")
        self.feedback.pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="Cancel",
                   command=self.destroy).pack(side="right")
        ttk.Button(footer, text="Update" if image_path else "Create", style="Accent.TButton",
                   command=self._create).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Control-Return>", lambda _e: self._create())
        self.grab_set()
        self.after(0, self.folder_tree.focus_set)

    def _roots(self) -> list[Path]:
        if os.name != "nt":
            return [Path("/")]
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [Path(f"{chr(65 + i)}:\\") for i in range(26)
                if mask & (1 << i)]

    def _populate_roots(self):
        for path in self._roots():
            node = self.folder_tree.insert("", "end", text=str(path),
                                           open=False)
            self._node_paths[node] = path
            self.folder_tree.insert(node, "end", text="")

    def _open_node(self, _event=None):
        node = self.folder_tree.focus()
        if not node or node in self._loaded:
            return
        self.folder_tree.delete(*self.folder_tree.get_children(node))
        path = self._node_paths[node]
        try:
            children = sorted(
                (p for p in path.iterdir() if p.is_dir() and not p.is_symlink()),
                key=lambda p: p.name.lower(),
            )
        except (OSError, PermissionError):
            children = []
        for child in children:
            item = self.folder_tree.insert(node, "end", text=child.name)
            self._node_paths[item] = child
            try:
                if any(p.is_dir() and not p.is_symlink()
                       for p in child.iterdir()):
                    self.folder_tree.insert(item, "end", text="")
            except (OSError, PermissionError):
                pass
        self._loaded.add(node)

    def _selected_paths(self) -> list[Path]:
        return [self._node_paths[n] for n in self.folder_tree.selection()
                if n in self._node_paths]

    def _add_selected(self):
        for path in self._selected_paths():
            if path not in self.sources:
                self.sources.append(path)
                self.selected_list.insert("end", str(path))
        self._feedback(f"{len(self.sources)} folder(s) included", error=False)

    def _remove_selected(self):
        for index in reversed(self.selected_list.curselection()):
            del self.sources[index]
            self.selected_list.delete(index)
        self._feedback(f"{len(self.sources)} folder(s) included", error=False)

    def _clear_sources(self):
        self.sources.clear()
        self.selected_list.delete(0, "end")
        self._feedback("", error=False)

    def _use_selected_output(self):
        paths = self._selected_paths()
        if len(paths) != 1:
            self._feedback("Select one folder to use as the destination.")
            return
        self.output_dir.set(str(paths[0]))
        self._feedback("", error=False)

    def _feedback(self, message: str, error: bool = True):
        self.feedback.configure(text=message,
                                foreground=WARN_RED if error else FG_DIM)

    def _create(self):
        self._add_selected()
        if not self.sources:
            self._feedback("Select and add at least one folder.")
            return
        output_dir = Path(self.output_dir.get().strip())
        if not output_dir.is_dir():
            self._feedback("The destination folder does not exist.")
            return
        name = self.image_name.get().strip()
        if not name:
            self._feedback("Enter an image name.")
            return
        if not name.lower().endswith(".pmf"):
            name += ".pmf"
        out_path = output_dir / name
        if out_path.exists() and self.image_path is None:
            self._feedback("An image with that name already exists.")
            return
        sources = list(self.sources)
        hot, root, preview, deletions = (self.hot_image.get(), self.image_root.get(),
                                         self.preview.get(), self.delete_missing.get())
        self.grab_release()
        self.destroy()
        if self.image_path:
            self.create_callback(self.image_path, sources, hot, root, preview, deletions)
        else:
            self.create_callback(out_path, sources, hot)


class PreviewWindow(tk.Toplevel):
    def __init__(self, master, full_path: str, e: Entry, data: bytes):
        super().__init__(master, bg=BG)
        self.title(f"Preview — {e.name}")
        self.geometry("900x600")
        apply_dark_titlebar(self)

        hdr = ttk.Frame(self, style="Toolbar.TFrame", padding=(12, 8))
        hdr.pack(fill="x")
        ttk.Label(hdr, text=full_path, style="Toolbar.TLabel").pack(side="left")
        ttk.Label(hdr, text=f"{human(e.size)}   ·   modified {ft_str(e.mtime)}",
                  style="Toolbar.TLabel").pack(side="right")

        txt = tk.Text(self, bg=PANEL, fg=FG, insertbackground=FG, bd=0,
                      padx=12, pady=10, wrap="none",
                      font=("Cascadia Mono", 10), selectbackground=SEL_BG)
        ysb = ttk.Scrollbar(self, orient="vertical", command=txt.yview)
        xsb = ttk.Scrollbar(self, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        txt.pack(fill="both", expand=True)

        printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in data)
        if data and printable / len(data) > 0.85:
            body = data.decode("utf-8", errors="replace")
            if e.size > len(data):
                body += f"\n\n… truncated preview ({human(len(data))} of {human(e.size)})"
        else:
            lines = []
            for i in range(0, min(len(data), 64 * 1024), 16):
                row = data[i:i + 16]
                hx = " ".join(f"{b:02x}" for b in row)
                asc = "".join(chr(b) if 32 <= b < 127 else "·" for b in row)
                lines.append(f"{i:08x}   {hx:<48}   {asc}")
            body = "\n".join(lines) or "(empty file)"
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        self.bind("<Escape>", lambda _e: self.destroy())


def main():
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1:
        app.open_image(Path(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
