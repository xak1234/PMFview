"""Optional Windows drive mounts. Importing this module never imports WinFsp."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import threading
from typing import Any
import uuid

from pmf_mount_store import MountStore, atomic_json
from pmfview import PmfError

_children: dict[str, subprocess.Popen] = {}


def mounts_directory() -> Path:
    override = os.environ.get('PMF_MOUNT_DIR')
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'PMFViewer' / 'mounts'


def drive_name(value: str) -> str:
    value = value.upper().rstrip('\\/')
    if not re.fullmatch('[D-Z]:', value):
        raise PmfError('Choose a free Windows drive letter from D: to Z:')
    return value


def list_mounts(image_path: Path | None = None) -> list[dict[str, Any]]:
    found = []
    for path in mounts_directory().glob('*/status.json'):
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
            if image_path is not None and os.path.normcase(item['image']) != os.path.normcase(str(image_path.resolve())):
                continue
            item['workspace'] = str(path.parent)
            if item.get('state') in ('mounted', 'starting') and time.time() - item.get('heartbeat', 0) > 15:
                item['state'] = 'unresponsive'
            found.append(item)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(found, key=lambda item: item.get('started', 0), reverse=True)


def start_mount(image_path: Path, drive: str, *, read_only: bool = True,
                revision: int | None = None) -> dict[str, Any]:
    if os.name != 'nt':
        raise PmfError('Drive-letter mounting requires Windows and WinFsp; normal PMF tools remain portable')
    drive = drive_name(drive)
    if Path(drive + '\\').exists():
        raise PmfError(f'{drive} is already in use')
    if not read_only and revision is not None:
        raise PmfError('Historical revisions are read-only')
    path = image_path.resolve(strict=True)
    directory = mounts_directory() / uuid.uuid4().hex
    directory.mkdir(parents=True)
    executable = Path(sys.executable)
    if getattr(sys, 'frozen', False) and (executable.parent / 'pmf.exe').exists():
        executable = executable.parent / 'pmf.exe'
    launcher = ([str(executable), '--pmf-mount-server'] if getattr(sys, 'frozen', False)
                else [sys.executable, str(Path(__file__).resolve()), 'serve'])
    arguments = launcher + [str(path), drive, '--workspace', str(directory)]
    if not read_only:
        arguments.append('--read-write')
    if revision is not None:
        arguments.extend(['--revision', str(revision)])
    with (directory / 'mount.log').open('wb') as log:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    _children[str(directory)] = process
    deadline = time.monotonic() + 25
    status = directory / 'status.json'
    while time.monotonic() < deadline:
        if status.exists():
            item = json.loads(status.read_text(encoding='utf-8'))
            if item['state'] == 'mounted':
                return item
            if item['state'] == 'failed':
                raise PmfError(f"Mount failed: {item['error']}. Log: {directory / 'mount.log'}")
        if process.poll() is not None:
            raise PmfError(f'Mount process exited. See {directory / "mount.log"}')
        time.sleep(0.1)
    return {'state': 'starting', 'drive': drive, 'image': str(path), 'workspace': str(directory),
            'message': 'Indexing continues in the background. Check mount-status.'}


def control_mount(drive: str, action: str, *, image_path: Path | None = None) -> dict[str, Any]:
    drive = drive_name(drive)
    if action not in ('status', 'flush', 'unmount'):
        raise PmfError('Unknown mount action')
    matches = [m for m in list_mounts(image_path) if m.get('drive') == drive
               and m.get('state') in ('mounted', 'starting', 'unresponsive')]
    if not matches:
        raise PmfError(f'No PMF mount for {drive} in this Windows user profile')
    item = matches[0]
    if action == 'status':
        return item
    directory = Path(item['workspace'])
    request = uuid.uuid4().hex
    atomic_json(directory / f'request-{request}.json', {'action': action})
    response = directory / f'response-{request}.json'
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        if response.exists():
            result = json.loads(response.read_text(encoding='utf-8'))
            response.unlink()
            if not result['ok']:
                raise PmfError(result['error'])
            if action == 'unmount':
                child = _children.pop(str(directory), None)
                if child is not None:
                    child.wait(timeout=5)
            return result
        time.sleep(0.1)
    raise PmfError(f'Mount request is still pending. Check status and {directory / "mount.log"}; do not kill the process while saving.')


def serve(image_path: Path, drive: str, directory: Path, *, read_only: bool,
          revision: int | None) -> None:
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')
    status_path = directory / 'status.json'
    status: dict[str, Any] = {'state': 'starting', 'image': str(image_path), 'drive': drive,
                             'read_only': read_only, 'pid': os.getpid(), 'started': time.time(),
                             'workspace': str(directory), 'error': ''}
    store = None
    fs = None
    stop_heartbeat = threading.Event()
    status_lock = threading.RLock()
    def heartbeat():
        while not stop_heartbeat.is_set():
            with status_lock:
                status['heartbeat'] = time.time()
                if store is not None:
                    status['revision'] = store.image.revision
                    status['pending'] = sum(n.dirty for n in list(store.nodes.values()))
                    status['error'] = store.error
                atomic_json(status_path, status)
            stop_heartbeat.wait(1)
    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        try:
            from pmf_winfsp import create_filesystem
        except (ImportError, RuntimeError, OSError) as exc:
            raise PmfError('Install WinFsp and run: python -m pip install -r requirements-mount.txt. ' + str(exc)) from exc
        store = MountStore(image_path, directory, read_only=read_only, revision=revision)
        fs = create_filesystem(store, drive)
        fs.start()
        with status_lock:
            status['state'] = 'mounted'
        running = True
        while running:
            for request in sorted(directory.glob('request-*.json')):
                response = directory / request.name.replace('request-', 'response-', 1)
                try:
                    action = json.loads(request.read_text(encoding='utf-8'))['action']
                    if action == 'flush':
                        with fs.operations.lock:
                            current = store.flush()
                        result: dict[str, Any] = {'ok': True, 'revision': current, 'drive': drive}
                    elif action == 'unmount':
                        fs.operations.prepare_unmount()
                        fs.stop()
                        store.finish()
                        running = False
                        with status_lock:
                            status['state'] = 'unmounted'
                        result = {'ok': True, 'state': 'unmounted', 'drive': drive,
                                  'revision': store.image.revision}
                    else:
                        raise PmfError('Unsupported mount request')
                except Exception as exc:
                    result = {'ok': False, 'error': str(exc)}
                    logging.exception('Mount control failed')
                atomic_json(response, result)
                request.unlink()
            if running:
                time.sleep(0.1)
    except BaseException as exc:
        logging.exception('Mount stopped unexpectedly')
        with status_lock:
            status['state'] = 'failed'
            status['error'] = str(exc)
        raise
    finally:
        stop_heartbeat.set()
        worker.join()
        if fs is not None and fs.started:
            fs.stop()
        if store is not None:
            store.release()
        status['heartbeat'] = time.time()
        atomic_json(status_path, status)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    server = sub.add_parser('serve', help=argparse.SUPPRESS)
    server.add_argument('image', type=Path)
    server.add_argument('drive')
    server.add_argument('--workspace', type=Path, required=True)
    server.add_argument('--read-write', action='store_true')
    server.add_argument('--revision', type=int)
    sub.add_parser('list')
    for action in ('flush', 'unmount', 'status'):
        command = sub.add_parser(action)
        command.add_argument('drive')
    args = parser.parse_args()
    if args.command == 'serve':
        serve(args.image, args.drive, args.workspace, read_only=not args.read_write, revision=args.revision)
    elif args.command == 'list':
        print(json.dumps(list_mounts(), indent=2))
    else:
        print(json.dumps(control_mount(args.drive, args.command), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (PmfError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
