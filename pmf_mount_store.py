"""Disk-backed mount workspace; commits use the existing verified PMF journal.

Only accessed files are cached. A writable mount owns the image writer lock.
Uncommitted work is retained in pending.json and numbered .data files on failure.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any

from pmf_update import (Change, SourceItem, UpdateResult, _hash_chunks, _publish,
                        _signature, _valid_name, _writer)
from pmfview import (FILE_ATTR_DIRECTORY, CreateEntry, Entry, PmfError, PmfImage,
                     extract_file, unix_to_ft)


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@dataclass
class Node:
    entry: Entry
    cache: Path | None = None
    dirty: bool = False
    data_dirty: bool = False
    deleted: bool = False
    handles: int = 0


class MountStore:
    """Callbacks serialize access to this store; it has no implicit background work."""

    def __init__(self, image_path: Path, workspace: Path, *, read_only: bool = True,
                 revision: int | None = None):
        if not read_only and revision is not None:
            raise PmfError('Historical revisions can only be mounted read-only')
        self.path = image_path.resolve(strict=True)
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.read_only = read_only
        self.error = ''
        self.closed = False
        self._writer_context = None
        if read_only:
            self.image = PmfImage(self.path, revision=revision)
            try:
                self.image.load_index(progress=False)
                if self.image.index_issues:
                    raise PmfError('Invalid directory structure; use repair-mft before mounting')
            except BaseException:
                self.image.close()
                raise
            self.hint = None
        else:
            self._writer_context = _writer(self.path)
            self.image, self.hint = self._writer_context.__enter__()
        self.nodes = {key: Node(replace(value)) for key, value in self.image.entries.items()}
        self.next_id = max(self.nodes, default=5) + 1
        self.image_signature = _signature(self.path)
        self.sentinel = self.workspace / 'metadata.source'
        self.sentinel.touch()
        self._reindex()
        self._save_pending()

    def _reindex(self) -> None:
        self.names = {(n.entry.parent, n.entry.name.casefold()): n for n in self.nodes.values()
                      if not n.deleted and n.entry.recno != 5}

    def writable(self) -> None:
        if self.read_only:
            raise PermissionError(errno.EROFS, 'Mount is read-only')
        if self.error:
            raise PmfError(f'Mount has a pending save error: {self.error}; staging retained at {self.workspace}')

    def lookup(self, name: str) -> Node:
        path = PureWindowsPath(name)
        current = self.nodes[5]
        for part in path.parts:
            if part in ('\\', '/', '.'):
                continue
            if part == '..' or ':' in part:
                raise FileNotFoundError(name)
            if not current.entry.is_dir:
                raise NotADirectoryError(name)
            child = self.names.get((current.entry.recno, part.casefold()))
            if child is None:
                raise FileNotFoundError(name)
            current = child
        return current

    def children(self, node: Node) -> list[Node]:
        if not node.entry.is_dir:
            raise NotADirectoryError(node.entry.name)
        return [n for n in self.nodes.values() if not n.deleted and n.entry.recno != 5
                and n.entry.parent == node.entry.recno]

    def full_path(self, node: Node) -> str:
        parts = []
        while node.entry.recno != 5:
            parts.append(node.entry.name)
            node = self.nodes[node.entry.parent]
        return '\\'.join(reversed(parts))

    def _save_pending(self) -> None:
        dirty = [n for n in self.nodes.values() if n.dirty]
        atomic_json(self.workspace / 'pending.json', {
            'image': str(self.path), 'revision': self.image.revision, 'error': self.error,
            'entries': [{'path': self.full_path(n), 'entry': asdict(n.entry),
                         'data': str(n.cache) if n.cache else None,
                         'data_dirty': n.data_dirty, 'deleted': n.deleted} for n in dirty]})

    def changed(self, node: Node, *, data: bool = False) -> None:
        node.dirty = True
        node.data_dirty |= data
        self._save_pending()

    def data_path(self, node: Node) -> Path:
        if node.entry.is_dir:
            raise IsADirectoryError(node.entry.name)
        if node.cache is None:
            target = self.workspace / f'{node.entry.recno}.data'
            original = self.image.entries.get(node.entry.recno)
            if original is not None:
                extract_file(self.image, original, target)
            else:
                target.write_bytes(b'')
            node.cache = target
        return node.cache

    def create(self, name: str, directory: bool, attributes: int = 0) -> Node:
        self.writable()
        path = PureWindowsPath(name)
        _valid_name(path.name)
        parent = self.lookup(str(path.parent))
        if not parent.entry.is_dir:
            raise NotADirectoryError(str(path.parent))
        if (parent.entry.recno, path.name.casefold()) in self.names:
            raise FileExistsError(name)
        now = unix_to_ft(time.time())
        flags = (attributes & 0x27) | (FILE_ATTR_DIRECTORY if directory else 0)
        node = Node(Entry(self.next_id, parent.entry.recno, path.name, 0, flags, 0, now, now))
        self.next_id += 1
        self.nodes[node.entry.recno] = node
        self._reindex()
        if not directory:
            self.data_path(node)
        self.changed(node, data=not directory)
        return node

    def read(self, node: Node, offset: int, length: int) -> bytes:
        with self.data_path(node).open('rb') as stream:
            stream.seek(offset)
            return stream.read(length)

    def write(self, node: Node, data: bytes, offset: int, *, append: bool = False,
              constrained: bool = False) -> int:
        self.writable()
        if append:
            offset = node.entry.size
        if constrained:
            data = data[:max(0, node.entry.size - offset)]
        if not data:
            return 0
        with self.data_path(node).open('r+b') as stream:
            stream.seek(offset)
            count = stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            node.entry.size = os.fstat(stream.fileno()).st_size
        node.entry.mtime = unix_to_ft(time.time())
        self.changed(node, data=True)
        return count

    def resize(self, node: Node, size: int, *, allocation: bool = False) -> None:
        self.writable()
        if allocation and size >= node.entry.size or size == node.entry.size:
            return
        with self.data_path(node).open('r+b') as stream:
            stream.truncate(size)
            stream.flush()
            os.fsync(stream.fileno())
        node.entry.size = size
        node.entry.mtime = unix_to_ft(time.time())
        self.changed(node, data=True)

    def can_delete(self, node: Node) -> None:
        self.writable()
        if node.entry.recno == 5:
            raise PermissionError('Cannot delete the volume root')
        if node.entry.is_dir and self.children(node):
            raise OSError(errno.ENOTEMPTY, 'Directory is not empty')

    def delete(self, node: Node) -> None:
        if node.deleted:
            return
        self.can_delete(node)
        node.deleted = True
        self._reindex()
        self.changed(node)

    def rename(self, node: Node, name: str, replace_existing: bool) -> None:
        self.writable()
        if node.entry.recno == 5:
            raise PermissionError('Cannot rename the volume root')
        path = PureWindowsPath(name)
        _valid_name(path.name)
        parent = self.lookup(str(path.parent))
        if not parent.entry.is_dir:
            raise NotADirectoryError(str(path.parent))
        ancestor = parent
        while ancestor.entry.recno != 5:
            if ancestor is node:
                raise PermissionError('Cannot move a folder into itself')
            ancestor = self.nodes[ancestor.entry.parent]
        existing = self.names.get((parent.entry.recno, path.name.casefold()))
        if existing is not None and existing is not node:
            if not replace_existing:
                raise FileExistsError(name)
            if existing.entry.is_dir or node.entry.is_dir:
                raise PermissionError('Cannot replace a directory')
            existing.deleted = True
            existing.dirty = True
        node.entry.parent = parent.entry.recno
        node.entry.name = path.name
        self._reindex()
        self.changed(node)

    def flush(self) -> int:
        if self.read_only:
            return self.image.revision
        self.writable()
        dirty = [n for n in self.nodes.values() if n.dirty and not n.deleted and n.entry.recno != 5]
        deleted = [n.entry.recno for n in self.nodes.values()
                   if n.deleted and n.entry.recno in self.image.entries]
        if not dirty and not deleted:
            return self.image.revision
        changes = []
        for node in dirty:
            e = node.entry
            source = self.data_path(node) if node.data_dirty and not e.is_dir else self.sentinel
            sig = _signature(source)
            if e.is_dir:
                digest = ''
            elif node.data_dirty:
                with source.open('rb') as stream:
                    digest = _hash_chunks(iter(lambda: stream.read(1 << 20), b''), e.size)
            else:
                digest = self.image.file_hashes.get(e.recno, '')
                if not digest:
                    digest = _hash_chunks(self.image.iter_file_data(self.image.entries[e.recno]), e.size)
            item = SourceItem(self.full_path(node), source, e.size, e.is_dir, e.mtime, e.crtime, sig)
            entry = CreateEntry(e.recno, e.parent, source if node.data_dirty else None,
                                e.name, e.size, e.attr_flags, e.crtime, e.mtime, blob=e.blob)
            changes.append(Change(item.path, entry, node.data_dirty and not e.is_dir, digest, item))
        live = [n for n in self.nodes.values() if not n.deleted and not n.entry.is_dir]
        added = sum(n.entry.recno not in self.image.entries for n in dirty)
        result = UpdateResult(added, len(dirty) - added, len(deleted),
                              len(self.image.entries) - len(deleted) - (len(dirty) - added),
                              len(live), sum(n.entry.size for n in live), revision=self.image.revision)
        def check_sources() -> None:
            for change in changes:
                if _signature(change.source_item.source) != change.source_item.signature:
                    raise PmfError('Mount cache changed during commit')
        try:
            assert self.hint is not None
            self.hint.seek(0)
            self.hint.truncate()
            result = _publish(self.path, self.image, self.hint, changes, deleted, result,
                              check_sources, lambda _message: None, self.image_signature)
            # Reopen the committed view before clearing pending work.
            fresh = PmfImage(self.path)
            try:
                fresh.load_index(progress=False)
            except BaseException:
                fresh.close()
                raise
            self.image.close()
            self.image = fresh
            self.image_signature = _signature(self.path)
            for node in self.nodes.values():
                if not node.deleted and node.entry.recno in fresh.entries:
                    node.entry = replace(fresh.entries[node.entry.recno])
                node.dirty = node.data_dirty = False
            self._save_pending()
            return result.revision
        except Exception as exc:
            self.error = str(exc)
            self._save_pending()
            raise

    def finish(self) -> None:
        if self.closed:
            return
        if not self.read_only:
            self.flush()
        self.release()
        for file in self.workspace.glob('*.data'):
            file.unlink()
        self.sentinel.unlink(missing_ok=True)

    def release(self) -> None:
        """Release resources without discarding the recovery workspace."""
        if self.closed:
            return
        self.closed = True
        self.image.close()
        if self._writer_context is not None:
            self._writer_context.__exit__(None, None, None)

    def volume_info(self) -> dict[str, int | str]:
        disk = shutil.disk_usage(self.path.parent)
        cache = shutil.disk_usage(self.workspace)
        return {'total_size': disk.total, 'free_size': min(disk.free, cache.free),
                'volume_label': ('PMF ' + self.path.stem)[:31]}
