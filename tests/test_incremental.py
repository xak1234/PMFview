"""Real temporary images and subprocesses; no mocked PMF or filesystem operations."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmf_update import FOOTER, put_pmf_file, update_pmf_image
from pmfview import BLOB_HDR, PmfError, PmfImage, create_pmf_image, extract_file

ROOT = Path(__file__).resolve().parents[1]


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pmf-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'docs'
        self.source.mkdir()
        (self.source / 'a.txt').write_bytes(b'original')
        (self.source / 'delete.txt').write_bytes(b'remove me')
        self.other = self.root / 'other'
        self.other.mkdir()
        (self.other / 'keep.txt').write_bytes(b'untouched')
        self.path = self.root / 'backup.pmf'
        create_pmf_image(self.path, [self.source, self.other])
        self.prefix = self.path.read_bytes()

    def image(self, revision=None):
        image = PmfImage(self.path, revision=revision)
        self.addCleanup(image.close)
        image.load_index(progress=False)
        return image

    def contents(self, path='docs/a.txt', revision=None):
        image = self.image(revision)
        return image.read_file(image.entries[image.resolve(path)])

    def update(self, **kwargs):
        return update_pmf_image(self.path, [self.source], **kwargs)

    def test_add_modify_delete_and_history(self):
        (self.source / 'a.txt').write_bytes(b'updated')
        (self.source / 'delete.txt').unlink()
        (self.source / 'new.txt').write_bytes(b'new')
        result = self.update()
        self.assertEqual(result.revision, 1)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.deleted, 1)
        self.assertTrue(self.path.read_bytes().startswith(self.prefix))
        self.assertEqual(self.contents(), b'updated')
        self.assertIsNone(self.image().resolve('docs/delete.txt'))
        self.assertEqual(self.contents(revision=0), b'original')
        self.assertEqual(self.contents('docs/delete.txt', revision=0), b'remove me')
        self.assertEqual(self.contents('other/keep.txt'), b'untouched')
        (self.source / 'a.txt').write_bytes(b'third')
        self.assertEqual(self.update().revision, 2)
        self.assertEqual(self.contents(revision=1), b'updated')
        self.assertEqual(self.contents(), b'third')

    def test_only_changed_content_appended(self):
        large = self.source / 'large.bin'
        large.write_bytes(os.urandom(3 << 20))
        create_pmf_image(self.path, [self.source, self.other], overwrite=True)
        before = self.path.read_bytes()
        old = self.image()
        blob = old.entries[old.resolve('docs/large.bin')].blob
        (self.source / 'a.txt').write_bytes(b'changed')
        result = self.update()
        self.assertLess(result.written, 16 << 10)
        self.assertTrue(self.path.read_bytes().startswith(before))
        current = self.image()
        self.assertEqual(current.entries[current.resolve('docs/large.bin')].blob, blob)
        self.assertEqual(self.contents('docs/large.bin'), large.read_bytes())

    def test_noop_and_preview_do_not_grow_image(self):
        self.assertEqual(self.update().written, 0)
        self.assertEqual(self.path.read_bytes(), self.prefix)
        (self.source / 'a.txt').write_bytes(b'changed')
        self.assertTrue(self.update(dry_run=True).dry_run)
        self.assertEqual(self.path.read_bytes(), self.prefix)
        self.update()
        before = self.path.read_bytes()
        self.assertEqual(self.update().written, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_same_size_and_mtime_change_is_found(self):
        source = self.source / 'a.txt'
        st = source.stat()
        source.write_bytes(b'changed!')
        os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.update()
        self.assertEqual(self.contents(), b'changed!')

    def test_metadata_only_change_reuses_blob(self):
        old = self.image()
        entry = old.entries[old.resolve('docs/a.txt')]
        source = self.source / 'a.txt'
        st = source.stat()
        os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 4_000_000_000))
        self.update()
        new = self.image()
        self.assertEqual(new.entries[new.resolve('docs/a.txt')].blob, entry.blob)
        self.assertEqual(self.contents(), b'original')

    def test_empty_files_directories_and_type_changes(self):
        (self.source / 'empty').mkdir()
        (self.source / 'zero').write_bytes(b'')
        (self.source / 'a.txt').unlink()
        (self.source / 'a.txt').mkdir()
        (self.source / 'a.txt' / 'child').write_bytes(b'child')
        self.update()
        self.assertEqual(self.contents('docs/zero'), b'')
        self.assertEqual(self.contents('docs/a.txt/child'), b'child')
        shutil.rmtree(self.source / 'a.txt')
        (self.source / 'a.txt').write_bytes(b'file again')
        self.update()
        self.assertEqual(self.contents(), b'file again')
        self.assertIsNone(self.image().resolve('docs/a.txt/child'))

    def test_root_mapping_and_non_deleting_update(self):
        (self.source / 'delete.txt').unlink()
        self.update(delete_missing=False)
        self.assertEqual(self.contents('docs/delete.txt'), b'remove me')
        root = self.root / 'all'
        shutil.copytree(self.source, root / 'docs')
        shutil.copytree(self.other, root / 'other')
        (root / 'docs' / 'a.txt').write_bytes(b'root mapping')
        update_pmf_image(self.path, [root], image_root=True)
        self.assertEqual(self.contents(), b'root mapping')
        self.assertIsNone(self.image().resolve('docs/delete.txt'))

    def test_upsert_preserves_other_files_and_noop(self):
        new = self.root / 'input'
        new.write_bytes(b'from import')
        result = put_pmf_file(self.path, 'docs/a.txt', new, expected_revision=0)
        self.assertEqual(result.revision, 1)
        self.assertEqual(self.contents(), b'from import')
        self.assertEqual(self.contents('docs/delete.txt'), b'remove me')
        self.assertEqual(self.contents(revision=0), b'original')
        self.assertEqual(put_pmf_file(self.path, 'docs/a.txt', new).written, 0)
        put_pmf_file(self.path, 'new.bin', new)
        self.assertEqual(self.contents('new.bin'), b'from import')
        with self.assertRaisesRegex(PmfError, 'Revision conflict'):
            put_pmf_file(self.path, 'docs/a.txt', new, expected_revision=0)

    def test_bad_paths_sources_and_locks_fail_without_writes(self):
        new = self.root / 'input'
        new.write_bytes(b'test')
        for path in ('../oops', 'docs/../../oops', 'docs/bad:name', 'missing/a', 'docs/a.txt/a', 'docs'):
            with self.subTest(path=path), self.assertRaises(PmfError):
                put_pmf_file(self.path, path, new)
        with self.assertRaises(PmfError):
            update_pmf_image(self.path, [self.root], image_root=True)
        missing = self.root / 'missing'
        with self.assertRaises(OSError):
            update_pmf_image(self.path, [missing])
        missing.mkdir()
        with self.assertRaisesRegex(PmfError, 'does not match'):
            update_pmf_image(self.path, [missing])
        lock = self.path.with_name(self.path.name + '.update.lock')
        lock.write_text('owned by another process')
        with self.assertRaisesRegex(PmfError, 'lock exists'):
            self.update()
        self.assertEqual(lock.read_text(), 'owned by another process')
        self.assertEqual(self.path.read_bytes(), self.prefix)

    def test_link_source_refused(self):
        try:
            (self.source / 'link').symlink_to(self.source / 'a.txt')
        except OSError as exc:
            self.skipTest(f'Symlink creation unavailable: {exc}')
        with self.assertRaisesRegex(PmfError, 'Link/reparse'):
            self.update()
        self.assertEqual(self.path.read_bytes(), self.prefix)

    def test_source_change_cancels_before_commit(self):
        (self.source / 'a.txt').write_bytes(b'new')
        def change_source(message):
            if message.startswith('Checking source inventory'):
                (self.source / 'a.txt').write_bytes(b'changed during backup')
        with self.assertRaisesRegex(PmfError, 'Source inventory changed'):
            self.update(progress=change_source)
        image = self.image()
        self.assertEqual(image.revision, 0)
        self.assertFalse(image.incremental_warning)
        self.assertEqual(self.contents(), b'original')
        self.assertTrue(self.path.read_bytes().startswith(self.prefix))
        self.assertEqual(self.update().revision, 1)

    def test_caught_interrupt_rolls_back(self):
        (self.source / 'a.txt').write_bytes(b'new')
        def interrupt(message):
            if message.startswith('Committing verified'):
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.update(progress=interrupt)
        self.assertEqual(self.contents(), b'original')
        self.assertFalse(self.image().incremental_warning)
        self.assertEqual(self.update().revision, 1)

    def test_hard_crash_keeps_last_commit_readable_without_cache_or_lock(self):
        (self.source / 'a.txt').write_bytes(b'committed')
        self.update()
        (self.source / 'a.txt').write_bytes(b'never committed')
        script = ("from pathlib import Path; import os; from pmf_update import update_pmf_image; "
                  f"update_pmf_image(Path({str(self.path)!r}), [Path({str(self.source)!r})], "
                  "progress=lambda m: os._exit(71) if m.startswith('Verifying appended') else None)")
        child = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True)
        self.assertEqual(child.returncode, 71, child.stderr.decode())
        self.assertEqual(self.contents(), b'committed')
        self.assertTrue(self.image().incremental_warning)
        self.path.with_name(self.path.name + '.update.lock').unlink()
        self.path.with_suffix('.pmf.pmfindex').unlink(missing_ok=True)
        self.assertEqual(self.contents(), b'committed')
        with self.assertRaisesRegex(PmfError, 'incomplete tail'):
            self.update()

    def test_manifest_corruption_detected_even_with_cache(self):
        (self.source / 'a.txt').write_bytes(b'new')
        self.update()
        self.image()
        with self.path.open('r+b') as out:
            out.seek(-FOOTER.size, os.SEEK_END)
            footer = FOOTER.unpack(out.read(FOOTER.size))
            out.seek(footer[1])
            value = out.read(1)
            out.seek(footer[1])
            out.write(bytes([value[0] ^ 1]))
        with self.assertRaisesRegex(PmfError, 'manifest checksum'):
            self.image()

    def test_corrupt_content_does_not_replace_extraction_target(self):
        (self.source / 'a.txt').write_bytes(os.urandom(4096))
        self.update()
        image = self.image()
        entry = image.entries[image.resolve('docs/a.txt')]
        header = image._blob_header(entry.blob)
        image.close()
        with self.path.open('r+b') as out:
            pos = entry.blob + BLOB_HDR + header[2] + 8 + 10
            out.seek(pos)
            byte = out.read(1)
            out.seek(pos)
            out.write(bytes([byte[0] ^ 1]))
        image = self.image()
        entry = image.entries[image.resolve('docs/a.txt')]
        target = self.root / 'restored.txt'
        target.write_bytes(b'keep destination')
        with self.assertRaisesRegex(PmfError, 'checksum|chunk chain'):
            extract_file(image, entry, target)
        self.assertEqual(target.read_bytes(), b'keep destination')
        self.assertFalse(list(self.root.glob('.pmf-extract-*')))

    def test_extract_latest_and_original(self):
        (self.source / 'a.txt').write_bytes(b'new')
        self.update()
        for revision, data in ((0, b'original'), (1, b'new')):
            image = self.image(revision)
            target = self.root / f'restored-{revision}'
            extract_file(image, image.entries[image.resolve('docs/a.txt')], target)
            self.assertEqual(hashlib.sha256(target.read_bytes()).digest(), hashlib.sha256(data).digest())

    def test_extract_deep_path(self):
        from pmfview import _extended_path
        image = self.image()
        entry = image.entries[image.resolve('docs/a.txt')]
        folder = self.root / 'deep'
        self.addCleanup(lambda: shutil.rmtree(_extended_path(folder), ignore_errors=True))
        target = folder / ('a' * 100) / ('b' * 100) / ('c' * 100) / 'restored.txt'
        extract_file(image, entry, target)
        self.assertEqual(_extended_path(target).read_bytes(), b'original')

    def test_cli_history_and_old_revision(self):
        (self.source / 'a.txt').write_bytes(b'new')
        result = subprocess.run([sys.executable, str(ROOT / 'pmfview.py'), str(self.path),
                                 'update', str(self.source)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b'Revision 1', result.stdout)
        history = subprocess.run([sys.executable, str(ROOT / 'pmfview.py'), str(self.path),
                                  'history'], capture_output=True)
        self.assertEqual(history.returncode, 0, history.stderr.decode())
        self.assertIn(b'Original image', history.stdout)
        old = subprocess.run([sys.executable, str(ROOT / 'pmfview.py'), str(self.path),
                              '--revision', '0', 'cat', 'docs/a.txt'], capture_output=True)
        self.assertEqual(old.returncode, 0, old.stderr.decode())
        self.assertEqual(old.stdout, b'original')


if __name__ == '__main__':
    unittest.main()
