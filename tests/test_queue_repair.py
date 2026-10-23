from __future__ import annotations

import gzip
import pickle
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmf_queue import JobQueue
from pmf_repair import repair_mft_index
from pmf_update import update_pmf_image
from pmfview import BLOB_HDR, PmfError, PmfImage, create_pmf_image

ROOT = Path(__file__).resolve().parents[1]


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pmf-queue-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'jobs.sqlite3'
        self.data = self.root / 'data'
        def write(arguments):
            with self.data.open('ab') as stream:
                stream.write(arguments['text'].encode())
            return self.data.read_text()
        def read(arguments):
            return self.data.read_text()
        self.operations = {'write': write, 'read': read}

    def queue(self, start=True):
        queue = JobQueue(self.database, self.operations, start=start)
        self.addCleanup(queue.close)
        return queue

    def test_pending_jobs_survive_restart_and_process_fifo(self):
        stopped = self.queue(False)
        ids = [stopped.submit('write', {'text': 'a'}), stopped.submit('read', {}),
               stopped.submit('write', {'text': 'b'}), stopped.submit('read', {})]
        self.assertTrue(all(stopped.get(i)['state'] == 'pending' for i in ids))
        stopped.close()
        running = self.queue()
        self.assertEqual(running.wait(ids[-1], 5)['result'], 'ab')
        self.assertEqual(running.get(ids[1])['result'], 'a')
        running.acknowledge(ids[0])
        self.assertEqual(running.get(ids[0])['result'], 'a')
        self.assertEqual(len(running.jobs()), 3)
        self.assertEqual(len(running.jobs(include_acknowledged=True)), 4)

    def test_failed_jobs_retained_and_explicit_retry_has_new_id(self):
        queue = self.queue()
        failed = queue.submit('read', {})
        self.assertEqual(queue.wait(failed, 5)['state'], 'failed')
        self.data.write_text('available now')
        retried = queue.retry(failed)
        self.assertGreater(retried, failed)
        self.assertEqual(queue.wait(retried, 5)['result'], 'available now')
        self.assertEqual(queue.get(failed)['state'], 'failed')

    def test_two_workers_do_not_duplicate_jobs(self):
        first = self.queue()
        second = self.queue()
        jobs = [second.submit('write', {'text': str(i)}) for i in range(10)]
        self.assertEqual(first.wait(jobs[-1], 5)['state'], 'completed')
        self.assertEqual(self.data.read_text(), '0123456789')
        first.close()
        next_job = second.submit('write', {'text': 'X'})
        self.assertEqual(second.wait(next_job, 5)['result'], '0123456789X')

    def test_crashed_running_job_not_blindly_replayed(self):
        script = self.root / 'crash.py'
        script.write_text(f'''from pathlib import Path
import os, time, sys
sys.path.insert(0, {str(ROOT)!r})
from pmf_queue import JobQueue
path = Path({str(self.data)!r})
def crash(arguments):
    with path.open('ab') as out:
        out.write(b'x')
        out.flush()
        os.fsync(out.fileno())
    os._exit(82)
q = JobQueue(Path({str(self.database)!r}), {{'write': crash, 'read': lambda a: path.read_text()}}, start=False)
q.submit('write', {{'text': 'x'}})
q.submit('read', {{}})
q.close()
q = JobQueue(Path({str(self.database)!r}), {{'write': crash, 'read': lambda a: path.read_text()}})
while True:
    time.sleep(0.1)
''', encoding='utf-8')
        result = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 82, result.stderr.decode())
        queue = self.queue()
        self.assertEqual(queue.wait(2, 5)['result'], 'x')
        self.assertEqual(queue.get(1)['state'], 'interrupted')
        self.assertIn('may have committed', queue.get(1)['error'])
        self.assertEqual(self.data.read_bytes(), b'x')


class RepairAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pmf-repair-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'docs'
        self.source.mkdir()
        (self.source / 'a.txt').write_text('original', encoding='utf-8')
        (self.source / 'last.txt').write_text('recoverable', encoding='utf-8')
        self.path = self.root / 'backup.pmf'
        create_pmf_image(self.path, [self.source])
        self.prefix = self.path.read_bytes()

    def open(self):
        image = PmfImage(self.path)
        self.addCleanup(image.close)
        image.load_index(progress=False)
        return image

    def test_corrupt_cache_rebuilt_automatically(self):
        image = self.open()
        cache = image._cache_path()
        cache.write_bytes(b'invalid index')
        fresh = self.open()
        self.assertIsNotNone(fresh.resolve('docs/a.txt'))
        self.assertEqual(self.path.read_bytes(), self.prefix)
        with gzip.open(cache, 'rb') as stream:
            payload = pickle.load(stream)
        target = next(e for e in payload['entries'].values() if e.name == 'a.txt')
        target.parent = 0x1234567
        with gzip.open(cache, 'wb') as stream:
            pickle.dump(payload, stream)
        self.assertIsNotNone(self.open().resolve('docs/a.txt'))
        self.assertEqual(self.path.read_bytes(), self.prefix)

    def test_repair_rebuilds_cache_without_image_edits(self):
        report = repair_mft_index(self.path, repair=False)
        self.assertEqual(report['status'], 'healthy')
        report = repair_mft_index(self.path, repair=True)
        self.assertEqual(report['status'], 'rebuilt')
        self.assertFalse(report['image_bytes_changed'])
        self.assertEqual(self.path.read_bytes(), self.prefix)

    def test_unreadable_metadata_blocks_writes_and_reports_recovery(self):
        image = self.open()
        entry = image.entries[image.resolve('docs/a.txt')]
        image.close()
        with self.path.open('r+b') as out:
            out.seek(entry.blob + BLOB_HDR)
            out.write(b'bad zlib')
        damaged = self.path.read_bytes()
        report = repair_mft_index(self.path, repair=True)
        self.assertEqual(report['status'], 'needs_recovery')
        self.assertFalse(report['index_rebuilt'])
        with self.assertRaisesRegex(PmfError, 'metadata needs recovery'):
            update_pmf_image(self.path, [self.source])
        self.assertEqual(self.path.read_bytes(), damaged)
        fresh = self.open()
        self.assertEqual(fresh.read_file(fresh.entries[fresh.resolve('docs/last.txt')]), b'recoverable')

    def cli(self, *arguments):
        result = subprocess.run([sys.executable, str(ROOT / 'pmfview.py'), str(self.path),
                                 *arguments], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout

    def test_full_cli_write_import_read_hash_stat_extract(self):
        self.assertIn(b'Revision 1', self.cli('write', 'docs/a.txt', 'updated', '--expected-revision', '0'))
        self.assertEqual(self.cli('cat', 'docs/a.txt'), b'updated')
        self.assertEqual(self.cli('read', 'docs/a.txt', '--offset', '1', '--length', '3'), b'pda')
        local = self.root / 'input.bin'
        local.write_bytes(bytes(range(256)))
        self.cli('import', str(local), 'docs/new.bin')
        self.assertEqual(self.cli('cat', 'docs/new.bin'), local.read_bytes())
        self.assertIn(b'sha256(', self.cli('hash', 'docs/a.txt'))
        self.assertIn(b'"revision": 2', self.cli('stat', 'docs/a.txt'))
        out = self.root / 'extracted'
        self.cli('extract', 'docs', '--out', str(out))
        self.assertEqual((out / 'docs/new.bin').read_bytes(), local.read_bytes())
        self.assertIn(b'"status": "rebuilt"', self.cli('repair-mft', '--apply'))


if __name__ == '__main__':
    unittest.main()
