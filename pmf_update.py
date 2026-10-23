"""Append-only PMFViewer revisions; native DiskGenius restore compatibility is not claimed."""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import stat
import struct
import time
import tempfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, cast

from pmfview import (
    ATTR_DATA, BLOB_HDR, CATALOG_START, CHUNK_MAX, FILE_ATTR_DIRECTORY,
    NAME_FIELD, CreateEntry, Entry, PmfError, PmfImage, _hot_snapshot_sources,
    _write_blob, unix_to_ft,
)

COMMIT_MAGIC = b'PMFVIEW-COMMIT1\0'
FOOTER = struct.Struct('<16sQQQQ32s')
MAX_MANIFEST = 256 << 20
Progress = Callable[[str], None]


@dataclass(frozen=True)
class Commit:
    end: int
    previous: int
    base_size: int
    start: int
    document: dict[str, object]


@dataclass(frozen=True)
class SourceItem:
    path: str
    source: Path
    size: int
    directory: bool
    mtime: int
    crtime: int
    signature: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class UpdateResult:
    added: int
    changed: int
    deleted: int
    unchanged: int
    files: int
    bytes: int
    revision: int = 0
    written: int = 0
    dry_run: bool = False


@dataclass
class Change:
    path: str
    entry: CreateEntry
    write_data: bool
    digest: str
    source_item: SourceItem


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _integer(value: object, name: str, maximum: int = (1 << 64) - 1) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise PmfError(f'Invalid revision {name}')
    return value


def _read_commit(stream: BinaryIO, end: int, size: int) -> Commit | None:
    if end < CATALOG_START + FOOTER.size or end > size:
        return None
    stream.seek(end - FOOTER.size)
    raw = stream.read(FOOTER.size)
    if len(raw) != FOOTER.size or raw[:16] != COMMIT_MAGIC:
        return None
    _magic, start, length, previous, base, checksum = FOOTER.unpack(raw)
    if (not CATALOG_START <= base <= start or not 0 < length <= MAX_MANIFEST
            or start + length + FOOTER.size != end
            or previous and not base < previous <= start):
        raise PmfError('Invalid incremental commit footer')
    stream.seek(start)
    compressed = stream.read(length)
    if hashlib.sha256(compressed).digest() != checksum:
        raise PmfError('Incremental manifest checksum failed; the image was not changed')
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(compressed, MAX_MANIFEST + 1)
        if len(decoded) > MAX_MANIFEST or not decoder.eof or decoder.unused_data:
            raise ValueError('Manifest exceeds bounds or is incomplete')
        document = json.loads(decoded)
    except (ValueError, zlib.error) as exc:
        raise PmfError(f'Invalid incremental manifest: {exc}') from exc
    if not isinstance(document, dict) or document.get('format') != 'pmfview-incremental-1':
        raise PmfError('Unsupported incremental manifest')
    return Commit(end, previous, base, start, document)


def _base_end(image: PmfImage) -> int:
    """Seek over the last original blob; never scan the entire base image for a footer."""
    original = [e for e in image.entries.values() if e.blob]
    if not original:
        return CATALOG_START
    entry = max(original, key=lambda e: e.blob)
    header = image._blob_header(entry.blob)
    metadata = image.read_stream(entry.blob)
    if header is None or metadata is None:
        raise PmfError('Cannot determine the end of the original image')
    pos = entry.blob + BLOB_HDR + header[2]
    if entry.is_dir or not entry.size:
        return min(pos, image.file_size)
    data = next((a for a in image.parse_attrs(metadata) if a.atype == ATTR_DATA and not a.name), None)
    if data is not None and not data.nonres:
        return min(pos, image.file_size)
    needed = min(entry.size, data.init_size) if data is not None else entry.size
    produced = 0
    while produced < needed:
        image.f.seek(pos)
        chunk = image.f.read(8)
        if len(chunk) != 8:
            raise PmfError('Original image has a truncated data chain')
        length, compressed = struct.unpack('<II', chunk)
        if not 0 < length <= CHUNK_MAX or not 0 < compressed < (1 << 30):
            raise PmfError('Original image has an invalid data chain')
        pos += 8 + compressed
        produced += length
        if pos > image.file_size:
            raise PmfError('Original image is truncated')
    return pos


