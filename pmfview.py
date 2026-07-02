#!/usr/bin/env python3
"""
pmfview — read-only file viewer/extractor for DiskGenius .pmf partition images
(VIMG container, "Backup All Files" mode).

Reverse-engineered format (hash-verified against real backup images):

  Image layout
    0x0000   VIMG header: magic "VIMG", filesystem tag @0x0C, disk model @0x11,
             serial @0x41, image name UTF-16 @0x252, created y/m/d/h/m/s @0x274.
    0x10000  Catalog: packed 0x44-byte entries (4-byte slip alignment between
             runs). Entry:
               +0x00 u64  NTFS $LogFile LSN of the record (unique id)
               +0x08 u64  FILETIME (modified)
               +0x10 u64  FILETIME
               +0x18 u64  file size in bytes
               +0x20 u64  blob offset in image
               +0x2C u64  FILETIME (created)
               +0x34 u32  NTFS MFT record number
    blobs    From first blob offset to EOF, one blob per file, in catalog order:
               [21-byte header][zlib metadata stream][data chunk]...
             Blob header: u32 recno, u32 0, u8 alg, u32 declen, u32 declen,
             u32 complen (compressed length of the metadata stream).
             Data chunks (only for files whose $DATA is non-resident):
             repeated [u32 declen (<=1MiB)][u32 complen][zlib bytes] until the
             file's full size is covered.

  Metadata stream (decompressed)
    0x0000   file name, UTF-16LE, field is 0x208 bytes
    0x0208   u64 parent MFT reference (48-bit recno + 16-bit sequence)
    0x0210   u64 file size
    0x0218   FILETIME created, modified, accessed
    0x0230   u32 NTFS attribute flags (FILE_ATTRIBUTE_*, 0x10000000 = directory)
    0x0238   u64 self MFT reference
    0x0240   NTFS FILE record header (0x30 bytes; "FILE", usa @0x30)
    Attributes are stored in fixed 0x248-byte slots:
      slot S:   attr header (0x18 resident form / 0x40 non-resident form),
                attr name (UTF-16) at S+0x48 when named
      S+0x248:  u64 self-ref marker, then the attr's resident content
                (content_len bytes), then the next attr header.
      Non-resident attrs contribute no content bytes to the stream; their run
      lists are zeroed. Non-resident $DATA content lives in the blob's data
      chunks after the metadata stream.
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import os
import pickle
import gzip
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

MAGIC = b"VIMG"
CATALOG_START = 0x10000
ENTRY_SIZE = 0x44
BLOB_HDR = 21
NAME_FIELD = 0x208
RECORD_OFF = 0x240
ATTR_SLOT = 0x248
CHUNK_MAX = 0x10000000          # sanity cap for a single data chunk (256 MiB)
FT_MIN = 116444736000000000        # 1970-01-01
FT_MAX = 159000000000000000        # ~2104
INDEX_CACHE_VERSION = 3

ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF
FILE_ATTR_DIRECTORY = 0x10000000   # DiskGenius wrapper flag for directories
MFT_FLAG_DIR = 0x0002

EPOCH_DELTA = 11644473600          # seconds between 1601 and 1970


def u16(d: bytes, o: int) -> int:
    return struct.unpack_from("<H", d, o)[0]


def u32(d: bytes, o: int) -> int:
    return struct.unpack_from("<I", d, o)[0]


def u64(d: bytes, o: int) -> int:
    return struct.unpack_from("<Q", d, o)[0]


def ft_to_unix(ft: int) -> float:
    return ft / 10_000_000 - EPOCH_DELTA


def ft_str(ft: int) -> str:
    if not (FT_MIN <= ft <= FT_MAX):
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ft_to_unix(ft)))


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n}"


def utf16z(d: bytes, off: int, maxbytes: int) -> str:
    end = off + maxbytes
    i = off
    while i + 1 < end:
        if d[i] == 0 and d[i + 1] == 0:
            break
        i += 2
    return d[off:i].decode("utf-16-le", errors="replace")


@dataclass
class Header:
    path: Path
    file_size: int
    filesystem: str
    disk_model: str
    serial: str
    image_name: str
    created: str


@dataclass
class Entry:
    recno: int
    parent: int
    name: str
    size: int
    attr_flags: int
    blob: int
    crtime: int
    mtime: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attr_flags & FILE_ATTR_DIRECTORY)


@dataclass
class Attr:
    atype: int
    name: str
    nonres: bool
    content_off: int      # offset in decompressed stream (resident only)
    content_len: int
    real_size: int        # non-resident only
    init_size: int


class PmfError(Exception):
    pass


class PmfImage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.f: BinaryIO = self.path.open("rb")
        self.file_size = self.path.stat().st_size
        self.header = self._parse_header()
        self.entries: dict[int, Entry] = {}
        self.children: dict[int, list[int]] = {}
        self.root_recno = 5
        self._loaded = False

    # ---------- header ----------

    def _parse_header(self) -> Header:
        self.f.seek(0)
        h = self.f.read(0x400)
        if h[:4] != MAGIC:
            raise PmfError(f"{self.path}: not a DiskGenius PMF/VIMG image")
        year = u16(h, 0x274)
        created = (
            f"{year:04d}-{h[0x276]:02d}-{h[0x277]:02d} "
            f"{h[0x278]:02d}:{h[0x279]:02d}:{h[0x27A]:02d}"
        )
        return Header(
            path=self.path,
            file_size=self.file_size,
            filesystem=h[0x0C:0x10].decode("ascii", "replace").rstrip("\x00"),
            disk_model=h[0x11:0x31].decode("ascii", "replace").rstrip("\x00"),
            serial=h[0x41:0x51].decode("ascii", "replace").rstrip("\x00"),
            image_name=utf16z(h, 0x252, 0x20),
            created=created,
        )

    # ---------- catalog / index ----------

    def _cache_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".pmfindex")

    def load_index(self, rebuild: bool = False, progress: bool = True) -> None:
        if self._loaded and not rebuild:
            return
        cache = self._cache_path()
        st = self.path.stat()
        if not rebuild and cache.exists():
            try:
                with gzip.open(cache, "rb") as fh:
                    payload = pickle.load(fh)
                if (
                    payload.get("version") == INDEX_CACHE_VERSION
                    and payload.get("size") == st.st_size
                    and payload.get("mtime") == st.st_mtime_ns
                ):
                    self.entries = payload["entries"]
                    self._finish_index()
                    if progress:
                        print(
                            f"[index] loaded {len(self.entries):,} entries from cache",
                            file=sys.stderr,
                        )
                    return
            except Exception:
                pass
        self._build_index(progress=progress)
        try:
            with gzip.open(cache, "wb", compresslevel=1) as fh:
                pickle.dump(
                    {
                        "version": INDEX_CACHE_VERSION,
                        "size": st.st_size,
                        "mtime": st.st_mtime_ns,
                        "entries": self.entries,
                    },
                    fh,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            if progress:
                print(f"[index] cached to {cache.name}", file=sys.stderr)
        except OSError as e:
            print(f"[index] warning: could not write cache: {e}", file=sys.stderr)

    def _catalog_end(self) -> int:
        """Catalog runs from CATALOG_START to the first blob (blobs are laid
        out in catalog order, so the first valid entry's blob bounds it)."""
        self.f.seek(CATALOG_START)
        buf = self.f.read(1 << 16)
        i = 0
        while i + ENTRY_SIZE <= len(buf):
            ft1 = u64(buf, i + 8)
            crt = u64(buf, i + 0x2C)
            blob = u64(buf, i + 0x20)
            if (
                FT_MIN <= ft1 <= FT_MAX
                and FT_MIN <= crt <= FT_MAX
                and CATALOG_START < blob < self.file_size
                and u32(buf, i + 0x34)
            ):
                return blob
            i += 2
        return min(self.file_size, 0x8000000)

    def scan_catalog(self, progress: bool = True) -> list[tuple[int, int, int, int, int]]:
        """Return [(blob, recno, size, crtime, mtime)] in catalog order."""
        end = self._catalog_end()
        self.f.seek(CATALOG_START)
        buf = self.f.read(end - CATALOG_START)
        out = []
        i = 0
        limit = len(buf) - ENTRY_SIZE
        fsize = self.file_size
        while i <= limit:
            ft1 = u64(buf, i + 8)
            if not (FT_MIN <= ft1 <= FT_MAX):
                # not an entry here: slip by 2, fast-skipping zero runs
                if buf[i] == 0:
                    j = i
                    while j <= limit and buf[j] == 0:
                        j += 2
                    if j > i + 2:
                        i = j - 2
                i += 2
                continue
            blob = u64(buf, i + 0x20)
            recno = u32(buf, i + 0x34)
            plausible_blob = end <= blob < fsize
            if blob == 0 or plausible_blob:
                # a real record (possibly empty/deleted) — keep 0x44 stride
                if plausible_blob and recno:
                    crt = u64(buf, i + 0x2C)
                    if crt == 0 or FT_MIN <= crt <= FT_MAX:
                        out.append((blob, recno, u64(buf, i + 0x18), crt, ft1))
                i += ENTRY_SIZE
            else:
                i += 2
        if progress:
            print(f"[catalog] region 0x{CATALOG_START:X}..0x{end:X}", file=sys.stderr)
        return out

    def _build_index(self, progress: bool = True) -> None:
        t0 = time.time()
        cat = self.scan_catalog(progress=progress)
        if progress:
            print(
                f"[catalog] {len(cat):,} entries in {time.time()-t0:.1f}s",
                file=sys.stderr,
            )
        entries: dict[int, Entry] = {}
        f = self.f
        t0 = time.time()
        n_bad = 0
        for n, (blob, recno, size, crtime, mtime) in enumerate(cat):
            meta = self._read_stream_prefix(blob, expect_recno=recno)
            if meta is None or len(meta) < RECORD_OFF:
                # keep an earlier good entry for this recno if we have one
                if recno not in entries:
                    n_bad += 1
                continue
            name = utf16z(meta, 0, NAME_FIELD)
            parent = u64(meta, 0x208) & 0xFFFFFFFFFFFF
            attr_flags = u32(meta, 0x230)
            entries[recno] = Entry(
                recno=recno,
                parent=parent,
                name=name,
                size=size,
                attr_flags=attr_flags,
                blob=blob,
                crtime=crtime,
                mtime=mtime,
            )
            if progress and (n + 1) % 50000 == 0:
                rate = (n + 1) / (time.time() - t0)
                eta = (len(cat) - n - 1) / rate
                print(
                    f"[index] {n+1:,}/{len(cat):,} streams "
                    f"({rate:,.0f}/s, ~{eta:,.0f}s left)",
                    file=sys.stderr,
                )
        if n_bad:
            print(f"[index] {n_bad} unreadable streams skipped", file=sys.stderr)
        self.entries = entries
        self._finish_index()
        if progress:
            print(
                f"[index] {len(entries):,} files indexed in {time.time()-t0:.1f}s",
                file=sys.stderr,
            )

    def _finish_index(self) -> None:
        children: dict[int, list[int]] = {}
        for e in self.entries.values():
            if e.recno == self.root_recno:
                continue
            children.setdefault(e.parent, []).append(e.recno)
        for lst in children.values():
            lst.sort(key=lambda r: (not self.entries[r].is_dir, self.entries[r].name.lower()))
        self.children = children
        self._loaded = True

    # ---------- stream reading ----------

    def _blob_header(self, blob: int) -> Optional[tuple[int, int, int]]:
        self.f.seek(blob)
        hdr = self.f.read(BLOB_HDR)
        if len(hdr) < BLOB_HDR:
            return None
        recno = u32(hdr, 0)
        declen = u32(hdr, 9)
        complen = u32(hdr, 17)
        if not (0 < complen < (1 << 30)):
            return None
        return recno, declen, complen

    def _read_stream_prefix(
        self, blob: int, want: int = 0x240, expect_recno: Optional[int] = None
    ) -> Optional[bytes]:
        bh = self._blob_header(blob)
        if bh is None:
            return None
        hdr_recno, declen, complen = bh
        if expect_recno is not None and hdr_recno != (expect_recno & 0xFFFFFFFF):
            return None
        self.f.seek(blob + BLOB_HDR)
        comp = self.f.read(min(complen, 2048))
        try:
            d = zlib.decompressobj()
            return d.decompress(comp, want)
        except zlib.error:
            return None

    def read_stream(self, blob: int) -> Optional[bytes]:
        bh = self._blob_header(blob)
        if bh is None:
            return None
        _, declen, complen = bh
        self.f.seek(blob + BLOB_HDR)
        comp = self.f.read(complen)
        try:
            return zlib.decompress(comp)
        except zlib.error:
            return None

    # ---------- record / attribute parsing ----------

    def parse_attrs(self, dec: bytes) -> list[Attr]:
        if len(dec) < RECORD_OFF + 0x30 or dec[RECORD_OFF : RECORD_OFF + 4] != b"FILE":
            return []
        attrs_off = u16(dec, RECORD_OFF + 0x14)
        count_pos = RECORD_OFF + attrs_off
        if count_pos + 4 > len(dec):
            return []
        count = u32(dec, count_pos)
        if count > 64:
            return []
        attrs: list[Attr] = []
        pos = count_pos + 4
        for _ in range(count):
            if pos + 0x18 > len(dec):
                break
            atype = u32(dec, pos)
            if atype in (0, ATTR_END):
                break
            nonres = dec[pos + 8] != 0
            namelen = dec[pos + 9]
            name = ""
            if namelen and pos + 0x48 + 2 * namelen <= len(dec):
                name = dec[pos + 0x48 : pos + 0x48 + 2 * namelen].decode(
                    "utf-16-le", errors="replace"
                )
            if nonres:
                real = u64(dec, pos + 0x30) if pos + 0x38 <= len(dec) else 0
                init = u64(dec, pos + 0x38) if pos + 0x40 <= len(dec) else 0
                attrs.append(Attr(atype, name, True, 0, 0, real, init))
                pos = pos + ATTR_SLOT + 8
            else:
                clen = u32(dec, pos + 0x10)
                coff = pos + ATTR_SLOT + 8
                attrs.append(Attr(atype, name, False, coff, clen, clen, clen))
                pos = coff + clen
        return attrs

    # ---------- data extraction ----------

    def iter_file_data(self, entry: Entry) -> Iterator[bytes]:
        """Yield the file's content in chunks."""
        if entry.size == 0:
            return
        bh = self._blob_header(entry.blob)
        if bh is None:
            raise PmfError(f"unreadable blob @0x{entry.blob:X}")
        _, declen, complen = bh
        dec = self.read_stream(entry.blob)
        if dec is None:
            raise PmfError(f"corrupt metadata stream @0x{entry.blob:X}")
        attrs = self.parse_attrs(dec)
        data_attr = next(
            (a for a in attrs if a.atype == ATTR_DATA and not a.name), None
        )
        if data_attr is not None and not data_attr.nonres:
            content = dec[data_attr.content_off : data_attr.content_off + data_attr.content_len]
            if len(content) < entry.size:
                content += b"\x00" * (entry.size - len(content))
            yield content[: entry.size]
            return
        # non-resident: data chunks follow the metadata stream
        init_size = data_attr.init_size if data_attr is not None else entry.size
        pos = entry.blob + BLOB_HDR + complen
        produced = 0
        while produced < entry.size:
            self.f.seek(pos)
            chdr = self.f.read(8)
            c_dec = u32(chdr, 0) if len(chdr) == 8 else 0
            c_comp = u32(chdr, 4) if len(chdr) == 8 else 0
            valid = 0 < c_dec <= CHUNK_MAX and 0 < c_comp < (1 << 30)
            chunk = b""
            if valid:
                comp = self.f.read(c_comp)
                if len(comp) == c_comp:
                    if c_comp == c_dec:
                        chunk = comp  # stored raw (incompressible data)
                    else:
                        try:
                            chunk = zlib.decompress(comp)
                        except zlib.error:
                            chunk = b""
            if not chunk:
                if produced >= init_size:
                    break  # rest is an uninitialized/sparse tail -> zeros
                raise PmfError(
                    f"data chunk chain broken @0x{pos:X} "
                    f"({produced:,}/{entry.size:,} bytes recovered)"
                )
            take = min(len(chunk), entry.size - produced)
            yield chunk[:take]
            produced += take
            pos += 8 + c_comp
        if produced < entry.size:
            remaining = entry.size - produced
            zeros = b"\x00" * (1 << 20)
            while remaining > 0:
                n = min(remaining, len(zeros))
                yield zeros[:n]
                remaining -= n

    def read_file(self, entry: Entry, max_bytes: Optional[int] = None) -> bytes:
        out = bytearray()
        for chunk in self.iter_file_data(entry):
            out += chunk
            if max_bytes is not None and len(out) >= max_bytes:
                return bytes(out[:max_bytes])
        return bytes(out)

    # ---------- paths ----------

    def full_path(self, recno: int) -> str:
        parts: list[str] = []
        cur = recno
        seen = set()
        while cur and cur != self.root_recno and cur not in seen:
            seen.add(cur)
            e = self.entries.get(cur)
            if e is None:
                parts.append(f"<#{cur}>")
                break
            parts.append(e.name)
            cur = e.parent
        return "\\".join(reversed(parts))

    def resolve(self, path: str) -> Optional[int]:
        """Resolve a backslash/slash path to a recno. '' or '\\' = root."""
        path = path.strip().replace("/", "\\").strip("\\")
        if path.lower().startswith("c:"):
            path = path[2:].strip("\\")
        if not path:
            return self.root_recno
        cur = self.root_recno
        for part in path.split("\\"):
            nxt = None
            for r in self.children.get(cur, []):
                if self.entries[r].name.lower() == part.lower():
                    nxt = r
                    break
            if nxt is None:
                return None
            cur = nxt
        return cur

    def walk(self, recno: int) -> Iterator[int]:
        """Yield recnos of all descendants (depth-first), including recno itself."""
        stack = [recno]
        while stack:
            r = stack.pop()
            yield r
            stack.extend(reversed(self.children.get(r, [])))


# ---------------- CLI ----------------


def fmt_entry(e: Entry, path: str = "") -> str:
    kind = "<DIR> " if e.is_dir else f"{e.size:>14,}"
    return f"{ft_str(e.mtime)}  {kind:>14}  {path or e.name}"


def cmd_info(img: PmfImage, args) -> None:
    h = img.header
    print(f"Image:       {h.path}")
    print(f"Format:      DiskGenius PMF (VIMG), Backup-All-Files mode")
    print(f"Image size:  {h.file_size:,} bytes ({human(h.file_size)})")
    print(f"Filesystem:  {h.filesystem}")
    print(f"Disk model:  {h.disk_model}")
    print(f"Serial:      {h.serial}")
    print(f"Image name:  {h.image_name}")
    print(f"Created:     {h.created}")
    img.load_index()
    files = sum(1 for e in img.entries.values() if not e.is_dir)
    dirs = len(img.entries) - files
    total = sum(e.size for e in img.entries.values() if not e.is_dir)
    print(f"Entries:     {len(img.entries):,} ({files:,} files, {dirs:,} dirs)")
    print(f"Total data:  {total:,} bytes ({human(total)})")


def cmd_ls(img: PmfImage, args) -> None:
    img.load_index()
    recno = img.resolve(args.path)
    if recno is None:
        raise SystemExit(f"path not found: {args.path}")
    e = img.entries.get(recno)
    if e and not e.is_dir and recno != img.root_recno:
        print(fmt_entry(e, img.full_path(recno)))
        return
    kids = img.children.get(recno, [])
    for r in kids:
        print(fmt_entry(img.entries[r]))
    print(f"\n{len(kids)} entries")


def cmd_tree(img: PmfImage, args) -> None:
    img.load_index()
    recno = img.resolve(args.path)
    if recno is None:
        raise SystemExit(f"path not found: {args.path}")

    def rec(r: int, prefix: str, depth: int) -> None:
        kids = img.children.get(r, [])
        for i, k in enumerate(kids):
            e = img.entries[k]
            last = i == len(kids) - 1
            tag = "" if e.is_dir else f"  ({human(e.size)})"
            print(f"{prefix}{'└── ' if last else '├── '}{e.name}{tag}")
            if e.is_dir and depth > 1:
                rec(k, prefix + ("    " if last else "│   "), depth - 1)

    name = img.full_path(recno) or "\\"
    print(name)
    rec(recno, "", args.depth)


def cmd_find(img: PmfImage, args) -> None:
    img.load_index()
    pat = args.pattern.lower()
    is_glob = any(c in pat for c in "*?[")
    n = 0
    for e in img.entries.values():
        name = e.name.lower()
        hit = fnmatch.fnmatch(name, pat) if is_glob else pat in name
        if hit:
            print(fmt_entry(e, img.full_path(e.recno)))
            n += 1
            if args.limit and n >= args.limit:
                print(f"... (stopped at {args.limit})")
                return
    print(f"\n{n} match(es)")


def _safe_rel(path: str) -> Path:
    parts = [p for p in path.split("\\") if p not in ("", ".", "..")]
    clean = []
    for p in parts:
        p = "".join(c if c not in '<>:"|?*' and ord(c) >= 32 else "_" for c in p)
        clean.append(p.rstrip(" ."))
    return Path(*clean) if clean else Path("_")


def _extract_one(img: PmfImage, e: Entry, target: Path, quiet: bool = False) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    tgt = target
    if os.name == "nt" and len(str(tgt)) > 250 and not str(tgt).startswith("\\\\?\\"):
        tgt = Path("\\\\?\\" + os.path.abspath(str(tgt)))
    try:
        with open(tgt, "wb") as out:
            for chunk in img.iter_file_data(e):
                out.write(chunk)
        try:
            ts = ft_to_unix(e.mtime)
            if ts > 0:
                os.utime(tgt, (ts, ts))
        except (OSError, OverflowError):
            pass
        if not quiet:
            print(f"  {img.full_path(e.recno)}  ({human(e.size)})")
        return True
    except (PmfError, OSError) as ex:
        print(f"  ERROR {img.full_path(e.recno)}: {ex}", file=sys.stderr)
        return False


def cmd_extract(img: PmfImage, args) -> None:
    img.load_index()
    out_dir = Path(args.out)
    targets: list[int] = []
    for spec in args.paths:
        recno = img.resolve(spec)
        if recno is not None:
            targets.append(recno)
            continue
        # try glob against full names
        pat = spec.lower().replace("/", "\\").strip("\\")
        matched = [
            e.recno
            for e in img.entries.values()
            if fnmatch.fnmatch(img.full_path(e.recno).lower(), pat)
        ]
        if not matched:
            raise SystemExit(f"path not found: {spec}")
        targets.extend(matched)

    ok = fail = skip = 0
    for recno in targets:
        base_e = img.entries.get(recno)
        base_parent_path = img.full_path(base_e.parent) if base_e else ""
        for r in img.walk(recno):
            e = img.entries[r]
            full = img.full_path(r)
            rel = full[len(base_parent_path):].lstrip("\\") if base_parent_path and full.startswith(base_parent_path) else full
            dest = out_dir / _safe_rel(rel)
            if e.is_dir:
                dest.mkdir(parents=True, exist_ok=True)
                continue
            if _extract_one(img, e, dest, quiet=args.quiet):
                ok += 1
            else:
                fail += 1
    print(f"\nExtracted {ok} file(s){f', {fail} failed' if fail else ''} -> {out_dir}")


def cmd_cat(img: PmfImage, args) -> None:
    img.load_index()
    recno = img.resolve(args.path)
    if recno is None:
        raise SystemExit(f"path not found: {args.path}")
    e = img.entries[recno]
    if e.is_dir:
        raise SystemExit(f"{args.path} is a directory")
    data = img.read_file(e, max_bytes=args.max_bytes)
    sys.stdout.buffer.write(data)


def cmd_index(img: PmfImage, args) -> None:
    img.load_index(rebuild=args.rebuild)
    print(f"{len(img.entries):,} entries indexed")


def main(argv: Optional[list[str]] = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    p = argparse.ArgumentParser(
        prog="pmfview",
        description="Read-only file viewer/extractor for DiskGenius .pmf images",
    )
    p.add_argument("image", type=Path, help="path to .pmf image")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="image summary")

    sp = sub.add_parser("index", help="build or refresh the file index cache")
    sp.add_argument("--rebuild", action="store_true")

    sp = sub.add_parser("ls", help="list a directory")
    sp.add_argument("path", nargs="?", default="", help="path inside image (default: root)")

    sp = sub.add_parser("tree", help="directory tree")
    sp.add_argument("path", nargs="?", default="")
    sp.add_argument("--depth", type=int, default=2)

    sp = sub.add_parser("find", help="find files by name (substring or glob)")
    sp.add_argument("pattern")
    sp.add_argument("--limit", type=int, default=200)

    sp = sub.add_parser("extract", help="extract files/directories")
    sp.add_argument("paths", nargs="+", help="paths inside image (dirs recurse; globs on full path ok)")
    sp.add_argument("--out", type=Path, default=Path("pmf_extracted"))
    sp.add_argument("--quiet", action="store_true")

    sp = sub.add_parser("cat", help="print file content to stdout")
    sp.add_argument("path")
    sp.add_argument("--max-bytes", type=int, default=None)

    args = p.parse_args(argv)
    img = PmfImage(args.image)
    {
        "info": cmd_info,
        "index": cmd_index,
        "ls": cmd_ls,
        "tree": cmd_tree,
        "find": cmd_find,
        "extract": cmd_extract,
        "cat": cmd_cat,
    }[args.cmd](img, args)


if __name__ == "__main__":
    main()
