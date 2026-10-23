#!/usr/bin/env python3
"""
pmfview — file viewer/extractor/creator for DiskGenius .pmf partition images
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
import contextlib
import fnmatch
import hashlib
import json
import os
import pickle
import gzip
import platform
import subprocess
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

if __name__ == '__main__':
    # The journal module imports pmfview too; keep one set of classes/errors.
    sys.modules['pmfview'] = sys.modules[__name__]

MAGIC = b"VIMG"
CATALOG_START = 0x10000
ENTRY_SIZE = 0x44
BLOB_HDR = 21
NAME_FIELD = 0x208
RECORD_OFF = 0x240
ATTR_SLOT = 0x248
CHUNK_MAX = 0x10000000          # sanity cap for a single data chunk (256 MiB)
WRITE_CHUNK = 1 << 20
FT_MIN = 116444736000000000        # 1970-01-01
FT_MAX = 159000000000000000        # ~2104
INDEX_CACHE_VERSION = 4

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


def unix_to_ft(ts: float) -> int:
    return int((ts + EPOCH_DELTA) * 10_000_000)


def ft_str(ft: int) -> str:
    if not (FT_MIN <= ft <= FT_MAX):
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ft_to_unix(ft)))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n}"


def _clean_ascii(d: bytes) -> str:
    return "".join(chr(b) for b in d if 32 <= b < 127).strip()


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


@dataclass
class CreateEntry:
    recno: int
    parent: int
    source: Optional[Path]
    name: str
    size: int
    attr_flags: int
    crtime: int
    mtime: int
    blob: int = 0

    @property
    def is_dir(self) -> bool:
        return bool(self.attr_flags & FILE_ATTR_DIRECTORY)


class PmfError(Exception):
    pass


class PmfImage:
    def __init__(self, path: Path, revision: Optional[int] = None):
        self.requested_revision = revision
        self.path = Path(path)
        self.f: BinaryIO = self.path.open("rb")
        self.file_stat = os.fstat(self.f.fileno())
        self.file_size = self.file_stat.st_size
        try:
            self.header = self._parse_header()
        except BaseException:
            self.f.close()
            raise
        self.entries: dict[int, Entry] = {}
        self.children: dict[int, list[int]] = {}
        self.root_recno = 5
        self._loaded = False
        self.index_errors = 0
        self.index_issues: list[str] = []
        self.file_hashes: dict[int, str] = {}
        self.revisions: list[dict[str, object]] = []
        self.revision = 0
        self.committed_end = self.file_size
        self.incremental_warning = ''

    def close(self) -> None:
        """Release the image handle."""
        self.f.close()

    # ---------- header ----------

    def _parse_header(self) -> Header:
        self.f.seek(0)
        h = self.f.read(0x400)
        if len(h) < 0x400 or h[:4] != MAGIC:
            raise PmfError(f"{self.path}: not a DiskGenius PMF/VIMG image")
        year = u16(h, 0x274)
        created = (
            f"{year:04d}-{h[0x276]:02d}-{h[0x277]:02d} "
            f"{h[0x278]:02d}:{h[0x279]:02d}:{h[0x27A]:02d}"
        )
        return Header(
            path=self.path,
            file_size=self.file_size,
            filesystem=_clean_ascii(h[0x0C:0x10]),
            disk_model=_clean_ascii(h[0x11:0x31]),
            serial=_clean_ascii(h[0x41:0x51]),
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
        st = self.file_stat
        loaded = False
        from pmf_repair import index_issues
        if not rebuild and cache.exists():
            try:
                with gzip.open(cache, "rb") as fh:
                    payload = pickle.load(fh)
                if (payload.get("version") == INDEX_CACHE_VERSION
                        and payload.get("size") == st.st_size
                        and payload.get("mtime") == st.st_mtime_ns):
                    self.entries = payload["entries"]
                    self.index_errors = payload["index_errors"]
                    self._finish_index()
                    loaded = not index_issues(self.entries)
            except Exception:
                pass
        if not loaded:
            self._build_index(progress=progress)
            try:
                self._save_index_cache(self.entries)
            except OSError as e:
                if progress:
                    print(f"[index] could not write cache: {e}", file=sys.stderr)
        from pmf_update import apply_updates
        try:
            apply_updates(self)
            self.index_issues = index_issues(self.entries)
        except BaseException:
            self._loaded = False
            raise
        if self.index_errors or self.index_issues:
            print(f"[index] Metadata issues: {self.index_errors} unreadable records; "
                  f"{len(self.index_issues)} tree issues. Writes are disabled; use repair-mft.", file=sys.stderr)
        if self.incremental_warning:
            print(f"[index] WARNING: {self.incremental_warning}", file=sys.stderr)

    def _save_index_cache(self, entries: dict[int, Entry]) -> None:
        cache = self._cache_path()
        fd, temporary = tempfile.mkstemp(prefix='.pmf-index-', dir=cache.parent)
        try:
            with os.fdopen(fd, 'wb') as raw:
                with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1) as fh:
                    pickle.dump({"version": INDEX_CACHE_VERSION,
                                 "size": self.file_stat.st_size, "mtime": self.file_stat.st_mtime_ns,
                                 "entries": entries, "index_errors": self.index_errors},
                                fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(temporary, cache)
        finally:
            Path(temporary).unlink(missing_ok=True)

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
        self.index_errors = n_bad + (not entries)
        self.entries = entries
        self._finish_index()
        if progress:
            print(
                f"[index] {len(entries):,} files indexed in {time.time()-t0:.1f}s",
                file=sys.stderr,
            )

    def _finish_index(self) -> None:
        self.entries.setdefault(self.root_recno,
            Entry(self.root_recno, 0, '', 0, FILE_ATTR_DIRECTORY, 0, 0, 0))
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
        """Yield content; a complete read verifies any incremental SHA-256 checksum.

        A caller stopping early receives a preview, not a full integrity check.
        """
        expected = self.file_hashes.get(entry.recno)
        digest = hashlib.sha256() if expected else None
        count = 0
        for chunk in self._iter_file_data_unchecked(entry):
            count += len(chunk)
            if digest is not None:
                digest.update(chunk)
            yield chunk
        if count != entry.size:
            raise PmfError(f'Incomplete file data: {entry.name}')
        if digest is not None and digest.hexdigest() != expected:
            raise PmfError(f'Incremental file checksum failed: {entry.name}')

    def _iter_file_data_unchecked(self, entry: Entry) -> Iterator[bytes]:
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
            if max_bytes is not None and max_bytes < entry.size and len(out) >= max_bytes:
                return bytes(out[:max_bytes])
        return bytes(out)

    def _zlib_range(self, start: int, compressed: int, offset: int, length: int) -> bytes:
        """Decode only a selected stream prefix, with 64 KiB input/output buffers."""
        self.f.seek(start)
        decoder = zlib.decompressobj()
        pending = b''
        remaining, produced = compressed, 0
        end = offset + length
        out = bytearray()
        try:
            while produced < end:
                if not pending and remaining:
                    size = min(remaining, 65536)
                    pending = self.f.read(size)
                    if len(pending) != size:
                        raise PmfError('Truncated compressed data')
                    remaining -= size
                block = decoder.decompress(pending, min(65536, end - produced))
                pending = decoder.unconsumed_tail
                out.extend(block[max(0, offset - produced):])
                produced += len(block)
                if decoder.eof and produced < end:
                    raise PmfError('Compressed data is shorter than the requested range')
                if not block and not pending and not remaining and produced < end:
                    raise PmfError('Incomplete compressed data')
        except zlib.error as exc:
            raise PmfError(f'Corrupt compressed data: {exc}') from exc
        return bytes(out)

    def read_file_range(self, entry: Entry, offset: int, length: int) -> bytes:
        """Read just a byte range; skip earlier chunk payloads with seeks.

        Partial reads do not verify skipped data or the whole-file checksum.
        Memory follows the requested range, not the image/file/chunk size.
        """
        if offset < 0 or length <= 0:
            raise PmfError('offset must be nonnegative and length must be positive')
        if entry.is_dir:
            raise PmfError(f'{entry.name} is a directory')
        if offset >= entry.size:
            return b''
        end = min(entry.size, offset + length)
        header = self._blob_header(entry.blob)
        if header is None:
            raise PmfError(f'unreadable blob @0x{entry.blob:X}')
        _, decoded, compressed = header
        if not 0 < decoded <= 16 << 20:
            raise PmfError('Metadata exceeds the 16 MiB range-read limit')
        position = entry.blob + BLOB_HDR + compressed
        if position > self.committed_end:
            raise PmfError('Metadata stream extends outside the image')
        metadata = self._zlib_range(entry.blob + BLOB_HDR, compressed, 0, decoded)
        attribute = next((a for a in self.parse_attrs(metadata)
                          if a.atype == ATTR_DATA and not a.name), None)
        out = bytearray()
        if attribute is not None and not attribute.nonres:
            content = metadata[attribute.content_off:attribute.content_off + attribute.content_len]
            out.extend(content[offset:end])
            out.extend(b'\x00' * (end - offset - len(out)))
        else:
            initialized = attribute.init_size if attribute is not None else entry.size
            produced = 0
            while produced < end:
                if produced >= initialized:
                    out.extend(b'\x00' * (end - max(offset, produced)))
                    break
                self.f.seek(position)
                chunk_header = self.f.read(8)
                if len(chunk_header) != 8:
                    raise PmfError('Truncated data chunk header')
                chunk_size, stored_size = struct.unpack('<II', chunk_header)
                if not (0 < chunk_size <= CHUNK_MAX and 0 < stored_size < 1 << 30
                        and position + 8 + stored_size <= self.committed_end):
                    raise PmfError(f'Invalid data chunk @0x{position:X}')
                next_position = min(entry.size, produced + chunk_size)
                if next_position > offset:
                    local_start = max(0, offset - produced)
                    count = min(next_position, end) - max(offset, produced)
                    if stored_size == chunk_size:
                        self.f.seek(position + 8 + local_start)
                        block = self.f.read(count)
                        if len(block) != count:
                            raise PmfError('Truncated raw data chunk')
                    else:
                        block = self._zlib_range(position + 8, stored_size, local_start, count)
                    out.extend(block)
                produced = next_position
                position += 8 + stored_size
        result = bytes(out)
        if len(result) != end - offset:
            raise PmfError('Incomplete file range')
        expected = self.file_hashes.get(entry.recno)
        if offset == 0 and end == entry.size and expected:
            if hashlib.sha256(result).hexdigest() != expected:
                raise PmfError(f'Incremental file checksum failed: {entry.name}')
        return result

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
        seen: set[int] = set()
        while stack:
            r = stack.pop()
            if r in seen:
                continue
            seen.add(r)
            yield r
            stack.extend(reversed(self.children.get(r, [])))


# ---------------- CLI ----------------

def _write_utf16z(buf: bytearray, off: int, maxbytes: int, s: str) -> None:
    raw = s.encode("utf-16-le", errors="replace")[: maxbytes - 2]
    if len(raw) % 2:
        raw = raw[:-1]
    buf[off : off + len(raw)] = raw


def _sanitize_image_name(name: str) -> str:
    clean = "".join(c if c not in '<>:"/\\|?*' and ord(c) >= 32 else "_" for c in name)
    return clean.rstrip(" .") or "folder"


def _unique_name(name: str, used: set[str]) -> str:
    base = _sanitize_image_name(name)
    candidate = base
    stem = Path(base).stem or base
    suffix = Path(base).suffix
    n = 2
    while candidate.lower() in used:
        candidate = f"{stem} ({n}){suffix}"
        n += 1
    used.add(candidate.lower())
    return candidate


def _make_header(out_path: Path, image_name: str) -> bytes:
    h = bytearray(CATALOG_START)
    h[:4] = MAGIC
    h[0x0C:0x10] = b"NTFS"
    model = f"pmfview {platform.system()}".encode("ascii", errors="ignore")[:0x20]
    serial = b"PMFVIEW"
    h[0x11 : 0x11 + len(model)] = model
    h[0x41 : 0x41 + len(serial)] = serial
    _write_utf16z(h, 0x252, 0x20, image_name or out_path.stem)
    now = time.localtime()
    struct.pack_into("<H", h, 0x274, now.tm_year)
    h[0x276] = now.tm_mon
    h[0x277] = now.tm_mday
    h[0x278] = now.tm_hour
    h[0x279] = now.tm_min
    h[0x27A] = now.tm_sec
    return bytes(h)


def _run_vss_script(script: str) -> str:
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise PmfError(f"could not start Volume Shadow Copy: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PmfError(
            "hot image snapshot failed; run PMF Viewer as administrator"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.strip()


def _create_volume_snapshot(volume: str) -> tuple[str, str]:
    escaped = volume.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        f"$r=Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create "
        f"-Arguments @{{Volume='{escaped}';Context='ClientAccessible'}};"
        "if($r.ReturnValue -ne 0){throw ('VSS returned '+$r.ReturnValue)};"
        "$s=Get-CimInstance -ClassName Win32_ShadowCopy | "
        "Where-Object {$_.ID -eq $r.ShadowID};"
        "if(-not $s){throw 'VSS snapshot was not found after creation'};"
        "[pscustomobject]@{Id=$s.ID;DeviceObject=$s.DeviceObject} | "
        "ConvertTo-Json -Compress"
    )
    raw = _run_vss_script(script)
    try:
        data = json.loads(raw)
        return str(data["Id"]), str(data["DeviceObject"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PmfError(f"invalid response from Volume Shadow Copy: {raw}") from exc


def _delete_volume_snapshot(snapshot_id: str) -> None:
    escaped = snapshot_id.replace("'", "''")
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$id='{escaped}';"
        "Get-CimInstance -ClassName Win32_ShadowCopy | "
        "Where-Object {$_.ID -eq $id} | Remove-CimInstance"
    )
    try:
        _run_vss_script(script)
    except PmfError:
        pass


@contextlib.contextmanager
def _hot_snapshot_sources(sources: list[Path]):
    if os.name != "nt":
        raise PmfError("hot image snapshots are currently supported only on Windows")

    originals = [p.resolve() for p in sources]
    volumes: dict[str, tuple[str, str]] = {}
    try:
        for source in originals:
            drive = source.drive
            if not drive or drive.startswith("\\"):
                raise PmfError(
                    f"hot image requires a local Windows volume: {source}"
                )
            volume = drive.rstrip("\\/") + "\\"
            key = volume.lower()
            if key not in volumes:
                volumes[key] = _create_volume_snapshot(volume)

        snapshot_sources: list[Path] = []
        for source in originals:
            volume = source.drive.rstrip("\\/") + "\\"
            _snapshot_id, device = volumes[volume.lower()]
            relative = os.path.relpath(source, volume)
            snapshot_sources.append(
                Path(device + "\\" + ("" if relative == "." else relative))
            )
        yield snapshot_sources
    finally:
        for snapshot_id, _device in volumes.values():
            _delete_volume_snapshot(snapshot_id)


def _build_create_entries(
    sources: list[Path], top_names: Optional[list[str]] = None
) -> list[CreateEntry]:
    entries: list[CreateEntry] = []
    path_recno: dict[Path, int] = {}
    root_names: set[str] = set()
    next_recno = 6

    def add_entry(path: Path, parent: int, name: str) -> int:
        nonlocal next_recno
        st = path.stat()
        is_dir = path.is_dir()
        recno = next_recno
        next_recno += 1
        ft_m = unix_to_ft(st.st_mtime)
        ft_c = unix_to_ft(getattr(st, "st_ctime", st.st_mtime))
        entries.append(CreateEntry(
            recno=recno,
            parent=parent,
            source=path,
            name=name,
            size=0 if is_dir else st.st_size,
            attr_flags=FILE_ATTR_DIRECTORY if is_dir else 0,
            crtime=ft_c,
            mtime=ft_m,
        ))
        path_recno[path] = recno
        return recno

    if top_names is not None and len(top_names) != len(sources):
        raise ValueError("top_names must match sources")

    for index, src in enumerate(sources):
        src = src.resolve()
        if not src.exists():
            raise PmfError(f"source not found: {src}")
        requested_name = top_names[index] if top_names is not None else (
            src.name or src.drive.rstrip(":")
        )
        top_name = _unique_name(requested_name, root_names)
        add_entry(src, 5, top_name)
        if not src.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            cur_path = Path(dirpath)
            cur_recno = path_recno[cur_path]
            dirnames[:] = sorted(
                [d for d in dirnames if not (cur_path / d).is_symlink()],
                key=str.lower,
            )
            for dirname in dirnames:
                child = cur_path / dirname
                add_entry(child, cur_recno, _sanitize_image_name(dirname))
            for filename in sorted(filenames, key=str.lower):
                child = cur_path / filename
                if child.is_symlink():
                    continue
                add_entry(child, cur_recno, _sanitize_image_name(filename))
    if not entries:
        raise PmfError("no source files or folders to write")
    return entries


def _make_metadata(e: CreateEntry) -> bytes:
    attrs_off = 0x30
    attr_count_pos = RECORD_OFF + attrs_off
    attr_pos = attr_count_pos + 4
    min_len = attr_pos + (0 if e.is_dir else ATTR_SLOT + 8)
    dec = bytearray(min_len)
    _write_utf16z(dec, 0, NAME_FIELD, e.name)
    struct.pack_into("<Q", dec, 0x208, e.parent)
    struct.pack_into("<Q", dec, 0x210, e.size)
    struct.pack_into("<Q", dec, 0x218, e.crtime)
    struct.pack_into("<Q", dec, 0x220, e.mtime)
    struct.pack_into("<Q", dec, 0x228, e.mtime)
    struct.pack_into("<I", dec, 0x230, e.attr_flags)
    struct.pack_into("<Q", dec, 0x238, e.recno)
    dec[RECORD_OFF : RECORD_OFF + 4] = b"FILE"
    struct.pack_into("<H", dec, RECORD_OFF + 0x14, attrs_off)
    struct.pack_into("<H", dec, RECORD_OFF + 0x16, MFT_FLAG_DIR if e.is_dir else 0)
    struct.pack_into("<I", dec, attr_count_pos, 0 if e.is_dir else 1)
    if not e.is_dir:
        struct.pack_into("<I", dec, attr_pos, ATTR_DATA)
        dec[attr_pos + 8] = 1
        struct.pack_into("<Q", dec, attr_pos + 0x30, e.size)
        struct.pack_into("<Q", dec, attr_pos + 0x38, e.size)
    return bytes(dec)


def _write_blob(out: BinaryIO, e: CreateEntry) -> None:
    meta = _make_metadata(e)
    comp = zlib.compress(meta, level=6)
    out.write(struct.pack("<II B II I", e.recno & 0xFFFFFFFF, 0, 1, len(meta), len(meta), len(comp)))
    out.write(comp)
    if e.is_dir or e.source is None:
        return
    with e.source.open("rb") as fh:
        while True:
            chunk = fh.read(WRITE_CHUNK)
            if not chunk:
                break
            c = zlib.compress(chunk, level=6)
            if len(c) >= len(chunk):
                c = chunk
            out.write(struct.pack("<II", len(chunk), len(c)))
            out.write(c)


def create_pmf_image(
    out_path: Path,
    sources: list[Path],
    image_name: str = "",
    overwrite: bool = False,
    progress=None,
    hot_image: bool = False,
) -> tuple[int, int, int]:
    out_path = Path(out_path)
    sources = [Path(p) for p in sources]
    if out_path.exists() and not overwrite:
        raise PmfError(f"output already exists: {out_path}")

    original_names = [p.resolve().name or p.resolve().drive.rstrip(":")
                      for p in sources]
    source_context = (
        _hot_snapshot_sources(sources)
        if hot_image else contextlib.nullcontext(sources)
    )
    with source_context as read_sources:
        entries = _build_create_entries(read_sources, top_names=original_names)
        files = sum(1 for e in entries if not e.is_dir)
        total = sum(e.size for e in entries if not e.is_dir)
        catalog_bytes = max(len(entries) * ENTRY_SIZE, ENTRY_SIZE)
        first_blob = CATALOG_START + ((catalog_bytes + 15) // 16) * 16
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as out:
            out.write(_make_header(out_path, image_name))
            out.seek(first_blob)
            for i, e in enumerate(entries, 1):
                e.blob = out.tell()
                _write_blob(out, e)
                if progress and (i == len(entries) or i % 100 == 0):
                    progress(i, len(entries), e)
            out.seek(CATALOG_START)
            for e in entries:
                row = bytearray(ENTRY_SIZE)
                struct.pack_into("<Q", row, 0x00, e.recno)
                struct.pack_into("<Q", row, 0x08, e.mtime)
                struct.pack_into("<Q", row, 0x10, e.mtime)
                struct.pack_into("<Q", row, 0x18, e.size)
                struct.pack_into("<Q", row, 0x20, e.blob)
                struct.pack_into("<Q", row, 0x2C, e.crtime)
                struct.pack_into("<I", row, 0x34, e.recno & 0xFFFFFFFF)
                out.write(row)
        return files, len(entries) - files, total



def fmt_entry(e: Entry, path: str = "") -> str:
    kind = "<DIR> " if e.is_dir else f"{e.size:>14,}"
    return f"{ft_str(e.mtime)}  {kind:>14}  {path or e.name}"


def cmd_info(img: PmfImage, args) -> None:
    h = img.header
    print(f"Image:       {h.path}")
    print("Format:      DiskGenius PMF (VIMG), Backup-All-Files mode")
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
    print(f"Revision:    {img.revision}")


def cmd_history(img: PmfImage, args) -> None:
    img.load_index()
    for row in img.revisions or [{"revision": 0, "created": img.header.created,
                                 "summary": "Original image"}]:
        print(f"{row['revision']}: {row['created']}  {row['summary']}")


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


def extraction_target(out_dir: Path, relative: str) -> Path:
    """Keep extraction inside its chosen destination, including through symlinks."""
    root = out_dir.resolve()
    dest = root / _safe_rel(relative.replace('/', '\\')) if relative.strip('\\/') else root
    if not dest.resolve().is_relative_to(root):
        raise PmfError(f'Extraction path escapes destination: {relative}')
    return dest


def _extended_path(path: Path) -> Path:
    if os.name != 'nt':
        return path
    text = os.path.abspath(path)
    prefix = "\\\\?\\"
    if text.startswith(prefix):
        return Path(text)
    if text.startswith("\\\\"):
        return Path(prefix + "UNC\\" + text[2:])
    return Path(prefix + text)


def make_extraction_directory(path: Path) -> None:
    _extended_path(path).mkdir(parents=True, exist_ok=True)


def extract_file(img: PmfImage, entry: Entry, target: Path) -> None:
    """Publish extracted bytes only after a complete read/checksum succeeds."""
    protected = (img.path, img._cache_path(), img.path.with_name(img.path.name + '.update.lock'))
    if target.resolve() in {p.resolve() for p in protected}:
        raise PmfError('Extraction would overwrite the source image or its state files')
    target = _extended_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pmf-extract-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            for chunk in img.iter_file_data(entry):
                out.write(chunk)
        ts = ft_to_unix(entry.mtime)
        if ts > 0:
            try:
                os.utime(temporary, (ts, ts))
            except (OSError, OverflowError):
                pass
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _extract_one(img: PmfImage, e: Entry, target: Path, quiet: bool = False) -> bool:
    try:
        extract_file(img, e, target)
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

    ok = fail = 0
    for recno in targets:
        base_e = img.entries.get(recno)
        base_parent_path = img.full_path(base_e.parent) if base_e else ""
        for r in img.walk(recno):
            e = img.entries[r]
            full = img.full_path(r)
            rel = full[len(base_parent_path):].lstrip("\\") if base_parent_path and full.startswith(base_parent_path) else full
            dest = extraction_target(out_dir, rel)
            if e.is_dir:
                make_extraction_directory(dest)
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


def cmd_create(args) -> None:
    files, dirs, total = create_pmf_image(
        args.image,
        args.sources,
        image_name=args.name or args.image.stem,
        overwrite=args.force,
        hot_image=args.hot,
        progress=None if args.quiet else lambda n, total_entries, e: print(
            f"[create] {n:,}/{total_entries:,} entries", file=sys.stderr
        ),
    )
    if not args.quiet:
        print(
            f"Created {args.image} with {files:,} file(s), {dirs:,} folder(s), "
            f"{human(total)}"
        )


def cmd_update(args) -> None:
    from pmf_update import PmfError as UpdateError, update_pmf_image

    try:
        result = update_pmf_image(
            args.image, args.sources, hot_image=args.hot, image_root=args.image_root,
            dry_run=args.dry_run, delete_missing=not args.keep_missing,
            expected_revision=args.expected_revision,
            progress=None if args.quiet else lambda text: print(text, file=sys.stderr),
        )
    except (UpdateError, OSError) as exc:
        raise SystemExit(f"Update cancelled: {exc}") from exc
    print(f"{'Preview' if result.dry_run else 'Updated'}: {result.added} added, "
          f"{result.changed} changed, {result.deleted} deleted, {result.unchanged} unchanged")
    print(f"Revision {result.revision}; appended {result.written:,} bytes")
    if result.dry_run:
        print("No image data was changed.")


def cmd_stat(img: PmfImage, args) -> None:
    img.load_index()
    recno = img.resolve(args.path)
    if recno is None:
        raise PmfError(f'Path not found: {args.path}')
    e = img.entries[recno]
    print(json.dumps({'path': img.full_path(recno), 'directory': e.is_dir,
        'size': e.size, 'created': ft_str(e.crtime), 'modified': ft_str(e.mtime),
        'record': recno, 'parent': e.parent, 'blob': e.blob, 'revision': img.revision}, indent=2))


def cmd_hash(img: PmfImage, args) -> None:
    img.load_index()
    recno = img.resolve(args.path)
    if recno is None or img.entries[recno].is_dir:
        raise PmfError(f'File not found: {args.path}')
    digest = hashlib.new(args.algorithm)
    for chunk in img.iter_file_data(img.entries[recno]):
        digest.update(chunk)
    print(f'{args.algorithm}({img.full_path(recno)}) = {digest.hexdigest()}')


def cmd_read(img: PmfImage, args) -> None:
    img.load_index()
    if args.offset < 0 or args.length <= 0:
        raise PmfError('offset must be nonnegative and length must be positive')
    recno = img.resolve(args.path)
    if recno is None or img.entries[recno].is_dir:
        raise PmfError(f'File not found: {args.path}')
    entry = img.entries[recno]
    pos, end = 0, args.offset + args.length
    for chunk in img.iter_file_data(entry):
        following = pos + len(chunk)
        if following > args.offset:
            sys.stdout.buffer.write(chunk[max(0, args.offset-pos):max(0, end-pos)])
        pos = following
        if pos >= end and end < entry.size:
            break


def main(argv: Optional[list[str]] = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    p = argparse.ArgumentParser(
        prog="pmfview",
        description="File viewer/extractor/creator for DiskGenius .pmf images",
    )
    p.add_argument("image", type=Path, help="path to .pmf image")
    p.add_argument("--revision", type=int, help="read an older revision (0 = original)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="image summary")
    sub.add_parser("history", help="list committed revisions")
    sp = sub.add_parser('repair-mft', help='check/rebuild MFT-derived PMF index; no NTFS volume repair')
    sp.add_argument('--apply', action='store_true', help='rebuild the cache if metadata validates')
    sp = sub.add_parser('stat', help='file/directory metadata')
    sp.add_argument('path')
    sp = sub.add_parser('hash', help='hash and verify an entire file')
    sp.add_argument('path')
    sp.add_argument('--algorithm', choices=('sha256', 'sha1', 'md5'), default='sha256')
    sp = sub.add_parser('read', help='read a byte range to stdout; use cat for the whole file')
    sp.add_argument('path')
    sp.add_argument('--offset', type=int, default=0)
    sp.add_argument('--length', type=int, default=16384)
    sp = sub.add_parser('write', help='create/replace complete text or base64 file contents')
    sp.add_argument('path', help='target path in image; parent folder must exist')
    sp.add_argument('content', help='complete UTF-8 text, or base64 with --encoding base64')
    sp.add_argument('--encoding', choices=('utf-8', 'base64'), default='utf-8')
    sp.add_argument('--expected-revision', type=int)
    sp = sub.add_parser('import', help='create/replace an image file from a local file')
    sp.add_argument('source', type=Path)
    sp.add_argument('path', help='target path in image; parent folder must exist')
    sp.add_argument('--expected-revision', type=int)

    sp = sub.add_parser('mount', help='mount as a Windows drive (optional WinFsp dependency)')
    sp.add_argument('drive', help='unused drive letter, e.g. P:')
    sp.add_argument('--read-write', action='store_true', help='enable saves into append-only PMF revisions')
    sp = sub.add_parser('mount-status', help='show mounts for this image')
    sp.add_argument('drive', nargs='?')
    for command, help_text in (('flush-mount', 'commit pending mount writes'),
                                ('unmount', 'flush and remove a mounted drive')):
        sp = sub.add_parser(command, help=help_text)
        sp.add_argument('drive')

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

    sp = sub.add_parser("create", help="create a PMF image from local files/folders")
    sp.add_argument("sources", nargs="+", type=Path, help="local files/folders to include")
    sp.add_argument("--name", default="", help="image name stored in the header")
    sp.add_argument("--force", action="store_true", help="overwrite output image if it exists")
    sp.add_argument("--hot", action="store_true",
                    help="read from Windows Volume Shadow Copy snapshots")
    sp.add_argument("--quiet", action="store_true")

    sp = sub.add_parser("update", help="append an incremental revision to an existing PMF")
    sp.add_argument("sources", nargs="+", type=Path, help="live folders matching folders inside the image")
    sp.add_argument("--image-root", action="store_true",
                    help="map the contents of one source folder/drive to the image root")
    sp.add_argument("--hot", action="store_true", help="read a Windows VSS snapshot (administrator)")
    sp.add_argument("--dry-run", action="store_true", help="compare and report without writing image data")
    sp.add_argument("--keep-missing", action="store_true", help="retain files missing from source")
    sp.add_argument("--expected-revision", type=int)
    sp.add_argument("--quiet", action="store_true")

    args = p.parse_args(argv)
    if args.revision is not None and args.cmd in ("update", "create", "write", "import", "repair-mft"):
        p.error("--revision applies only to reading existing images")
    if args.cmd in ('mount', 'mount-status', 'flush-mount', 'unmount'):
        from pmf_mount import control_mount, list_mounts, start_mount
        if args.cmd == 'mount':
            mount_result: object = start_mount(args.image, args.drive, read_only=not args.read_write,
                                       revision=args.revision)
        elif args.cmd == 'mount-status':
            mount_result = (control_mount(args.drive, 'status', image_path=args.image)
                            if args.drive else list_mounts(args.image))
        else:
            mount_result = control_mount(args.drive, 'flush' if args.cmd == 'flush-mount' else 'unmount',
                                         image_path=args.image)
        print(json.dumps(mount_result, indent=2))
        return
    if args.cmd == 'repair-mft':
        from pmf_repair import repair_mft_index
        result = repair_mft_index(args.image, repair=args.apply)
        print(json.dumps(result, indent=2))
        if result['status'] == 'needs_recovery':
            raise SystemExit(2)
        return
    if args.cmd in ('write', 'import'):
        from pmf_update import put_pmf_file, write_pmf_file
        if args.cmd == 'write':
            written = write_pmf_file(args.image, args.path, args.content, encoding=args.encoding,
                                     expected_revision=args.expected_revision)
        else:
            written = put_pmf_file(args.image, args.path, args.source,
                                   expected_revision=args.expected_revision)
        print(f'Revision {written.revision}: {written.added} added, {written.changed} changed; '
              f'{written.written:,} bytes appended')
        return
    if args.cmd == "update":
        cmd_update(args)
        return
    if args.cmd == "create":
        cmd_create(args)
        return
    img = PmfImage(args.image, revision=args.revision)
    try:
        {
            "info": cmd_info,
            "history": cmd_history,
            "stat": cmd_stat,
            "hash": cmd_hash,
            "read": cmd_read,
            "index": cmd_index,
            "ls": cmd_ls,
            "tree": cmd_tree,
            "find": cmd_find,
            "extract": cmd_extract,
            "cat": cmd_cat,
        }[args.cmd](img, args)
    finally:
        img.close()


if __name__ == "__main__":
    Entry.__module__ = 'pmfview'
    try:
        main()
    except (PmfError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