def _latest(image: PmfImage) -> Commit | None:
    direct = _read_commit(image.f, image.file_size, image.file_size)
    if direct is not None:
        return direct
    # An interrupted writer leaves a verified offset hint; it contains no external paths.
    lock = image.path.with_name(image.path.name + '.update.lock')
    try:
        hint = json.loads(lock.read_text())
        end = _integer(hint.get('committed_end'), 'lock hint')
        candidate = _read_commit(image.f, end, image.file_size)
        if candidate is not None:
            return candidate
    except (OSError, ValueError, TypeError, AttributeError, PmfError):
        pass
    floor = _base_end(image)
    end = image.file_size
    # A copied image remains recoverable even when its optional lock/cache files are absent.
    while end > floor:
        start = max(floor, end - (1 << 20))
        image.f.seek(start)
        block = image.f.read(end - start)
        offset = block.rfind(COMMIT_MAGIC)
        while offset >= 0:
            candidate_end = start + offset + FOOTER.size
            if candidate_end <= image.file_size:
                candidate = _read_commit(image.f, candidate_end, image.file_size)
                if candidate is not None:
                    return candidate
            offset = block.rfind(COMMIT_MAGIC, 0, offset)
        if start == floor:
            break
        end = start + FOOTER.size - 1
    return None


def apply_updates(image: PmfImage) -> None:
    """Merge complete revisions into the base index; incomplete trailing bytes are ignored."""
    tip = _latest(image)
    image.file_hashes = {}
    image.revisions = []
    image.revision = 0
    image.committed_end = image.file_size
    image.incremental_warning = ''
    if tip is None:
        if image.requested_revision not in (None, 0):
            raise PmfError('This image has no incremental revisions')
        return
    chain: list[Commit] = []
    current = tip
    while True:
        chain.append(current)
        if len(chain) > 10000:
            raise PmfError('Incremental chain exceeds 10,000 revisions')
        if not current.previous:
            break
        parent = _read_commit(image.f, current.previous, image.file_size)
        if parent is None or parent.base_size != tip.base_size:
            raise PmfError('Incremental parent commit is missing or inconsistent')
        current = parent
    chain.reverse()
    image.committed_end = tip.end
    if tip.end != image.file_size:
        image.incremental_warning = f'{image.file_size - tip.end:,} uncommitted trailing bytes ignored'
    selected = image.requested_revision
    if selected is not None and not 0 <= selected < len(chain):
        raise PmfError(f'Revision {selected} does not exist')
    for expected, commit in enumerate(chain):
        doc = commit.document
        revision = _integer(doc.get('revision'), 'number')
        changes, deleted = doc.get('entries'), doc.get('deleted')
        if revision != expected or not isinstance(changes, list) or not isinstance(deleted, list):
            raise PmfError('Invalid incremental revision sequence')
        image.revisions.append({'revision': revision, 'created': str(doc.get('created', '')),
                                'summary': str(doc.get('summary', '')), 'end': commit.end})
        if selected is not None and revision > selected:
            continue
        if revision == 0 and (changes or deleted or commit.previous or commit.start != commit.base_size):
            raise PmfError('Invalid original-image checkpoint')
        touched: set[int] = set()
        for raw_id in deleted:
            recno = _integer(raw_id, 'deleted ID', 0xFFFFFFFF)
            if recno == 5 or recno in touched or recno not in image.entries:
                raise PmfError('Invalid deleted record')
            touched.add(recno)
            del image.entries[recno]
            image.file_hashes.pop(recno, None)
        for row in changes:
            if not isinstance(row, dict):
                raise PmfError('Invalid incremental record')
            values = {k: _integer(row.get(k), k) for k in
                      ('recno', 'parent', 'size', 'attr_flags', 'blob', 'crtime', 'mtime')}
            recno = values['recno']
            name, digest = row.get('name'), row.get('sha256')
            if recno <= 5 or recno > 0xFFFFFFFF or recno in touched or not isinstance(name, str):
                raise PmfError('Invalid incremental record identity')
            _valid_name(name)
            if not isinstance(digest, str) or digest and (len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                raise PmfError('Invalid incremental file checksum')
            entry = Entry(values['recno'], values['parent'], name, values['size'],
                          values['attr_flags'], values['blob'], values['crtime'], values['mtime'])
            if not entry.is_dir and (not digest or not CATALOG_START <= entry.blob < commit.start):
                raise PmfError('Invalid incremental content reference')
            touched.add(recno)
            image.entries[recno] = entry
            if digest:
                image.file_hashes[recno] = digest
            else:
                image.file_hashes.pop(recno, None)
        image.revision = revision
    for entry in image.entries.values():
        if entry.recno != 5 and entry.parent != 5:
            parent_entry = image.entries.get(entry.parent)
            if parent_entry is None or not parent_entry.is_dir:
                raise PmfError('Incremental directory parent is missing')
    _previous_paths(image)  # reject duplicate paths, unresolved parents and cycles
    image._finish_index()


def _valid_name(name: str) -> None:
    if (not name or name in ('.', '..') or name.rstrip(' .') != name
            or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)
            or len(name.encode('utf-16-le')) > NAME_FIELD - 2):
        raise PmfError(f'Name cannot be represented safely in PMF: {name!r}')


