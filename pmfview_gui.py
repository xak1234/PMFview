#!/usr/bin/env python3
"""
pmfview_gui — simple read-only browser/extractor GUI for DiskGenius .pmf images.

Usage:  python pmfview_gui.py [image.pmf]

Requires only the Python standard library (tkinter). Uses pmfview.py for all
format parsing. The first open of an image builds an index cache
(<image>.pmfindex); subsequent opens are instant.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from pmfview import Entry, PmfImage, PmfError, ft_str, human, _safe_rel


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PMF Viewer — DiskGenius image browser (read-only)")
        self.root.geometry("1100x700")
        self.img: PmfImage | None = None
        self.q: queue.Queue = queue.Queue()
        self._node_recno: dict[str, int] = {}
        self._loaded_dirs: set[str] = set()

        bar = ttk.Frame(root, padding=4)
        bar.pack(fill="x")
        ttk.Button(bar, text="Open image...", command=self.open_dialog).pack(side="left")
        self.status = tk.StringVar(value="Open a .pmf image to begin")
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=12)

        search = ttk.Frame(root, padding=(4, 0))
        search.pack(fill="x")
        ttk.Label(search, text="Find:").pack(side="left")
        self.search_var = tk.StringVar()
        se = ttk.Entry(search, textvariable=self.search_var, width=40)
        se.pack(side="left", padx=4)
        se.bind("<Return>", lambda e: self.do_search())
        ttk.Button(search, text="Search", command=self.do_search).pack(side="left")
        ttk.Button(search, text="Extract selected...", command=self.extract_selected).pack(
            side="right", padx=4
        )

        panes = ttk.Panedwindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=4, pady=4)

        # left: directory tree
        left = ttk.Frame(panes)
        self.tree = ttk.Treeview(left, columns=("size", "mtime"), selectmode="extended")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("mtime", text="Modified")
        self.tree.column("#0", width=420)
        self.tree.column("size", width=110, anchor="e")
        self.tree.column("mtime", width=150, anchor="center")
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self.on_open_node)
        panes.add(left, weight=3)

        # right: search results
        right = ttk.Frame(panes)
        ttk.Label(right, text="Search results (double-click to reveal)").pack(anchor="w")
        self.results = ttk.Treeview(
            right, columns=("size", "path"), selectmode="extended", show="headings"
        )
        self.results.heading("size", text="Size")
        self.results.heading("path", text="Path")
        self.results.column("size", width=90, anchor="e")
        self.results.column("path", width=380)
        rsb = ttk.Scrollbar(right, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=rsb.set)
        self.results.pack(side="left", fill="both", expand=True)
        rsb.pack(side="right", fill="y")
        self.results.bind("<Double-1>", self.reveal_result)
        panes.add(right, weight=2)

        self.pbar = ttk.Progressbar(root, mode="indeterminate")
        self.root.after(100, self.poll_queue)

    # ---------- image loading ----------

    def open_dialog(self):
        p = filedialog.askopenfilename(
            title="Open DiskGenius image", filetypes=[("PMF images", "*.pmf"), ("All", "*.*")]
        )
        if p:
            self.open_image(Path(p))

    def open_image(self, path: Path):
        self.status.set(f"Opening {path.name} — building index (first time can take a few minutes)...")
        self.pbar.pack(fill="x", padx=4, pady=2)
        self.pbar.start(12)

        def work():
            try:
                img = PmfImage(path)
                img.load_index(progress=False)
                self.q.put(("loaded", img))
            except Exception as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "loaded":
                    self.img = payload
                    self.pbar.stop()
                    self.pbar.pack_forget()
                    files = sum(1 for e in self.img.entries.values() if not e.is_dir)
                    self.status.set(
                        f"{self.img.path.name} — {len(self.img.entries):,} entries "
                        f"({files:,} files) — created {self.img.header.created}"
                    )
                    self.populate_root()
                elif kind == "error":
                    self.pbar.stop()
                    self.pbar.pack_forget()
                    self.status.set("Failed to open image")
                    messagebox.showerror("PMF Viewer", payload)
                elif kind == "extract_done":
                    ok, fail, out = payload
                    self.pbar.stop()
                    self.pbar.pack_forget()
                    self.status.set(f"Extracted {ok} file(s){f', {fail} failed' if fail else ''} to {out}")
                    messagebox.showinfo(
                        "Extract complete",
                        f"Extracted {ok} file(s){f', {fail} FAILED' if fail else ''}\n\n{out}",
                    )
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    # ---------- tree ----------

    def populate_root(self):
        self.tree.delete(*self.tree.get_children())
        self._node_recno.clear()
        self._loaded_dirs.clear()
        root_id = self.tree.insert("", "end", text="\\ (root)", open=True, values=("", ""))
        self._node_recno[root_id] = self.img.root_recno
        self.fill_children(root_id, self.img.root_recno)
        self._loaded_dirs.add(root_id)

    def fill_children(self, node: str, recno: int):
        img = self.img
        for r in img.children.get(recno, []):
            e = img.entries[r]
            size = "" if e.is_dir else human(e.size)
            nid = self.tree.insert(
                node, "end",
                text=("📁 " if e.is_dir else "   ") + e.name,
                values=(size, ft_str(e.mtime)),
            )
            self._node_recno[nid] = r
            if e.is_dir and img.children.get(r):
                self.tree.insert(nid, "end", text="...")  # expander placeholder

    def on_open_node(self, _ev):
        node = self.tree.focus()
        if node in self._loaded_dirs or node not in self._node_recno:
            return
        for c in self.tree.get_children(node):
            self.tree.delete(c)
        self.fill_children(node, self._node_recno[node])
        self._loaded_dirs.add(node)

    # ---------- search ----------

    def do_search(self):
        if not self.img:
            return
        import fnmatch

        pat = self.search_var.get().strip().lower()
        if not pat:
            return
        is_glob = any(c in pat for c in "*?[")
        self.results.delete(*self.results.get_children())
        n = 0
        for e in self.img.entries.values():
            name = e.name.lower()
            if (fnmatch.fnmatch(name, pat) if is_glob else pat in name):
                self.results.insert(
                    "", "end",
                    values=("" if e.is_dir else human(e.size), self.img.full_path(e.recno)),
                    tags=(str(e.recno),),
                )
                n += 1
                if n >= 2000:
                    break
        self.status.set(f"{n} match(es){' (capped at 2000)' if n >= 2000 else ''}")

    def reveal_result(self, _ev):
        sel = self.results.focus()
        if not sel:
            return
        path = self.results.item(sel, "values")[1]
        recno = self.img.resolve(path)
        if recno is None:
            return
        # expand tree down to it
        chain = []
        cur = recno
        while cur != self.img.root_recno and cur in self.img.entries:
            chain.append(cur)
            cur = self.img.entries[cur].parent
        node = next(iter(self._node_recno))  # root node id
        for r in reversed(chain):
            if node not in self._loaded_dirs:
                for c in self.tree.get_children(node):
                    self.tree.delete(c)
                self.fill_children(node, self._node_recno[node])
                self._loaded_dirs.add(node)
            self.tree.item(node, open=True)
            nxt = None
            for c in self.tree.get_children(node):
                if self._node_recno.get(c) == r:
                    nxt = c
                    break
            if nxt is None:
                return
            node = nxt
        self.tree.see(node)
        self.tree.selection_set(node)

    # ---------- extract ----------

    def extract_selected(self):
        if not self.img:
            return
        recnos = [
            self._node_recno[n] for n in self.tree.selection() if n in self._node_recno
        ]
        for sel in self.results.selection():
            tags = self.results.item(sel, "tags")
            if tags:
                recnos.append(int(tags[0]))
        if not recnos:
            messagebox.showinfo("PMF Viewer", "Select files or folders first (tree or search results).")
            return
        out = filedialog.askdirectory(title="Extract to folder")
        if not out:
            return
        out_dir = Path(out)
        self.status.set("Extracting...")
        self.pbar.pack(fill="x", padx=4, pady=2)
        self.pbar.start(12)

        def work():
            img = self.img
            ok = fail = 0
            for recno in recnos:
                base_e = img.entries.get(recno)
                if base_e is None:
                    continue
                base_parent = img.full_path(base_e.parent) if base_e.parent in img.entries else ""
                for r in img.walk(recno):
                    e = img.entries[r]
                    full = img.full_path(r)
                    rel = (
                        full[len(base_parent):].lstrip("\\")
                        if base_parent and full.startswith(base_parent)
                        else full
                    )
                    dest = out_dir / _safe_rel(rel)
                    if e.is_dir:
                        dest.mkdir(parents=True, exist_ok=True)
                        continue
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        tgt = dest
                        if os.name == "nt" and len(str(tgt)) > 250:
                            tgt = Path("\\\\?\\" + os.path.abspath(str(tgt)))
                        with open(tgt, "wb") as fh:
                            for chunk in img.iter_file_data(e):
                                fh.write(chunk)
                        ok += 1
                    except (PmfError, OSError):
                        fail += 1
            self.q.put(("extract_done", (ok, fail, str(out_dir))))

        threading.Thread(target=work, daemon=True).start()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    app = App(root)
    if len(sys.argv) > 1:
        app.open_image(Path(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
