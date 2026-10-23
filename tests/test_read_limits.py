"""Real PMF files, SQLite records and stdio sessions; no mocked file operations."""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pmfview import (  # noqa: E402
    CATALOG_START, CHUNK_MAX, ENTRY_SIZE, CreateEntry, PmfError,
    PmfImage, _make_header, _make_metadata, create_pmf_image, unix_to_ft,
)
from pmf_update import write_pmf_file  # noqa: E402


class MeasuredFile(io.FileIO):
    """Count bytes read from a real file, delegating all I/O to FileIO."""
    def __init__(self, path: Path):
        super().__init__(path, 'rb')
        self.bytes_read = 0
        self.largest_read = 0

    def read(self, size: int | None = -1) -> bytes:
        data = super().read(size)
        assert data is not None
        self.bytes_read += len(data)
        self.largest_read = max(self.largest_read, len(data))
        return data


def create_sparse_image(path: Path, size: int) -> tuple[int, bytes]:
    """Write a valid raw-chunk PMF; holes consume no drive-sized allocation."""
    path.touch()
    if os.name == 'nt':
        result = subprocess.run(['fsutil', 'sparse', 'setflag', str(path)],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise unittest.SkipTest('Filesystem does not permit sparse test images')
    ft = unix_to_ft(time.time())
    entry = CreateEntry(6, 5, None, 'large.bin', size, 0, ft, ft)
    entry.blob = CATALOG_START + 80
    metadata = _make_metadata(entry)
    compressed = zlib.compress(metadata)
    marker = b'ONLY-THE-REQUESTED-RANGE'
    requested_offset = size - len(marker)
    with path.open('r+b') as stream:
        stream.write(_make_header(path, 'Range test'))
        row = bytearray(ENTRY_SIZE)
        for offset, value in ((0, 6), (8, ft), (16, ft), (24, size),
                              (32, entry.blob), (44, ft)):
            struct.pack_into('<Q', row, offset, value)
        struct.pack_into('<I', row, 52, 6)
        stream.write(row)
        stream.seek(entry.blob)
        stream.write(struct.pack('<IIBIII', 6, 0, 1, len(metadata), len(metadata), len(compressed)))
        stream.write(compressed)
        written = 0
        while written < size:
            chunk = min(CHUNK_MAX, size - written)
            stream.write(struct.pack('<II', chunk, chunk))
            start = stream.tell()
            if written <= requested_offset < written + chunk:
                stream.seek(start + requested_offset - written)
                stream.write(marker)
            stream.seek(start + chunk)
            written += chunk
        stream.truncate()
    return requested_offset, marker


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pmf-range-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def image(self, path: Path) -> PmfImage:
        image = PmfImage(path)
        self.addCleanup(image.close)
        image.load_index(progress=False)
        return image

    def test_200_gib_image_reads_only_headers_and_requested_payload(self):
        path = self.root / 'sparse-200gib.pmf'
        offset, expected = create_sparse_image(path, 200 << 30)
        image = self.image(path)
        entry = image.entries[6]
        image.f.close()
        measured = MeasuredFile(path)
        image.f = measured
        self.assertEqual(image.read_file_range(entry, offset, len(expected)), expected)
        self.assertLess(measured.bytes_read, 16384)
        self.assertLessEqual(measured.largest_read, 65536)
        previous = measured.bytes_read
        self.assertEqual(image.read_file_range(entry, entry.size + 1, 4096), b'')
        self.assertEqual(measured.bytes_read, previous)

    def test_compressed_and_raw_cross_chunk_ranges_and_revision_checksum(self):
        source = self.root / 'data.bin'
        content = b'A' * (1 << 20) + os.urandom(1 << 20) + b'last page'
        source.write_bytes(content)
        path = self.root / 'range.pmf'
        create_pmf_image(path, [source])
        image = self.image(path)
        entry = image.entries[6]
        for offset, length in ((0, 1), ((1 << 20) - 3, 20),
                               ((2 << 20) - 3, 100), (len(content), 10)):
            self.assertEqual(image.read_file_range(entry, offset, length), content[offset:offset + length])
        with self.assertRaises(PmfError):
            image.read_file_range(entry, -1, 10)
        image.close()
        write_pmf_file(path, 'data.bin', 'small replacement')
        changed = self.image(path)
        self.assertEqual(changed.read_file_range(changed.entries[6], 0, 4096), b'small replacement')
        original = PmfImage(path, revision=0)
        self.addCleanup(original.close)
        original.load_index(progress=False)
        self.assertEqual(original.read_file_range(original.entries[6], (1 << 20) - 3, 20),
                         content[(1 << 20) - 3:(1 << 20) + 17])


class McpLimitTests(unittest.TestCase):
    def setUp(self):
        import pmfview_mcp
        self.server = pmfview_mcp
        self.temp = tempfile.TemporaryDirectory(prefix='pmf-exposure-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous = os.environ.get('PMF_QUEUE_DB')
        os.environ['PMF_QUEUE_DB'] = str(self.root / 'queue.sqlite3')
        self.addCleanup(self.restore)
        self.addCleanup(self.server._close_queues)
        source = self.root / 'docs'
        source.mkdir()
        for index in range(205):
            (source / f'file-{index:03}.txt').write_text(f'PRIVATE-CONTENT-{index}', encoding='utf-8')
        (source / 'text').write_bytes(b'A' * 4096 + b'B' * 4096 + b'X' * 65536)
        (source / 'binary').write_bytes(bytes(range(256)) * 200)
        self.path = self.root / 'image.pmf'
        create_pmf_image(self.path, [source])

    def restore(self):
        if self.previous is None:
            os.environ.pop('PMF_QUEUE_DB', None)
        else:
            os.environ['PMF_QUEUE_DB'] = self.previous

    def test_preview_and_binary_continuations_and_strict_limits(self):
        m, path = self.server, str(self.path)
        result = m.pmf_read(path, 'docs/text')
        self.assertIn('returned_bytes=4096; next_offset=4096', result)
        self.assertEqual(result.count('A'), 4096)
        self.assertNotIn('BBBB', result)
        binary = m.pmf_read(path, 'docs/binary', length=32768)
        self.assertIn('returned_bytes=4096; next_offset=4096', binary)
        self.assertLess(len(binary.encode()), m.MAX_RESPONSE_BYTES)
        self.assertIn('returned_bytes=0; next_offset=none', m.pmf_read(path, 'docs/text', offset=10**10))
        for length in (-1, 0, 32769, 200 << 30):
            with self.assertRaises(PmfError):
                m.pmf_read(path, 'docs/text', length=length)

    def test_listing_pages_do_not_expose_content_or_omit_entries(self):
        m, path = self.server, str(self.path)
        result = m.pmf_ls(path, 'docs')
        self.assertIn('returned=50;', result)
        self.assertNotIn('PRIVATE-CONTENT', result)
        found, offset = [], 0
        while True:
            page = m.pmf_find(path, 'file-', limit=37, offset=offset, revision=0)
            found.extend(re.findall(r'file-\d{3}\.txt', page))
            cursor = re.search(r'next_offset=(none|\d+)', page)
            assert cursor is not None
            if cursor[1] == 'none':
                break
            offset = int(cursor[1])
        self.assertEqual(len(found), 205)
        self.assertEqual(len(set(found)), 205)
        self.assertIn('returned=50;', m.pmf_tree(path, 'docs'))
        for call, args in ((m.pmf_ls, {'limit': -1}), (m.pmf_ls, {'limit': 10**9}),
                           (m.pmf_find, {'pattern': '*', 'limit': 201}),
                           (m.pmf_tree, {'depth': 1000}), (m.pmf_tree, {'max_lines': 201}),
                           (m.pmf_history, {'limit': 101})):
            with self.assertRaises(PmfError):
                call(path, **args)

    def test_legacy_job_result_requires_explicit_bounded_page(self):
        m = self.server
        m.pmf_read(str(self.path), 'docs/text', length=1)
        queue = m._queue()
        job_id = queue.jobs()[0]['id']
        # A real old-style persisted result, including Unicode and embedded NUL.
        saved = 'PRIVATE-CONTENT-' + '\u00e9\x00\U0001f600' * 300000
        with queue._db() as db:
            db.execute('UPDATE jobs SET result=? WHERE id=?', (saved, job_id))
        status = json.loads(m.pmf_job(job_id))
        self.assertIsNone(status['result'])
        self.assertNotIn('PRIVATE-CONTENT', m.pmf_job(job_id))
        first = json.loads(m.pmf_job(job_id, include_result=True, result_length=4096))
        self.assertLessEqual(len(first['result'].encode()), 4096)
        next_page = json.loads(m.pmf_job(job_id, include_result=True,
                                       result_offset=first['next_result_offset']))
        self.assertTrue(saved.startswith(first['result'] + next_page['result']))
        self.assertEqual(first['result_bytes'], len(saved.encode()))
        self.assertNotIn('arguments', status)
        with self.assertRaises(PmfError):
            m.pmf_job(job_id, include_result=True, result_length=8193)
        self.assertLess(len(m.pmf_jobs().encode()), m.MAX_RESPONSE_BYTES)

    def test_stdio_content_is_bounded_and_not_duplicated(self):
        huge = self.root / 'large-stdio.pmf'
        large_offset, marker = create_sparse_image(huge, 200 << 30)
        async def exercise():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            packaged = os.environ.get('PMF_LIMIT_TEST_EXE')
            parameters = StdioServerParameters(command=packaged or sys.executable,
                args=['--mcp'] if packaged else [str(ROOT / 'pmfview_mcp.py')], env=dict(os.environ))
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool('pmf_read', {
                        'image_path': str(self.path), 'path': 'docs/text'})
                    self.assertFalse(result.isError, result.content)
                    self.assertIsNone(result.structuredContent)
                    self.assertEqual(len(result.content), 1)
                    self.assertLess(len(result.model_dump_json().encode()), 8192)
                    self.assertIn('next_offset=4096', result.model_dump_json())
                    rejected = await session.call_tool('pmf_read', {
                        'image_path': str(self.path), 'path': 'docs/text', 'length': 200 << 30})
                    self.assertTrue(rejected.isError)
                    self.assertNotIn('AAAA', rejected.model_dump_json())
                    large = await session.call_tool('pmf_read', {
                        'image_path': str(huge), 'path': 'large.bin',
                        'offset': large_offset, 'length': len(marker)})
                    self.assertFalse(large.isError, large.content)
                    self.assertIsNone(large.structuredContent)
                    self.assertIn(marker.decode(), large.model_dump_json())
                    self.assertLess(len(large.model_dump_json().encode()), 2048)
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main(verbosity=2)
