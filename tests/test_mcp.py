"""MCP tests use the installed official SDK and real PMF files."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pmf_update import update_pmf_image  # noqa: E402
from pmfview import PmfError, create_pmf_image  # noqa: E402

HAS_MCP = importlib.util.find_spec('mcp') is not None


@unittest.skipUnless(HAS_MCP, 'Install mcp>=1.12,<2 for MCP integration tests')
class McpTests(unittest.TestCase):
    def setUp(self):
        import pmfview_mcp
        self.server = pmfview_mcp
        self.temp = tempfile.TemporaryDirectory(prefix='pmf-mcp-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous_queue = os.environ.get('PMF_QUEUE_DB')
        os.environ['PMF_QUEUE_DB'] = str(self.root / 'queue.sqlite3')
        self.addCleanup(self.restore_queue)
        self.addCleanup(self.server._close_queues)
        self.source = self.root / 'docs'
        self.source.mkdir()
        (self.source / 'a.txt').write_text('original', encoding='utf-8')
        self.image = self.root / 'backup.pmf'
        create_pmf_image(self.image, [self.source])
        self.path = str(self.image)
        self.addCleanup(self.server._drop, self.path)

    def restore_queue(self):
        if self.previous_queue is None:
            os.environ.pop('PMF_QUEUE_DB', None)
        else:
            os.environ['PMF_QUEUE_DB'] = self.previous_queue

    def test_write_read_import_extract_and_old_revision(self):
        m = self.server
        self.assertIn('Revision: 0', m.pmf_open(self.path))
        result = m.pmf_write(self.path, 'docs/a.txt', 'updated text', expected_revision=0)
        self.assertIn('Revision 1', result)
        self.assertIn('updated text', m.pmf_read(self.path, 'docs/a.txt'))
        self.assertIn('original', m.pmf_read(self.path, 'docs/a.txt', revision=0))
        binary = bytes(range(256))
        m.pmf_write(self.path, 'docs/binary', base64.b64encode(binary).decode(), encoding='base64')
        large = self.root / 'large'
        large.write_bytes(os.urandom(2 << 20))
        m.pmf_import(self.path, str(large), 'docs/imported')
        out = self.root / 'extract'
        self.assertIn('Extracted 3 file(s)', m.pmf_extract(self.path, ['docs'], str(out)))
        self.assertEqual((out / 'docs/a.txt').read_bytes(), b'updated text')
        self.assertEqual((out / 'docs/binary').read_bytes(), binary)
        self.assertEqual((out / 'docs/imported').read_bytes(), large.read_bytes())
        self.assertIn('Original image', m.pmf_history(self.path))
        old = self.root / 'old'
        m.pmf_extract(self.path, ['docs/a.txt'], str(old), revision=0)
        self.assertEqual((old / 'a.txt').read_bytes(), b'original')

    def test_external_update_refreshes_cached_image(self):
        m = self.server
        m.pmf_open(self.path)
        (self.source / 'a.txt').write_text('external change', encoding='utf-8')
        update_pmf_image(self.image, [self.source])
        self.assertIn('external change', m.pmf_read(self.path, 'docs/a.txt'))
        self.assertIn('Revision: 1', m.pmf_open(self.path))

    def test_conflicting_and_invalid_writes_do_not_change_image(self):
        m = self.server
        m.pmf_write(self.path, 'docs/a.txt', 'saved')
        before = self.image.read_bytes()
        with self.assertRaisesRegex(PmfError, 'Revision conflict'):
            m.pmf_write(self.path, 'docs/a.txt', 'stale', expected_revision=0)
        for arguments in ({'path': '../bad', 'content': 'x'},
                          {'path': 'docs/b', 'content': '???', 'encoding': 'base64'},
                          {'path': 'docs/b', 'content': 'x', 'encoding': 'wrong'}):
            with self.assertRaises(PmfError):
                m.pmf_write(self.path, **arguments)
        self.assertEqual(self.image.read_bytes(), before)
        with self.assertRaises(PmfError):
            m.pmf_read(self.path, 'docs/a.txt', offset=-1)

    def test_mcp_update_preview_then_commit(self):
        m = self.server
        (self.source / 'a.txt').write_text('from source', encoding='utf-8')
        before = self.image.read_bytes()
        self.assertIn('Preview', m.pmf_update(self.path, [str(self.source)]))
        self.assertEqual(self.image.read_bytes(), before)
        self.assertIn('Committed', m.pmf_update(self.path, [str(self.source)], dry_run=False))
        self.assertIn('from source', m.pmf_read(self.path, 'docs/a.txt'))

    def test_real_stdio_tool_discovery_and_round_trip(self):
        async def exercise():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(command=sys.executable,
                args=[str(ROOT / 'pmfview_mcp.py')], env=dict(os.environ))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    found = {t.name: t for t in (await session.list_tools()).tools}
                    self.assertIn('pmf_import', found)
                    self.assertTrue({'pmf_mount', 'pmf_mount_status', 'pmf_flush_mount', 'pmf_unmount'} <= found.keys())
                    self.assertTrue(found['pmf_read'].annotations.readOnlyHint)
                    self.assertFalse(found['pmf_write'].annotations.readOnlyHint)
                    result = await session.call_tool('pmf_write', {
                        'image_path': self.path, 'path': 'docs/a.txt',
                        'content': 'written through MCP', 'expected_revision': 0})
                    self.assertFalse(result.isError, result.content)
                    result = await session.call_tool('pmf_read', {
                        'image_path': self.path, 'path': 'docs/a.txt'})
                    self.assertFalse(result.isError, result.content)
                    self.assertIn('written through MCP', json.dumps([c.model_dump() for c in result.content]))
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
