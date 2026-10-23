"""Exercise real PMF images and disk-backed mount transactions without WinFsp."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmf_mount_store import MountStore
from pmf_update import write_pmf_file
from pmfview import PmfImage, PmfError, create_pmf_image


class MountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        docs = self.root / 'docs'
        docs.mkdir()
        (docs / 'a.txt').write_bytes(b'original')
        (docs / 'keep.txt').write_bytes(b'keep')
        self.image = self.root / 'image.pmf'
        create_pmf_image(self.image, [docs])
        self.prefix = self.image.read_bytes()

    def mount(self, readonly=False, revision=None):
        store = MountStore(self.image, self.root / 'workspace', read_only=readonly, revision=revision)
        self.addCleanup(store.release)
        return store

    def contents(self, path, revision=None):
        image = PmfImage(self.image, revision=revision)
        try:
            image.load_index(progress=False)
            recno = image.resolve(path)
            return image.read_file(image.entries[recno]) if recno else None
        finally:
            image.close()

    def test_random_write_resize_flush_rename_delete_history(self):
        store = self.mount()
        node = store.lookup('\\DOCS\\A.TXT')
        self.assertEqual(store.read(node, 2, 3), b'igi')
        store.write(node, b'XYZ', 2)
        store.resize(node, 6)
        self.assertEqual(store.flush(), 1)
        self.assertEqual(self.contents('docs/a.txt'), b'orXYZn')
        folder = store.create('\\new', True)
        child = store.create('\\new\\temp', False)
        store.write(child, b'new content', 0)
        store.rename(child, '\\docs\\a.txt', True)
        store.delete(folder)
        self.assertEqual(store.flush(), 2)
        self.assertEqual(self.contents('docs/a.txt'), b'new content')
        self.assertEqual(self.contents('docs/keep.txt'), b'keep')
        self.assertEqual(self.contents('docs/a.txt', 0), b'original')
        self.assertEqual(self.contents('docs/a.txt', 1), b'orXYZn')
        self.assertTrue(self.image.read_bytes().startswith(self.prefix))
        self.assertEqual(json.loads((store.workspace / 'pending.json').read_text())['entries'], [])
        store.finish()
        self.assertFalse(self.image.with_suffix('.pmf.update.lock').exists())

    def test_readonly_and_exclusive_writer(self):
        store = self.mount()
        with self.assertRaisesRegex(PmfError, 'Update lock'):
            write_pmf_file(self.image, 'docs/a.txt', 'competing writer')
        store.finish()
        store = self.mount(readonly=True)
        with self.assertRaises(PermissionError):
            store.create('file', False)
        self.assertEqual(store.read(store.lookup('docs/a.txt'), 0, 8), b'original')
        store.finish()
        self.assertEqual(self.image.read_bytes(), self.prefix)

    def test_crash_workspace_and_external_change_refusal(self):
        store = self.mount()
        node = store.lookup('docs/a.txt')
        store.write(node, b'pending!', 0)
        pending = json.loads((store.workspace / 'pending.json').read_text())
        self.assertEqual(Path(pending['entries'][0]['data']).read_bytes(), b'pending!')
        with self.image.open('ab') as stream:
            stream.write(b'external change')
        before = self.image.read_bytes()
        with self.assertRaisesRegex(PmfError, 'Image changed'):
            store.flush()
        self.assertEqual(self.image.read_bytes(), before)
        store.release()
        self.assertTrue(node.cache.exists())

    def test_directory_rename_and_invalid_moves(self):
        store = self.mount()
        docs = store.lookup('docs')
        store.create('docs/sub', True)
        with self.assertRaises(PermissionError):
            store.rename(docs, 'docs/sub/recursive', False)
        with self.assertRaises(OSError):
            store.delete(docs)
        store.rename(docs, 'renamed', False)
        store.flush()
        self.assertEqual(self.contents('renamed/a.txt'), b'original')
        self.assertIsNone(self.contents('docs/a.txt'))
        store.finish()


if __name__ == '__main__':
    unittest.main()
