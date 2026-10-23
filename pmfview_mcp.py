#!/usr/bin/env python3
"""
pmfview_mcp — MCP server exposing DiskGenius .pmf image investigation tools.

Runs over stdio. Register with Claude Code:

    claude mcp add pmfview --scope user -- python B:\\pmf\\pmfview_mcp.py

Tools: pmf_open, pmf_info, pmf_ls, pmf_tree, pmf_find, pmf_stat, pmf_read,
pmf_hash, pmf_extract, pmf_history, pmf_write, pmf_import, pmf_update. All tools take the image path and open/index the image
on first use (first open of a new image builds a cache and can take ~1 minute;
after that it loads in seconds).
"""
from __future__ import annotations

import atexit
import asyncio
import inspect
import json
import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from functools import wraps
from typing import ParamSpec, cast
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pmfview import Entry, PmfError, PmfImage, ft_str, human  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from pmf_queue import JobQueue, Operation, default_database  # noqa: E402
from pmf_update import put_pmf_file, update_pmf_image, write_pmf_file  # noqa: E402

mcp = FastMCP(
    "pmfview",
    instructions=(
        "Read, extract, and incrementally update PMF partition backup images "
        "(VIMG, Backup-All-Files mode). Paths inside images are Windows-style "
        "and case-insensitive; '' or '\\' is the volume root. The first open "
        "of a new image builds an index cache and may take about a minute. "
        "Writes append PMFViewer revisions; native DiskGenius restore is not validated. "
        "pmf_write replaces complete file contents; pmf_import imports a local file. "
        "Use expected_revision to prevent overwriting a newer edit. "
        "All image operations enter a persistent FIFO queue. Calls wait briefly, "
        "then return a job ID if still running; use pmf_job for status and "
        "include_result=True for a bounded result page. Read only the file/range "
        "needed for the task: pmf_stat first, then pmf_read (4 KiB default, 32 KiB "
        "maximum). Listings are paginated; use next_offset and the reported revision. "
        "File contents and the full index are never included in open/list responses. "
        "Extraction saves files locally and returns a summary, not file contents. "
        "Processed requests remain until acknowledged (records are retained)."
    ),
)

_images: OrderedDict[tuple[str, int | None], PmfImage] = OrderedDict()
_tool_lock = threading.RLock()
_queue_lock = threading.RLock()
_operations: dict[str, Operation] = {}
_queues: dict[str, JobQueue] = {}
_active = threading.local()
P = ParamSpec('P')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
EXTRACT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
MAX_RESPONSE_BYTES = 65536
PAGE_BYTES = 24576
MAX_READ_BYTES = 32768
MAX_HEX_BYTES = 4096
MAX_CACHED_IMAGES = 2


def _clip(text: str, budget: int, notice: str = '\n[display truncated]') -> str:
    raw = text.encode('utf-8', errors='replace')
    if len(raw) <= budget:
        return text
    return raw[:budget - len(notice.encode('utf-8'))].decode('utf-8', errors='ignore') + notice


