#!/usr/bin/env python3
"""
pmfview_mcp — MCP server exposing DiskGenius .pmf image investigation tools.

Runs over stdio. Register with Claude Code:

    claude mcp add pmfview --scope user -- python B:\\pmf\\pmfview_mcp.py

Tools: pmf_open, pmf_info, pmf_ls, pmf_tree, pmf_find, pmf_stat, pmf_read,
pmf_hash, pmf_extract. All tools take the image path and open/index the image
on first use (first open of a new image builds a cache and can take ~1 minute;
after that it loads in seconds).
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pmfview import Entry, PmfError, PmfImage, ft_str, human  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP(
    "pmfview",
    instructions=(
        "Read-only investigation of DiskGenius .pmf partition backup images "
        "(VIMG, Backup-All-Files mode). Paths inside images are Windows-style "
        "and case-insensitive; '' or '\\' is the volume root. The first open "
        "of a new image builds an index cache and may take about a minute."
    ),
)

_images: dict[str, PmfImage] = {}


def _get(image_path: str) -> PmfImage:
    key = os.path.normcase(os.path.abspath(image_path))
    img = _images.get(key)
    if img is None:
        p = Path(image_path)
        if not p.exists():
            raise PmfError(f"image not found: {image_path}")
        img = PmfImage(p)
        _images[key] = img
    img.load_index(progress=False)
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


@mcp.tool()
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
        f"Total file data: {human(total)}"
    )


@mcp.tool()
def pmf_info(image_path: str) -> str:
    """Image header and index summary for an open or new .pmf image."""
    return pmf_open(image_path)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def pmf_read(image_path: str, path: str, offset: int = 0, length: int = 16384) -> str:
    """Read file content from inside the image (no extraction). Returns text
    when decodable, otherwise a hex dump. length is capped at 262144 bytes."""
    img = _get(image_path)
    e = _entry(img, path)
    if e.is_dir:
        raise PmfError(f"{path} is a directory")
    length = min(length, 262144)
    end = offset + length
    buf = bytearray()
    pos = 0
    for chunk in img.iter_file_data(e):
        nxt = pos + len(chunk)
        if nxt > offset:
            buf += chunk[max(0, offset - pos) : max(0, end - pos)]
        pos = nxt
        if pos >= end:
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
        lines.append(f"... hex dump capped at 4096 bytes (read more with offset)")
    return head + "\n".join(lines)


@mcp.tool()
def pmf_hash(image_path: str, path: str, algorithm: str = "sha256") -> str:
    """Hash a file inside the image (md5/sha1/sha256) without extracting it."""
    img = _get(image_path)
    e = _entry(img, path)
    if e.is_dir:
        raise PmfError(f"{path} is a directory")
    h = hashlib.new(algorithm)
    for chunk in img.iter_file_data(e):
        h.update(chunk)
    return f"{algorithm}({img.full_path(e.recno)}) = {h.hexdigest()}  ({e.size:,} bytes)"


@mcp.tool()
def pmf_extract(image_path: str, paths: list[str], out_dir: str, quiet: bool = True) -> str:
    """Extract files/directories from the image to a local folder.
    Directories recurse; entries keep their relative layout under out_dir.
    Modification times are preserved. Returns a result summary."""
    import fnmatch

    from pmfview import _safe_rel

    img = _get(image_path)
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
            dest = out / _safe_rel(rel)
            if e.is_dir:
                dest.mkdir(parents=True, exist_ok=True)
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tgt = dest
                if os.name == "nt" and len(str(tgt)) > 250 and not str(tgt).startswith("\\\\?\\"):
                    tgt = Path("\\\\?\\" + os.path.abspath(str(tgt)))
                with open(tgt, "wb") as fh:
                    for chunk in img.iter_file_data(e):
                        fh.write(chunk)
                try:
                    from pmfview import ft_to_unix

                    ts = ft_to_unix(e.mtime)
                    if ts > 0:
                        os.utime(tgt, (ts, ts))
                except (OSError, OverflowError):
                    pass
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


if __name__ == "__main__":
    mcp.run()