def _in_scope(path: str, scopes: list[str]) -> bool:
    key = path.casefold()
    return any(not scope or key == scope or key.startswith(scope + '\\') for scope in scopes)


def _scan(sources: list[Path], names: list[str], image_root: bool) -> dict[str, SourceItem]:
    found: dict[str, SourceItem] = {}
    def walk(source: Path, path: str) -> None:
        s = source.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
            raise PmfError(f'Link/reparse point not supported: {source}. Select regular source folders.')
        directory = stat.S_ISDIR(s.st_mode)
        if not directory and not stat.S_ISREG(s.st_mode):
            raise PmfError(f'Not a regular file or directory: {source}')
        if path:
            for part in path.split('\\'):
                _valid_name(part)
            key = path.casefold()
            if key in found:
                raise PmfError(f'Duplicate image path: {path}')
            found[key] = SourceItem(path, source, 0 if directory else s.st_size, directory,
                unix_to_ft(s.st_mtime), unix_to_ft(s.st_ctime), _signature(source))
        if directory:
            with os.scandir(source) as listing:
                children = sorted(list(listing), key=lambda e: e.name.casefold())
            for child in children:
                walk(Path(child.path), path + ('\\' if path else '') + child.name)
    for source, name in zip(sources, names):
        walk(source, '' if image_root else name)
    return found


def _hash_chunks(chunks: Iterator[bytes], expected: int) -> str:
    digest = hashlib.sha256()
    count = 0
    for chunk in chunks:
        count += len(chunk)
        if count > expected:
            raise PmfError('File data exceeds the recorded size')
        digest.update(chunk)
    if count != expected:
        raise PmfError(f'Incomplete file data: {count} of {expected} bytes')
    return digest.hexdigest()


def _source_chunks(item: SourceItem) -> Iterator[bytes]:
    if _signature(item.source) != item.signature:
        raise PmfError(f'Source changed during backup: {item.source}')
    with item.source.open('rb') as stream:
        while chunk := stream.read(1 << 20):
            yield chunk
    if _signature(item.source) != item.signature:
        raise PmfError(f'Source changed during backup: {item.source}')


def _previous_paths(image: PmfImage) -> dict[str, Entry]:
    found: dict[str, Entry] = {}
    for entry in image.entries.values():
        if entry.recno == image.root_recno:
            continue
        current, seen = entry.recno, set()
        while current not in (0, 5):
            if current in seen or current not in image.entries:
                raise PmfError('Image contains an unresolved or cyclic directory parent')
            seen.add(current)
            current = image.entries[current].parent
        path = image.full_path(entry.recno)
        key = path.casefold()
        if key in found:
            raise PmfError(f'Ambiguous path in the image: {path}')
        found[key] = entry
    return found


