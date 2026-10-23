"""Durable FIFO jobs for PMF MCP operations, shared by local server processes."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, BinaryIO

Operation = Callable[[dict[str, Any]], str]
TERMINAL = {'completed', 'failed', 'interrupted'}


def default_database() -> Path:
    override = os.environ.get('PMF_QUEUE_DB')
    if override:
        return Path(override).expanduser().resolve()
    root = Path(os.environ['LOCALAPPDATA']) if os.name == 'nt' else Path.home() / '.local' / 'state'
    return root / 'PMFViewer' / 'queue.sqlite3'


class JobQueue:
    def __init__(self, database: Path, operations: dict[str, Operation], *, start: bool = True):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.operations = operations
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.worker_error = ''
        with self._db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL, arguments TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL,
                started REAL, finished REAL, result TEXT, error TEXT,
                acknowledged INTEGER NOT NULL DEFAULT 0)''')
        self.thread: threading.Thread | None = None
        if start:
            self.thread = threading.Thread(target=self._work, daemon=True, name='PMF queue')
            self.thread.start()

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, operation: str, arguments: dict[str, Any]) -> int:
        if operation not in self.operations:
            raise ValueError(f'Unknown queue operation: {operation}')
        with self._db() as db:
            row = db.execute('INSERT INTO jobs(operation,arguments,created) VALUES (?,?,?)',
                             (operation, json.dumps(arguments, ensure_ascii=False), time.time()))
            job_id = int(row.lastrowid or 0)
        self.wake.set()
        return job_id

    def get(self, job_id: int) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError(f'Job {job_id} does not exist')
        result = dict(row)
        result.pop('arguments')  # Request contents stay local; status does not echo file content.
        return result

    def jobs(self, limit: int = 50, include_acknowledged: bool = False) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute('''SELECT id,operation,state,created,started,finished,acknowledged
                FROM jobs WHERE (? OR acknowledged=0) ORDER BY id DESC LIMIT ?''',
                (include_acknowledged, max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in rows]

    def wait(self, job_id: int, timeout: float = 20) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, min(timeout, 60))
        while True:
            job = self.get(job_id)
            if job['state'] in TERMINAL or time.monotonic() >= deadline:
                return job
            if self.worker_error:
                job['worker_error'] = self.worker_error
                return job
            self.stop.wait(min(0.05, max(0, deadline - time.monotonic())))

    def acknowledge(self, job_id: int) -> None:
        if self.get(job_id)['state'] not in TERMINAL:
            raise ValueError('Only processed/interrupted jobs can be acknowledged')
        with self._db() as db:
            db.execute('UPDATE jobs SET acknowledged=1 WHERE id=?', (job_id,))

    def retry(self, job_id: int) -> int:
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['state'] not in ('failed', 'interrupted'):
                raise ValueError('Only failed or interrupted jobs can be retried')
            operation, arguments = row['operation'], json.loads(row['arguments'])
        return self.submit(operation, arguments)

    @staticmethod
    def _claim_worker(handle: BinaryIO) -> bool:
        try:
            handle.seek(0)
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _work(self) -> None:
        # OS-held lock is automatically released on process death. Only its owner
        # may claim jobs or mark another worker's running job as interrupted.
        try:
            lock_path = self.database.with_suffix(self.database.suffix + '.worker.lock')
            with lock_path.open('a+b') as lease:
                if lease.seek(0, os.SEEK_END) == 0:
                    lease.write(b'0')
                    lease.flush()
                while not self.stop.is_set() and not self._claim_worker(lease):
                    self.stop.wait(0.25)
                if self.stop.is_set():
                    return
                with self._db() as db:
                    db.execute("""UPDATE jobs SET state='interrupted',finished=?,error=?
                        WHERE state='running'""", (time.time(),
                        'Worker stopped. Operation may have committed; inspect the image before retrying.'))
                while not self.stop.is_set():
                    with self._db() as db:
                        db.execute('BEGIN IMMEDIATE')
                        row = db.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
                        if row is not None:
                            db.execute("UPDATE jobs SET state='running',started=? WHERE id=?",
                                       (time.time(), row['id']))
                    if row is None:
                        self.wake.wait(0.25)
                        self.wake.clear()
                        continue
                    state, result, error = 'completed', None, None
                    try:
                        operation = self.operations.get(row['operation'])
                        if operation is None:
                            raise ValueError(f"Operation unavailable: {row['operation']}")
                        result = operation(json.loads(row['arguments']))
                    except Exception as exc:
                        state, error = 'failed', f'{type(exc).__name__}: {exc}'
                    with self._db() as db:
                        db.execute('UPDATE jobs SET state=?,finished=?,result=?,error=? WHERE id=?',
                                   (state, time.time(), result, error, row['id']))
        except Exception as exc:
            self.worker_error = f'{type(exc).__name__}: {exc}'

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description='Inspect/manage the persistent PMF request queue')
    parser.add_argument('--database', type=Path, default=default_database())
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    for name in ('show', 'ack', 'retry'):
        sub.add_parser(name).add_argument('job_id', type=int)
    submit = sub.add_parser('submit')
    submit.add_argument('operation', help='MCP operation, e.g. pmf_write or pmf_read')
    submit.add_argument('arguments', type=Path, help='JSON file containing the tool arguments')
    sub.add_parser('run', help='process queued jobs until Ctrl+C (requires MCP SDK)')
    args = parser.parse_args()
    if args.command in ('run', 'submit', 'retry'):
        import pmfview_mcp
        operations = pmfview_mcp._operations
    else:
        operations = {}
    queue = JobQueue(args.database, operations, start=args.command == 'run')
    try:
        if args.command == 'list':
            print(json.dumps(queue.jobs(include_acknowledged=True), indent=2))
        elif args.command == 'show':
            print(json.dumps(queue.get(args.job_id), indent=2, ensure_ascii=False))
        elif args.command == 'ack':
            queue.acknowledge(args.job_id)
            print(f'Job {args.job_id} acknowledged; record retained')
        elif args.command == 'retry':
            print(f'Queued job {queue.retry(args.job_id)}')
        elif args.command == 'submit':
            arguments = json.loads(args.arguments.read_text(encoding='utf-8'))
            if not isinstance(arguments, dict):
                raise ValueError('Arguments must be a JSON object')
            print(f'Queued job {queue.submit(args.operation, arguments)}')
        else:
            print(f'Processing queue: {queue.database}', flush=True)
            while not queue.stop.wait(0.25):
                if queue.worker_error:
                    raise RuntimeError(queue.worker_error)
    except KeyboardInterrupt:
        pass
    finally:
        queue.close()


if __name__ == '__main__':
    main()
