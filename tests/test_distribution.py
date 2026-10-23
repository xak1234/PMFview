"""Run with PMF_DISTRIBUTION pointing at a built folder. Uses real EXEs and drives."""
from __future__ import annotations
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == 'nt' and os.environ.get('PMF_DISTRIBUTION'), 'Requires portable Windows build')
class DistributionTests(unittest.TestCase):
    def test_packaged_cli_mount_gui_and_mcp(self):
        folder = Path(os.environ['PMF_DISTRIBUTION'])
        cli, gui = folder / 'pmf.exe', folder / 'PMFViewer.exe'
        with tempfile.TemporaryDirectory(prefix='pmf-portable-test-') as directory:
            root = Path(directory)
            source = root / 'docs'
            source.mkdir()
            (source / 'a.txt').write_text('original', encoding='utf-8')
            image = root / 'image.pmf'
            env = dict(os.environ, PMF_MOUNT_DIR=str(root / 'mounts'), PMF_QUEUE_DB=str(root / 'queue.sqlite3'))
            def run(*args):
                result = subprocess.run([str(cli), str(image), *args], env=env, cwd=root,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout
            run('create', str(source))
            self.assertIn('original', run('read', 'docs/a.txt'))
            drive = next(f'{letter}:' for letter in 'ZYXWVUTSRQP' if not Path(f'{letter}:\\').exists())
            self.assertEqual(json.loads(run('mount', drive, '--read-write'))['state'], 'mounted')
            try:
                mounted = Path(drive + '\\')
                (mounted / 'docs' / 'a.txt').write_text('portable save', encoding='utf-8')
                (mounted / 'folder').mkdir()
                (mounted / 'folder' / 'new.txt').write_bytes(b'new')
                (mounted / 'folder' / 'new.txt').rename(mounted / 'folder' / 'renamed.txt')
                self.assertGreater(json.loads(run('flush-mount', drive))['revision'], 0)
            finally:
                run('unmount', drive)
            self.assertIn('portable save', run('read', 'docs/a.txt'))
            self.assertIn('original', run('--revision', '0', 'read', 'docs/a.txt'))
            self.assertEqual(run('cat', 'folder/renamed.txt'), 'new')
            # Optional driver failure must not prevent ordinary browsing/extraction.
            unavailable = dict(env, WINFSP_LIBRARY_PATH=str(root / 'not-installed'))
            result = subprocess.run([str(cli), str(image), 'info'], env=unavailable, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            process = subprocess.Popen([str(gui), str(image)], env=env, cwd=root)
            user32 = ctypes.windll.user32
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            windows = []
            @callback_type
            def visit(hwnd, _data):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == process.pid and user32.IsWindowVisible(hwnd):
                    windows.append(hwnd)
                return True
            try:
                deadline = time.monotonic() + 10
                while not windows and time.monotonic() < deadline and process.poll() is None:
                    user32.EnumWindows(visit, 0)
                    time.sleep(0.1)
                self.assertTrue(windows, 'Packaged GUI did not create a window')
                user32.PostMessageW(windows[0], 0x0010, 0, 0)
                self.assertEqual(process.wait(timeout=10), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
            async def mcp_round_trip():
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
                params = StdioServerParameters(command=str(cli), args=['--mcp'], env=env)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        names = {t.name for t in (await session.list_tools()).tools}
                        self.assertTrue({'pmf_mount', 'pmf_unmount', 'pmf_write'} <= names)
                        result = await session.call_tool('pmf_write', {'image_path': str(image), 'path': 'via-mcp.txt', 'content': 'portable MCP'})
                        self.assertFalse(result.isError, result.content)
                        result = await session.call_tool('pmf_read', {'image_path': str(image), 'path': 'via-mcp.txt'})
                        self.assertFalse(result.isError, result.content)
                        self.assertIn('portable MCP', json.dumps([c.model_dump() for c in result.content]))
            asyncio.run(mcp_round_trip())


if __name__ == '__main__':
    unittest.main(verbosity=2)