def _number(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PmfError(f'{name} must be between {minimum} and {maximum}')
    return value


def _page(rows: Iterable[str], offset: int, limit: int, label: str) -> str:
    """Stop before either the row or byte budget; offsets count rows, not bytes."""
    _number('offset', offset, 0, (1 << 63) - 2)
    out: list[str] = []
    used, more = 0, False
    for index, row in enumerate(rows):
        if index < offset:
            continue
        line = _clip(row, 8192)
        size = len(line.encode('utf-8')) + 1
        if len(out) >= limit or used + size > PAGE_BYTES:
            more = True
            break
        out.append(line)
        used += size
    next_offset = str(offset + len(out)) if more else 'none'
    return ('\n'.join(out) if out else '(no entries in this page)') + (
        f'\n[{label}; returned={len(out)}; offset={offset}; next_offset={next_offset}]')


def _tool(annotations: ToolAnnotations) -> Callable[[Callable[P, str]], Callable[P, str]]:
    """Keep the stdio event loop free to enqueue requests while a job is running."""
    def register(function: Callable[P, str]) -> Callable[P, str]:
        @wraps(function)
        async def run(*args: P.args, **kwargs: P.kwargs) -> str:
            try:
                result = await asyncio.to_thread(function, *args, **kwargs)
                return _clip(result, MAX_RESPONSE_BYTES,
                             '\n[response limit reached; narrow the request or use pagination]')
            except Exception as exc:
                raise PmfError(_clip(str(exc), 2048)) from None
        # Plain text is sufficient; avoid duplicating file contents in structuredContent.
        mcp.tool(annotations=annotations, structured_output=False)(run)
        return function
    return register


def _queue() -> JobQueue:
    key = str(default_database())
    with _queue_lock:
        if key not in _queues:
            _queues[key] = JobQueue(Path(key), _operations)
        return _queues[key]


def _close_queues() -> None:
    for queue in list(_queues.values()):
        queue.close()
    _queues.clear()
    for image in list(_images.values()):
        image.close()
    _images.clear()


atexit.register(_close_queues)


def _queued(function: Callable[P, str]) -> Callable[P, str]:
    """Persist requests before processing; nested helper calls stay in the same job."""
    signature = inspect.signature(function)
    def execute(arguments: dict) -> str:
        _active.inside = True
        try:
            with _tool_lock:
                return cast(Callable[..., str], function)(**arguments)
        finally:
            _active.inside = False
    _operations[function.__name__] = execute
    @wraps(function)
    def call(*args: P.args, **kwargs: P.kwargs) -> str:
        if getattr(_active, 'inside', False):
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        for name in ('image_path', 'source_path', 'out_dir'):
            if name in arguments:
                arguments[name] = str(Path(arguments[name]).expanduser().absolute())
        if 'sources' in arguments:
            arguments['sources'] = [str(Path(p).expanduser().absolute()) for p in arguments['sources']]
        queue = _queue()
        job_id = queue.submit(function.__name__, arguments)
        job = queue.wait(job_id, timeout=20, result_limit=MAX_RESPONSE_BYTES - 1024, error_limit=2048)
        if job['state'] in ('failed', 'interrupted'):
            raise PmfError(f"Job {job_id} {job['state']}: {job['error']}")
        if job['state'] == 'completed':
            continuation = (f"\n[more result: pmf_job({job_id}, include_result=True, "
                            f"result_offset={job['next_result_offset']})]"
                            if job.get('result_truncated') else '')
            return f"Job {job_id}: completed\n{job['result']}" + continuation
        return (f"Job {job_id}: {job['state']}. Request retained; use pmf_job to check results."
                + (f" Worker error: {job['worker_error']}" if job.get('worker_error') else ''))
    return call


def _key(image_path: str) -> str:
    return os.path.normcase(str(Path(image_path).resolve()))


def _drop(image_path: str) -> None:
    for key in list(_images):
        if key[0] == _key(image_path):
            _images.pop(key).close()


def _get(image_path: str, revision: int | None = None) -> PmfImage:
    key = (_key(image_path), revision)
    img = _images.get(key)
    p = Path(key[0])
    st = p.stat()
    if img is not None:
        old = img.file_stat
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (
                old.st_dev, old.st_ino, old.st_size, old.st_mtime_ns, old.st_ctime_ns):
            _drop(image_path)
            img = None
    if img is None:
        while len(_images) >= MAX_CACHED_IMAGES:
            _images.popitem(last=False)[1].close()
        img = PmfImage(p, revision=revision)
        try:
            img.load_index(progress=False)
        except BaseException:
            img.close()
            raise
        _images[key] = img
    _images.move_to_end(key)
    return img


def _entry(img: PmfImage, path: str) -> Entry:
    recno = img.resolve(path)
    if recno is None:
        raise PmfError(f"path not found in image: {path}")
    return img.entries[recno]


def _line(img: PmfImage, e: Entry, with_path: bool = False) -> str:
    kind = "<DIR>" if e.is_dir else f"{e.size:,}"
    name = img.full_path(e.recno) if with_path else e.name
    return f"{ft_str(e.mtime)}  {kind:>14}  {name}"


@_tool(annotations=READ)
@_queued
def pmf_open(image_path: str) -> str:
    """Open a .pmf image and build/load its file index. Returns a summary.
    Other tools call this implicitly, but use it first on a new image so the
    (possibly ~1 minute) indexing happens in a dedicated step."""
    img = _get(image_path)
    files = sum(1 for e in img.entries.values() if not e.is_dir)
    dirs = len(img.entries) - files
    total = sum(e.size for e in img.entries.values() if not e.is_dir)
    h = img.header
    return (
        f"Opened {h.path}\n"
        f"Filesystem: {h.filesystem}  Disk: {h.disk_model}  Serial: {h.serial}\n"
        f"Image name: {h.image_name}  Created: {h.created}\n"
        f"Entries: {len(img.entries):,} ({files:,} files, {dirs:,} dirs)\n"
        f"Total file data: {human(total)}\nRevision: {img.revision}"
        + (f"\nWARNING: {img.incremental_warning}" if img.incremental_warning else "")
        + (f"\nMetadata issues: {img.index_errors} unreadable records; {img.index_issues}"
           if img.index_errors or img.index_issues else "")
    )


@_tool(annotations=READ)
@_queued
def pmf_info(image_path: str) -> str:
    """Image header and index summary for an open or new .pmf image."""
    return pmf_open(image_path)


@_tool(annotations=READ)
@_queued
def pmf_ls(image_path: str, path: str = "", limit: int = 50, offset: int = 0,
           revision: int | None = None) -> str:
    """List one directory, without file contents. 1-200 entries per page (default 50).

    Follow next_offset with the same path and reported revision. Pages also have
    a 24 KiB text budget. '' means root; directory listing does not recurse.
    """
    _number('limit', limit, 1, 200)
    _number('offset', offset, 0, (1 << 63) - 2)
    img = _get(image_path, revision)
    recno = img.resolve(path)
    if recno is None:
        raise PmfError(f"path not found in image: {path}")
    e = img.entries.get(recno)
    kids = [recno] if e is not None and not e.is_dir else img.children.get(recno, [])
    return _page((_line(img, img.entries[r]) for r in kids), offset, limit,
                 f'revision={img.revision}; total={len(kids)}')

@_tool(annotations=READ)
@_queued
def pmf_tree(image_path: str, path: str = "", depth: int = 2, max_lines: int = 50,
             offset: int = 0, revision: int | None = None) -> str:
    """Page through a directory tree: depth 1-8, max_lines 1-200 (default 50).

    No file contents. Follow next_offset with the same depth/path/revision.
    """
    _number('depth', depth, 1, 8)
    _number('max_lines', max_lines, 1, 200)
    _number('offset', offset, 0, (1 << 63) - 2)
    img = _get(image_path, revision)
    recno = img.resolve(path)
    if recno is None:
        raise PmfError(f"path not found in image: {path}")

    def rows(r: int, prefix: str, remaining: int) -> Iterator[str]:
        kids = img.children.get(r, [])
        for i, child in enumerate(kids):
            e = img.entries[child]
            last = i == len(kids) - 1
            tag = '' if e.is_dir else f'  ({human(e.size)})'
            yield f"{prefix}{'└── ' if last else '├── '}{e.name}{tag}"
            if e.is_dir and remaining > 1:
                yield from rows(child, prefix + ('    ' if last else '│   '), remaining - 1)

    def all_rows() -> Iterator[str]:
        yield img.full_path(recno) or chr(92)
        yield from rows(recno, '', depth)

    return _page(all_rows(), offset, max_lines, f'revision={img.revision}; depth={depth}')

@_tool(annotations=READ)
@_queued
def pmf_find(image_path: str, pattern: str, limit: int = 50, path_glob: bool = False,
             offset: int = 0, revision: int | None = None) -> str:
    """Find names by substring or glob (* ? [); never search/return file contents.

    path_glob matches the full image path. Returns 1-200 matches per page
    (default 50); follow next_offset with the same pattern and revision.
    """
    import fnmatch

    _number('limit', limit, 1, 200)
    _number('offset', offset, 0, (1 << 63) - 2)
    img = _get(image_path, revision)
    pat = pattern.lower().replace('/', chr(92))
    is_glob = path_glob or any(c in pat for c in '*?[')

    def rows() -> Iterator[str]:
        for e in img.entries.values():
            hay = img.full_path(e.recno).lower() if path_glob else e.name.lower()
            hit = fnmatch.fnmatch(hay, pat.strip(chr(92))) if is_glob else pat in hay
            if hit:
                yield _line(img, e, with_path=True)

    return _page(rows(), offset, limit, f'revision={img.revision}; matches')

@_tool(annotations=READ)
@_queued
def pmf_stat(image_path: str, path: str) -> str:
    """Detailed metadata for one file/directory inside the image."""
    img = _get(image_path)
    e = _entry(img, path)
    return (
        f"Path:      {img.full_path(e.recno)}\n"
        f"Type:      {'directory' if e.is_dir else 'file'}\n"
        f"Size:      {e.size:,} bytes ({human(e.size)})\n"
        f"Created:   {ft_str(e.crtime)}\n"
        f"Modified:  {ft_str(e.mtime)}\n"
        f"Attrs:     0x{e.attr_flags:08X}\n"
        f"MFT recno: {e.recno}   parent: {e.parent}\n"
        f"Blob:      0x{e.blob:X}"
    )


@_tool(annotations=READ)
@_queued
def pmf_read(image_path: str, path: str, offset: int = 0, length: int = 4096,
             revision: int | None = None) -> str:
    """Preview exactly one file range: default 4 KiB, hard maximum 32 KiB.

    Text is decoded as UTF-8; binary uses hex (at most 4096 displayed bytes).
    Follow next_offset for more, using the reported revision. Ranges skip
    preceding data chunks; they do not verify skipped data. pmf_hash verifies
    the whole file locally. No automatic full-file extraction or content upload.
    """
    _number('offset', offset, 0, (1 << 63) - 2)
    _number('length', length, 1, MAX_READ_BYTES)
    img = _get(image_path, revision)
    e = _entry(img, path)
    data = img.read_file_range(e, offset, length)
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in data)
    is_text = bool(data) and printable / len(data) > 0.85
    if is_text:
        body = data.decode('utf-8', errors='replace')
        shown = len(data)
    elif data:
        shown = min(len(data), MAX_HEX_BYTES)
        lines = []
        for i in range(0, shown, 16):
            row = data[i:i + 16]
            hx = ' '.join(f'{b:02x}' for b in row)
            asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
            lines.append(f'{offset+i:08x}  {hx:<48}  {asc}')
        body = '\n'.join(lines)
    else:
        shown, body = 0, '(no data in range)'
    next_offset = str(offset + shown) if offset + shown < e.size else 'none'
    name = _clip(img.full_path(e.recno), 2048)
    return (f'[{name}; revision={img.revision}; bytes={offset}..{offset + shown} '
            f'of {e.size}; returned_bytes={shown}; next_offset={next_offset}]\n' + body)

