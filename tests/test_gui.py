from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmf_update import update_pmf_image
from pmfview import create_pmf_image


@unittest.skipUnless(sys.platform == 'win32' or os.environ.get('DISPLAY'), 'Requires a GUI display')
class GuiTests(unittest.TestCase):
    def test_latest_old_revision_and_incremental_dialog(self):
        import tkinter as tk
        from pmfview_gui import App, CreateImageWindow
        with tempfile.TemporaryDirectory(prefix='pmf-gui-test-') as directory:
            folder = Path(directory)
            source = folder / 'docs'
            source.mkdir()
            (source / 'a.txt').write_text('original')
            path = folder / 'backup.pmf'
            create_pmf_image(path, [source])
            (source / 'a.txt').write_text('updated')
            update_pmf_image(path, [source])
            root = tk.Tk()
            root.withdraw()
            app = App(root)
            try:
                def finish_load():
                    deadline = time.monotonic() + 10
                    while app._busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.01)
                    self.assertFalse(app._busy)
                    self.assertIsNotNone(app.img)
                app.open_image(path)
                finish_load()
                self.assertEqual(app.img.revision, 1)
                self.assertNotIn('disabled', app.btn_update.state())
                app.open_image(path, revision=0)
                finish_load()
                self.assertEqual(app.img.revision, 0)
                self.assertIn('disabled', app.btn_update.state())
                app.refresh_latest()
                finish_load()
                self.assertEqual(app.img.revision, 1)
                selected = []
                dialog = CreateImageWindow(root, lambda *args: selected.append(args), image_path=path)
                dialog.withdraw()
                dialog.sources = [source]
                dialog.preview.set(True)
                dialog.hot_image.set(False)
                dialog.folder_tree.selection_remove(*dialog.folder_tree.selection())
                dialog._create()
                self.assertEqual(selected[0], (path, [source], False, False, True, True))
                app.versions_dialog()
                root.update()
                versions = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
                self.assertEqual(len(versions), 1)
                versions[0].destroy()
                app.mount_dialog()
                root.update()
                from pmf_mount_gui import MountWindow
                mounts = [w for w in root.winfo_children() if isinstance(w, MountWindow)]
                self.assertEqual(len(mounts), 1)
                self.assertFalse(mounts[0].read_write.get())
                self.assertEqual(mounts[0].image, path)
                mounts[0].destroy()
            finally:
                if app.img is not None:
                    app.img.close()
                root.destroy()


if __name__ == '__main__':
    unittest.main()