def _plan(image: PmfImage, scanned: dict[str, SourceItem], scopes: list[str],
          progress: Progress, *, delete_missing: bool = True,
          content_only: bool = False) -> tuple[list[Change], list[int], UpdateResult]:
    old = _previous_paths(image)
    for scope in scopes:
        if scope and scope not in old:
            raise PmfError(f'Source does not match an image folder: {scope}. '
                           'Use --image-root when its contents match the image root.')
    deleted = [e.recno for k, e in old.items() if delete_missing and _in_scope(k, scopes) and k not in scanned]
    deleted_set = set(deleted)
    ids = {k: e.recno for k, e in old.items() if e.recno not in deleted_set}
    next_id = max(image.entries, default=5) + 1
    for key in sorted(scanned):
        if key not in ids:
            ids[key] = next_id
            next_id += 1
    if next_id > 0xFFFFFFFF:
        raise PmfError('Image record IDs exceed the supported range')
    changes: list[Change] = []
    added = changed = unchanged = 0
    for n, (key, item) in enumerate(sorted(scanned.items()), 1):
        previous = old.get(key)
        parent_path, _, name = item.path.rpartition('\\')
        parent_key = parent_path.casefold()
        if parent_path and parent_key not in ids:
            raise PmfError(f'Parent folder does not exist in image: {parent_path}')
        if parent_path:
            parent_item = scanned.get(parent_key)
            if not (parent_item.directory if parent_item else old[parent_key].is_dir):
                raise PmfError(f'Parent path is not a folder: {parent_path}')
        parent = ids[parent_key] if parent_path else 5
        if content_only and previous and previous.is_dir:
            raise PmfError(f'Cannot overwrite a directory with a file: {item.path}')
        digest, same = '', bool(previous and previous.is_dir and item.directory)
        if not item.directory:
            if n % 100 == 1 or n == len(scanned):
                progress(f'Comparing files: {n:,}/{len(scanned):,}')
            digest = _hash_chunks(_source_chunks(item), item.size)
            if previous and not previous.is_dir and previous.size == item.size:
                # Hash the old content too: a damaged baseline cannot masquerade as unchanged.
                same = digest == _hash_chunks(image.iter_file_data(previous), previous.size)
        flags = (previous.attr_flags if previous and previous.is_dir == item.directory
                 else FILE_ATTR_DIRECTORY if item.directory else 0)
        entry = CreateEntry(ids[key], parent, item.source, name, item.size, flags, item.crtime, item.mtime,
                            previous.blob if same and previous else 0)
        unchanged_metadata = bool(same and previous and previous.name == name and previous.parent == parent
            and previous.mtime == item.mtime and previous.crtime == item.crtime)
        if unchanged_metadata or content_only and same:
            unchanged += 1
            continue
        added += previous is None
        changed += previous is not None
        changes.append(Change(item.path, entry, not item.directory and not same, digest, item))
    remaining = [e for k, e in old.items() if k not in scanned and e.recno not in deleted_set]
    files = sum(not e.is_dir for e in remaining) + sum(not e.directory for e in scanned.values())
    total = sum(e.size for e in remaining) + sum(e.size for e in scanned.values())
    return changes, deleted, UpdateResult(added, changed, len(deleted), unchanged, files, total, revision=image.revision)


def _commit(out: BinaryIO, previous: int, base: int, document: dict[str, object]) -> int:
    decoded = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode()
    if len(decoded) > MAX_MANIFEST:
        raise PmfError('Incremental manifest is too large')
    payload = zlib.compress(decoded, 6)
    if len(payload) > MAX_MANIFEST:
        raise PmfError('Incremental manifest is too large')
    start = out.tell()
    out.write(payload)
    out.flush()
    os.fsync(out.fileno())
    out.write(FOOTER.pack(COMMIT_MAGIC, start, len(payload), previous, base, hashlib.sha256(payload).digest()))
    out.flush()
    os.fsync(out.fileno())
    return out.tell()