@_tool(annotations=READ)
@_queued
def pmf_hash(image_path: str, path: str, algorithm: str = "sha256") -> str:
    """Hash a file inside the image (md5/sha1/sha256) without extracting it."""
    img = _get(image_path)
    e = _entry(img, path)
    if e.is_dir:
        raise PmfError(f"{path} is a directory")
    if algorithm not in ("md5", "sha1", "sha256"):
        raise PmfError("algorithm must be md5, sha1, or sha256")
    h = hashlib.new(algorithm)
    for chunk in img.iter_file_data(e):
        h.update(chunk)
    return f"{algorithm}({img.full_path(e.recno)}) = {h.hexdigest()}  ({e.size:,} bytes)"


@_tool(annotations=EXTRACT)
@_queued
def pmf_extract(image_path: str, paths: list[str], out_dir: str, quiet: bool = True,
                revision: int | None = None) -> str:
    """Extract files/directories from the image to a local folder.
    Directories recurse; entries keep their relative layout under out_dir.
    Modification times are preserved. File contents stay on local disk;
    MCP receives only counts (or at most 20 names when quiet=False)."""
    import fnmatch

    from pmfview import extract_file, extraction_target, make_extraction_directory

    img = _get(image_path, revision)
    out = Path(out_dir)
    targets: list[int] = []
    for spec in paths:
        recno = img.resolve(spec)
        if recno is not None:
            targets.append(recno)
            continue
        pat = spec.lower().replace("/", "\\").strip("\\")
        matched = [
            e.recno
            for e in img.entries.values()
            if fnmatch.fnmatch(img.full_path(e.recno).lower(), pat)
        ]
        if not matched:
            raise PmfError(f"path not found in image: {spec}")
        targets.extend(matched)

    ok = fail = 0
    extracted: list[str] = []
    errors: list[str] = []
    for recno in targets:
        base_e = img.entries.get(recno)
        if base_e is None:
            continue
        base_parent = img.full_path(base_e.parent) if base_e.parent in img.entries else ""
        for r in img.walk(recno):
            e = img.entries[r]
            full = img.full_path(r)
            rel = (
                full[len(base_parent):].lstrip("\\")
                if base_parent and full.startswith(base_parent)
                else full
            )
            dest = extraction_target(out, rel)
            if e.is_dir:
                make_extraction_directory(dest)
                continue
            try:
                extract_file(img, e, dest)
                ok += 1
                if not quiet and len(extracted) < 20:
                    extracted.append(_clip(f"{full} ({human(e.size)})", 1024))
            except (PmfError, OSError) as ex:
                fail += 1
                if len(errors) < 10:
                    errors.append(_clip(f"{full}: {ex}", 1024))

    result = f"Extracted {ok} file(s) to {out}"
    if fail:
        result += f"\n{fail} FAILED:\n" + "\n".join(errors)
    if extracted:
        result += "\n" + "\n".join(extracted)
    return result


