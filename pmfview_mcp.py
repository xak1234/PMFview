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
from collections.abc import Callable
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
        "then return a job ID if still running; use pmf_job for status/results. "
        "Processed requests remain until acknowledged (records are retained)."
    ),
)

_images: dict[tuple[str, int | None], PmfImage] = {}
_tool_lock = threading.RLock()
_queue_lock = threading.RLock()
_operations: dict[str, Operation] = {}
_queues: dict[str, JobQueue] = {}
_active = threading.local()
P = ParamSpec('P')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
EXTRACT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _tool(annotations: ToolAnnotations) -> Callable[[Callable[P, str]], Callable[P, str]]:
    """Keep the stdio event loop free to enqueue requests while a job is running."""
    def register(function: Callable[P, str]) -> Callable[P, str]:
        @wraps(function)
        async def run(*args: P.args, **kwargs: P.kwargs) -> str:
            return await asyncio.to_thread(function, *args, **kwargs)
        mcp.tool(annotations=annotations)(run)
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
        job = queue.wait(job_id, timeout=20)
        if job['state'] in ('failed', 'interrupted'):
            raise PmfError(f"Job {job_id} {job['state']}: {job['error']}")
        if job['state'] == 'completed':
            return f"Job {job_id}: completed\n{job['result']}"
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
        img = PmfImage(p, revision=revision)
        try:
            img.load_index(progress=False)
        except BaseException:
            img.close()
            raise
        _images[key] = img
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
def pmf_ls(image_path: str, path: str = "", limit: int = 500) -> str:
    """List a directory inside the image. `path` like 'Users\\me\\Documents'
    ('' = root). Returns 'mtime size|<DIR> name' lines, capped at `limit`."""
    img = _get(image_path)
    recno = img.resolve(path)
    if recno is None:
        raise PmfError(f"path not found in image: {path}")
    e = img.entries.get(recno)
    if e is not None and not e.is_dir and recno != img.root_recno:
        return _line(img, e, with_path=True)
    kids = img.children.get(recno, [])
    lines = [_line(img, img.entries[r]) for r in kids[:limit]]
    extra = f"\n... {len(kids) - limit} more entries (raise limit to see them)" if len(kids) > limit else ""
    return "\n".join(lines) + extra + f"\n\n{len(kids)} entries total"


@_tool(annotations=READ)
@_queued
def pmf_tree(image_path: str, path: str = "", depth: int = 2, max_lines: int = 300) -> str:
    """Directory tree of a path inside the image, `depth` levels deep."""
    img = _get(image_path)
    recno = img.resolve(path)
    if recno is None:
        raise PmfError(f"path not found in image: {path}")
    out: list[str] = [img.full_path(recno) or "\\"]
    truncated = False

    def rec(r: int, prefix: str, d: int) -> None:
        nonlocal truncated
        if truncated:
            return
        kids = img.children.get(r, [])
        for i, k in enumerate(kids):
            if len(out) >= max_lines:
                truncated = True
                return
            e = img.entries[k]
            last = i == len(kids) - 1
            tag = "" if e.is_dir else f"  ({human(e.size)})"
            out.append(f"{prefix}{'└── ' if last else '├── '}{e.name}{tag}")
            if e.is_dir and d > 1:
                rec(k, prefix + ("    " if last else "│   "), d - 1)

    rec(recno, "", depth)
    if truncated:
        out.append(f"... truncated at {max_lines} lines")
    return "\n".join(out)


@_tool(annotations=READ)
@_queued
def pmf_find(image_path: str, pattern: str, limit: int = 100, path_glob: bool = False) -> str:
    """Search files by name: substring match, or glob if the pattern contains
    * ? [. With path_glob=True the glob is matched against the full path
    (e.g. 'Users\\*\\Documents\\*.xlsx'). Returns matching entries with paths."""
    import fnmatch

    img = _get(image_path)
    pat = pattern.lower().replace("/", "\\")
    is_glob = path_glob or any(c in pat for c in "*?[")
    lines = []
    for e in img.entries.values():
        if path_glob:
            hay = img.full_path(e.recno).lower()
            hit = fnmatch.fnmatch(hay, pat.strip("\\"))
        else:
            name = e.name.lower()
            hit = fnmatch.fnmatch(name, pat) if is_glob else pat in name
        if hit:
            lines.append(_line(img, e, with_path=True))
            if len(lines) >= limit:
                lines.append(f"... stopped at {limit} matches")
                break
    return "\n".join(lines) if lines else "no matches"


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
def pmf_read(image_path: str, path: str, offset: int = 0, length: int = 16384,
             revision: int | None = None) -> str:
    """Read file content from inside the image (no extraction). Returns text
    when decodable, otherwise a hex dump. length is capped at 262144 bytes. Partial reads are previews; pmf_hash verifies
    the whole file. revision=None reads the latest committed version."""
    img = _get(image_path, revision)
    e = _entry(img, path)
    if e.is_dir:
        raise PmfError(f"{path} is a directory")
    if offset < 0 or length <= 0:
        raise PmfError("offset must be nonnegative and length must be positive")
    length = min(length, 262144)
    end = offset + length
    buf = bytearray()
    pos = 0
    for chunk in img.iter_file_data(e):
        nxt = pos + len(chunk)
        if nxt > offset:
            buf += chunk[max(0, offset - pos) : max(0, end - pos)]
        pos = nxt
        if pos >= end and end < e.size:
            break
    data = bytes(buf)
    head = f"[{img.full_path(e.recno)} bytes {offset}..{offset+len(data)} of {e.size:,}]\n"
    if not data:
        return head + "(no data in range)"
    printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in data)
    if printable / len(data) > 0.85:
        try:
            return head + data.decode("utf-8")
        except UnicodeDecodeError:
            return head + data.decode("utf-8", errors="replace")
    lines = []
    for i in range(0, min(len(data), 4096), 16):
        row = data[i : i + 16]
        hx = " ".join(f"{b:02x}" for b in row)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{offset+i:08x}  {hx:<48}  {asc}")
    if len(data) > 4096:
        lines.append("... hex dump capped at 4096 bytes (read more with offset)")
    return head + "\n".join(lines)


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
    Modification times are preserved. Returns a result summary."""
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
                if not quiet and len(extracted) < 100:
                    extracted.append(f"{full} ({human(e.size)})")
            except (PmfError, OSError) as ex:
                fail += 1
                if len(errors) < 20:
                    errors.append(f"{full}: {ex}")

    result = f"Extracted {ok} file(s) to {out}"
    if fail:
        result += f"\n{fail} FAILED:\n" + "\n".join(errors)
    if extracted:
        result += "\n" + "\n".join(extracted)
    return result


@_tool(annotations=READ)
@_queued
def pmf_history(image_path: str) -> str:
    """List committed revisions. Pass a revision to pmf_read or pmf_extract to read it."""
    img = _get(image_path)
    rows = img.revisions or [{'revision': 0, 'created': img.header.created, 'summary': 'Original image'}]
    return '\n'.join(f"{r['revision']}: {r['created']}  {r['summary']}" for r in rows)


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
def pmf_jobs(limit: int = 50, include_acknowledged: bool = False) -> str:
    """List durable request IDs/states. Pending and processed results are never auto-deleted."""
    return json.dumps(_queue().jobs(limit, include_acknowledged), indent=2)


@_tool(annotations=READ)
def pmf_job(job_id: int, wait_seconds: float = 0) -> str:
    """Get a queued request's status, result or error; optionally wait up to 60 seconds."""
    return json.dumps(_queue().wait(job_id, timeout=wait_seconds), indent=2, ensure_ascii=False)


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
