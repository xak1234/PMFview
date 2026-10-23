"""Real drive-letter integration tests; no mocked Windows filesystem calls."""
import importlib.util
import os
from pathlib import Path
import shutil
import json
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmf_mount import control_mount, start_mount
from pmfview import PmfImage, create_pmf_image


@unittest.skipUnless(os.name == 'nt' and importlib.util.find_spec('winfspy'), 'Windows + WinFsp required')
class WindowsMountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pmf-mount-real-')
        self.root = Path(self.temp.name)
        self.previous_env = os.environ.get('PMF_MOUNT_DIR')
        os.environ['PMF_MOUNT_DIR'] = str(self.root / 'sessions')
        self.drive = next((f'{letter}:' for letter in 'ZYXWVUTSRQP' if not Path(f'{letter}:\\').exists()), None)
        if self.drive is None:
            self.skipTest('No free test drive')
        docs = self.root / 'docs'
        docs.mkdir()
        (docs / 'a.txt').write_bytes(b'original')
        (docs / 'keep.txt').write_bytes(b'keep')
        self.image = self.root / 'test.pmf'
        create_pmf_image(self.image, [docs])
        self.prefix = self.image.read_bytes()
        self.mounted = False
        self.addCleanup(self.cleanup_mount)

    def cleanup_mount(self):
        if self.mounted:
            self.unmount()
        if self.previous_env is None:
            os.environ.pop('PMF_MOUNT_DIR', None)
        else:
            os.environ['PMF_MOUNT_DIR'] = self.previous_env
        self.temp.cleanup()

    def mount(self, readonly=False, revision=None):
        result = start_mount(self.image, self.drive, read_only=readonly, revision=revision)
        self.assertEqual(result['state'], 'mounted', result)
        self.mounted = True
        return Path(self.drive + '\\')

    def unmount(self):
        deadline = time.monotonic() + 5
        while True:
            try:
                control_mount(self.drive, 'unmount')
                self.mounted = False
                return
            except Exception as exc:
                if 'Files are still open' not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def test_cli_and_queued_mcp_mount_controls(self):
        cli = Path(__file__).resolve().parents[1] / 'pmfview.py'
        result = subprocess.run([sys.executable, str(cli), str(self.image), 'mount', self.drive],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)['state'], 'mounted')
        self.mounted = True
        drive = Path(self.drive + '\\')
        self.assertEqual((drive / 'docs' / 'a.txt').read_bytes(), b'original')
        result = subprocess.run([sys.executable, str(cli), str(self.image), 'unmount', self.drive],
                                capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.mounted = False
        import pmfview_mcp as m
        old_queue = os.environ.get('PMF_QUEUE_DB')
        os.environ['PMF_QUEUE_DB'] = str(self.root / 'queue.sqlite3')
        try:
            result = m.pmf_mount(str(self.image), self.drive, read_only=False)
            self.assertIn('completed', result)
            self.mounted = True
            (drive / 'created.txt').write_text('MCP mount', encoding='utf-8')
            self.assertIn('mounted', m.pmf_mount_status(str(self.image)))
            self.assertIn('revision', m.pmf_flush_mount(str(self.image), self.drive))
            self.assertIn('unmounted', m.pmf_unmount(str(self.image), self.drive))
            self.mounted = False
            self.assertIn('MCP mount', m.pmf_read(str(self.image), 'created.txt'))
            self.assertEqual(len(m._queue().jobs()), 5)
        finally:
            m._close_queues()
            if old_queue is None:
                os.environ.pop('PMF_QUEUE_DB', None)
            else:
                os.environ['PMF_QUEUE_DB'] = old_queue

    def test_real_read_write_rename_delete_and_reopen(self):
        drive = self.mount()
        self.assertEqual((drive / 'DOCS' / 'A.TXT').read_bytes(), b'original')
        with (drive / 'docs' / 'a.txt').open('r+b') as stream:
            stream.seek(2)
            stream.write(b'XYZ')
            stream.truncate(6)
            stream.flush()
            os.fsync(stream.fileno())
        self.assertEqual((drive / 'docs' / 'a.txt').read_bytes(), b'orXYZn')
        (drive / 'new').mkdir()
        (drive / 'new' / 'file.txt').write_bytes(b'created through Windows')
        (drive / 'new' / 'file.txt').rename(drive / 'new' / 'renamed.txt')
        (drive / 'new' / 'renamed.txt').replace(drive / 'docs' / 'a.txt')
        (drive / 'new').rmdir()
        (drive / 'docs' / 'keep.txt').unlink()
        binary = self.root / 'binary'
        binary.write_bytes(os.urandom((2 << 20) + 37))
        shutil.copyfile(binary, drive / 'binary.bin')
        self.assertEqual((drive / 'binary.bin').read_bytes(), binary.read_bytes())
        with (drive / 'sparse.bin').open('wb') as stream:
            stream.seek(8192)
            stream.write(b'end')
        self.assertEqual((drive / 'sparse.bin').read_bytes(), bytes(8192) + b'end')
        with (drive / 'binary.bin').open('rb'):
            with self.assertRaisesRegex(Exception, 'Files are still open'):
                control_mount(self.drive, 'unmount')
        result = control_mount(self.drive, 'flush')
        self.assertGreater(result['revision'], 0)
        self.unmount()
        self.assertFalse(drive.exists())
        self.assertTrue(self.image.read_bytes().startswith(self.prefix))
        image = PmfImage(self.image)
        try:
            image.load_index(progress=False)
            self.assertEqual(image.read_file(image.entries[image.resolve('docs/a.txt')]), b'created through Windows')
            self.assertIsNone(image.resolve('docs/keep.txt'))
        finally:
            image.close()
        drive = self.mount(readonly=True, revision=0)
        self.assertEqual((drive / 'docs' / 'a.txt').read_bytes(), b'original')
        self.assertEqual((drive / 'docs' / 'keep.txt').read_bytes(), b'keep')
        with self.assertRaises(OSError):
            (drive / 'forbidden').write_bytes(b'no')
        self.unmount()


if __name__ == '__main__':
    unittest.main(verbosity=2)