@_tool(annotations=READ)
@_queued
def pmf_history(image_path: str, limit: int = 20, offset: int = 0) -> str:
    """List revision metadata only, 1-100 rows per page (default 20)."""
    _number('limit', limit, 1, 100)
    _number('offset', offset, 0, (1 << 63) - 2)
    img = _get(image_path)
    rows = img.revisions or [{'revision': 0, 'created': img.header.created, 'summary': 'Original image'}]
    return _page((f"{r['revision']}: {r['created']}  {r['summary']}" for r in rows),
                 offset, limit, f'total={len(rows)} revisions')

@_tool(annotations=WRITE)
@_queued
def pmf_write(image_path: str, path: str, content: str, encoding: str = 'utf-8',
              expected_revision: int | None = None) -> str:
    """Create or replace a file's COMPLETE contents inside an existing image folder.

    encoding is 'utf-8' for text or 'base64' for binary, up to 1 MiB decoded.
    Other files are preserved. Old contents remain in prior revisions.
    expected_revision (from pmf_open) rejects edits if the image has changed.
    """
    _drop(image_path)
    result = write_pmf_file(Path(image_path), path, content, encoding=encoding,
                            expected_revision=expected_revision)
    return (f'Revision {result.revision}: {result.added} added, {result.changed} updated; '
            f'{result.written:,} bytes appended. Path: {path}')