def _document(revision: int, summary: str, entries: list[dict[str, object]], deleted: list[int]) -> dict[str, object]:
    return {'format': 'pmfview-incremental-1', 'revision': revision,
            'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'summary': summary,
            'entries': entries, 'deleted': deleted}



@contextlib.contextmanager
def _writer(path: Path, expected_revision: int | None = None) -> Iterator[tuple[PmfImage, BinaryIO]]:
    lock = path.with_name(path.name + '.update.lock')
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PmfError(f'Update lock exists: {lock}. Check for an active/interrupted update before removing it.') from exc
    image: PmfImage | None = None
    try:
        with os.fdopen(lock_fd, 'wb') as hint:
            image = PmfImage(path)
            image.load_index(rebuild=True, progress=False)
            if image.index_errors or image.index_issues:
                raise PmfError('Image metadata needs recovery; run repair-mft for a diagnostic report. Update refused')
            if image.incremental_warning:
                raise PmfError('An incomplete tail exists. Read/extract the last revision; repair the tail before appending.')
            if expected_revision is not None and image.revision != expected_revision:
                raise PmfError(f'Revision conflict: expected {expected_revision}, current {image.revision}')
            yield image, hint
    finally:
        if image is not None:
            image.close()
        lock.unlink(missing_ok=True)


def _publish(path: Path, image: PmfImage, hint: BinaryIO, changes: list[Change],
             deleted: list[int], result: UpdateResult, check_sources: Callable[[], None],
             report: Progress, original_signature: tuple[int, int, int, int, int]) -> UpdateResult:
    if not changes and not deleted:
        return result
    if image.revision >= 9999:
        raise PmfError('Revision limit reached (10,000 checkpoints); extract the latest view to create a new image')
    base = image.file_size
    if image.revisions:
        first = _read_commit(image.f, cast(int, image.revisions[0]['end']), image.file_size)
        if first is None:
            raise PmfError('Original checkpoint is missing')
        base = first.base_size
    prior_end = image.file_size
    next_revision = image.revision + 1
    if _signature(path) != original_signature:
        raise PmfError('Image changed during comparison; update refused')
    with path.open('r+b') as out:
        out.seek(prior_end)
        rollback_end = prior_end
        published = False
        try:
            if prior_end == base:
                rollback_end = _commit(out, 0, base, _document(0, 'Original image', [], []))
            hint.write(json.dumps({'pid': os.getpid(), 'committed_end': rollback_end}).encode())
            hint.flush()
            os.fsync(hint.fileno())
            rows: list[dict[str, object]] = []
            for n, change in enumerate(changes, 1):
                if _signature(change.source_item.source) != change.source_item.signature:
                    raise PmfError(f'Source changed during backup: {change.path}')
                if change.write_data:
                    change.entry.blob = out.tell()
                    _write_blob(out, change.entry)
                row = asdict(change.entry)
                row.pop('source')
                row['sha256'] = change.digest
                rows.append(row)
                if n % 100 == 0 or n == len(changes):
                    report(f'Appending changes: {n:,}/{len(changes):,}')
            out.flush()
            os.fsync(out.fileno())
            report('Verifying appended file data…')
            check = PmfImage(path)
            try:
                for change in changes:
                    if not change.entry.is_dir:
                        raw = change.entry
                        entry = Entry(raw.recno, raw.parent, raw.name, raw.size, raw.attr_flags,
                                      raw.blob, raw.crtime, raw.mtime)
                        if _hash_chunks(check.iter_file_data(entry), entry.size) != change.digest:
                            raise PmfError(f'Appended content verification failed: {change.path}')
            finally:
                check.close()
            report('Checking source inventory before commit…')
            check_sources()
            summary = f'{result.added} added, {result.changed} changed, {result.deleted} deleted'
            report('Committing verified incremental revision…')
            end = _commit(out, rollback_end, base, _document(next_revision, summary, rows, deleted))
            published = True
        except BaseException:
            if not published:
                out.seek(rollback_end)
                out.truncate()
                out.flush()
                os.fsync(out.fileno())
            raise
    return UpdateResult(result.added, result.changed, result.deleted, result.unchanged,
                        result.files, result.bytes, revision=next_revision, written=end - prior_end)