@_tool(annotations=WRITE)
@_queued
def pmf_import(image_path: str, source_path: str, path: str,
               expected_revision: int | None = None) -> str:
    """Import a local file into an existing folder in the image, creating/replacing path.

    Streams large files without sending their content through MCP. Other image
    paths are unchanged. The new revision is verified before it becomes current.
    """
    _drop(image_path)
    result = put_pmf_file(Path(image_path), path, Path(source_path),
                          expected_revision=expected_revision)
    return (f'Revision {result.revision}: {result.added} added, {result.changed} updated; '
            f'{result.written:,} bytes appended. Path: {path}')


@_tool(annotations=WRITE)
@_queued
def pmf_update(image_path: str, sources: list[str], image_root: bool = False,
               hot_image: bool = False, delete_missing: bool = False, dry_run: bool = True,
               expected_revision: int | None = None) -> str:
    """Incrementally back up local folders into the same PMF.

    Defaults to a preview. Set dry_run=False to append the revision. Each source
    name must match a top-level image folder. image_root=True maps the contents
    of exactly one folder/drive to the image root. delete_missing=True mirrors
    source deletions in those scopes; older revisions retain the deleted files.
    hot_image=True uses Windows VSS and requires administrator rights.
    """
    _drop(image_path)
    result = update_pmf_image(Path(image_path), [Path(p) for p in sources],
        image_root=image_root, hot_image=hot_image, delete_missing=delete_missing,
        dry_run=dry_run, expected_revision=expected_revision)
    return (f'{"Preview" if dry_run else "Committed"}: {result.added} added, '
            f'{result.changed} changed, {result.deleted} deleted; revision {result.revision}; '
            f'{result.written:,} bytes appended')


@_tool(annotations=READ)
def pmf_jobs(limit: int = 20, include_acknowledged: bool = False, offset: int = 0) -> str:
    """List job metadata only (no arguments/results), 1-100 rows per page."""
    _number('limit', limit, 1, 100)
    _number('offset', offset, 0, (1 << 63) - 2)
    rows = _queue().jobs(limit + 1, include_acknowledged, offset)
    return json.dumps({'jobs': rows[:limit],
                       'next_offset': offset + limit if len(rows) > limit else None}, indent=2)

@_tool(annotations=READ)
def pmf_job(job_id: int, wait_seconds: float = 0, include_result: bool = False,
            result_offset: int = 0, result_length: int = 4096) -> str:
    """Get status without file contents by default; optionally wait up to 60 seconds.

    include_result=True returns a saved-result page (4-8192 UTF-8 bytes, default
    4096). Follow next_result_offset. Request arguments are never returned.
    These limits also apply to old jobs created before response limits existed.
    """
    _number('job_id', job_id, 1, (1 << 63) - 1)
    _number('result_offset', result_offset, 0, (1 << 31) - 2)
    _number('result_length', result_length, 4, 8192)
    if not 0 <= wait_seconds <= 60:
        raise PmfError('wait_seconds must be between 0 and 60')
    return json.dumps(_queue().wait(job_id, timeout=wait_seconds,
        include_result=include_result, result_offset=result_offset,
        result_limit=result_length, error_limit=1024), indent=2, ensure_ascii=False)

@_tool(annotations=WRITE)
def pmf_acknowledge(job_id: int) -> str:
    """Mark a processed job reviewed. Its record and result remain stored."""
    _queue().acknowledge(job_id)
    return f'Job {job_id} acknowledged; record retained'


@_tool(annotations=WRITE)
def pmf_retry(job_id: int) -> str:
    """Requeue a failed/interrupted request as a NEW job. Inspect prior effects first.

    An interrupted write may have committed before its worker stopped. Check the
    image revision and any image update lock before retrying; no automatic retry.
    """
    return f'Queued job {_queue().retry(job_id)}'


@_tool(annotations=WRITE)
@_queued
def pmf_repair_mft(image_path: str, repair: bool = False) -> str:
    """Check/rebuild the PMF MFT-derived index cache (NOT an NTFS-volume MFT repair).

    repair=False checks readable metadata and parent links. repair=True publishes
    a fresh index only when the metadata is consistent. Image bytes are unchanged;
    damaged/missing records are reported for recovery, never guessed.
    """
    from pmf_repair import repair_mft_index
    _drop(image_path)
    return json.dumps(repair_mft_index(Path(image_path), repair=repair), indent=2)


@_tool(WRITE)
@_queued
def pmf_mount(image_path: str, drive: str, read_only: bool = True, revision: int | None = None) -> str:
    """Mount an image on a free Windows drive letter. Requires optional WinFsp.

    Defaults to read-only. Read/write mounts append revisions on file flush/close.
    Historical revisions are read-only. Runs in the MCP server's Windows session.
    """
    from pmf_mount import start_mount
    return json.dumps(start_mount(Path(image_path), drive, read_only=read_only, revision=revision), indent=2)


@_tool(READ)
@_queued
def pmf_mount_status(image_path: str) -> str:
    """List mount drive letters, modes, revisions, pending work, errors and recovery folders."""
    from pmf_mount import list_mounts
    return json.dumps(list_mounts(Path(image_path)), indent=2)


@_tool(WRITE)
@_queued
def pmf_flush_mount(image_path: str, drive: str) -> str:
    """Commit pending mounted file changes and return the resulting PMF revision."""
    from pmf_mount import control_mount
    return json.dumps(control_mount(drive, 'flush', image_path=Path(image_path)), indent=2)


@_tool(WRITE)
@_queued
def pmf_unmount(image_path: str, drive: str) -> str:
    """Flush and unmount. Refuses while files remain open or a save error is unresolved."""
    from pmf_mount import control_mount
    result = control_mount(drive, 'unmount', image_path=Path(image_path))
    _drop(image_path)
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    _queue()  # Resume pending requests as soon as the server starts.
    mcp.run()