def update_pmf_image(image_path: Path, sources: list[Path], *, image_root: bool = False,
                     hot_image: bool = False, dry_run: bool = False, delete_missing: bool = True,
                     expected_revision: int | None = None,
                     progress: Progress | None = None) -> UpdateResult:
    """Append a source-folder backup revision. Deletions affect only selected scopes."""
    report: Progress = progress if progress is not None else lambda _text: None
    path = Path(image_path).resolve(strict=True)
    originals = [Path(p).resolve(strict=True) for p in sources]
    if not originals or image_root and (len(originals) != 1 or not originals[0].is_dir()):
        raise PmfError('Choose sources; --image-root requires exactly one folder/drive')
    for source in originals:
        if source == path or source in path.parents:
            raise PmfError('The image must be outside every source folder/drive')
    names = [p.name or p.drive.rstrip(':\\/') for p in originals]
    if len({n.casefold() for n in names}) != len(names):
        raise PmfError('Source folders must have distinct names')
    scopes = [''] if image_root else [n.casefold() for n in names]
    report('Reading image and committed revisions…')
    with _writer(path, expected_revision) as (image, hint):
        original_signature = _signature(path)
        context = _hot_snapshot_sources(originals) if hot_image else contextlib.nullcontext(originals)
        with context as read_sources:
            report('Scanning source folders…')
            scanned = _scan(read_sources, names, image_root)
            changes, deleted, result = _plan(image, scanned, scopes, report, delete_missing=delete_missing)
            if dry_run:
                return UpdateResult(result.added, result.changed, result.deleted, result.unchanged,
                                    result.files, result.bytes, revision=image.revision, dry_run=True)
            def check_sources() -> None:
                if _scan(read_sources, names, image_root) != scanned:
                    raise PmfError('Source inventory changed; update cancelled. Use --hot on Windows.')
            return _publish(path, image, hint, changes, deleted, result, check_sources,
                            report, original_signature)


def put_pmf_file(image_path: Path, target_path: str, source: Path, *,
                 expected_revision: int | None = None,
                 progress: Progress | None = None) -> UpdateResult:
    """Create or replace one file in an existing folder; leave all other paths intact."""
    report: Progress = progress if progress is not None else lambda _text: None
    path = Path(image_path).resolve(strict=True)
    source = Path(source).absolute()
    if source.resolve(strict=True) == path:
        raise PmfError('Cannot import the image into itself')
    target = target_path.replace('/', '\\').strip('\\')
    if target.lower().startswith('c:'):
        target = target[2:].lstrip('\\')
    for part in target.split('\\'):
        _valid_name(part)
    with _writer(path, expected_revision) as (image, hint):
        original_signature = _signature(path)
        if not source.is_file():
            raise PmfError('Import source must be one regular file')
        scanned = _scan([source], [target], False)
        changes, deleted, result = _plan(image, scanned, [], report, content_only=True)
        def check_sources() -> None:
            if _scan([source], [target], False) != scanned:
                raise PmfError('Import source changed before commit')
        return _publish(path, image, hint, changes, deleted, result, check_sources,
                        report, original_signature)


def write_pmf_file(image_path: Path, target_path: str, content: str, *, encoding: str = 'utf-8',
                   expected_revision: int | None = None) -> UpdateResult:
    """Write complete text/base64 contents, using the same verified append transaction."""
    if len(content) > 2 << 20:
        raise PmfError('Inline content too large; import a local file instead')
    if encoding == 'utf-8':
        data = content.encode('utf-8')
    elif encoding == 'base64':
        try:
            data = base64.b64decode(content, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PmfError('Invalid base64 content') from exc
    else:
        raise PmfError("encoding must be 'utf-8' or 'base64'")
    if len(data) > 1 << 20:
        raise PmfError('Inline content exceeds 1 MiB; import a local file instead')
    with tempfile.TemporaryDirectory(prefix='pmf-write-') as directory:
        source = Path(directory) / 'content'
        source.write_bytes(data)
        return put_pmf_file(image_path, target_path, source, expected_revision=expected_revision)
