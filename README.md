# pmfview — DiskGenius .pmf image viewer / extractor

Read-only browser and extractor for **DiskGenius partition backup images**
(`.pmf`, "VIMG" container, created with *Backup Partition → Backup All Files*).
Lets you list, search, and extract files from a backup without DiskGenius and
without restoring the whole partition.

No dependencies beyond Python 3.9+ (GUI uses stdlib tkinter).

## Usage

```
# CLI
python pmfview.py backup.pmf info
python pmfview.py backup.pmf ls "Users\me\Documents"
python pmfview.py backup.pmf tree "Program Files" --depth 2
python pmfview.py backup.pmf find "*.kdbx"
python pmfview.py backup.pmf extract "Users\me\Documents" --out D:\restored
python pmfview.py backup.pmf cat "Windows\System32\drivers\etc\hosts"

# GUI (tree browser + search + extract)
python pmfview_gui.py backup.pmf
```

- The first open builds an index cache (`<image>.pmfindex`, ~1 min for a 1.3M-file
  image); subsequent opens load in seconds. `index --rebuild` forces a refresh.
- `extract` accepts directories (recursive), multiple paths, and full-path globs
  (e.g. `"Users\*\Documents\*.xlsx"`). File modification times are preserved.
- Paths are case-insensitive; a leading `C:\` is accepted and ignored.

## Verification

Extraction was validated against the live source volume of two real backup
images (~111 GB, ~1.35 M files each): 460 randomly sampled files
(0 B – 202 MB, including multi-chunk, raw-stored, and resident files) —
all SHA-256 hashes match the originals.

## Reverse-engineered format notes (VIMG, "Backup All Files" mode)

There is no public documentation for this format; everything below was
reverse-engineered from real images and is implemented in `pmfview.py`.

```
Image layout
  0x0000    Header: "VIMG" magic, filesystem tag @0x0C, disk model @0x11,
            serial @0x41, image name (UTF-16) @0x252, created y/m/d/h/m/s @0x274.
  0x10000   Catalog: packed 0x44-byte entries (array may start at any 2-byte
            offset; empty slots have blob = 0):
              +0x00 u64  NTFS $LogFile LSN of the file record (unique id)
              +0x08 u64  FILETIME (modified)     +0x10 u64 FILETIME
              +0x18 u64  file size               +0x20 u64 blob offset
              +0x2C u64  FILETIME (created)      +0x34 u32 MFT record number
  blobs     From the first blob offset (which bounds the catalog) to EOF,
            one blob per file, in catalog order:
              [21-byte header][zlib metadata stream][data chunks ...]
            Blob header: u32 recno, u32 0, u8 alg, u32 declen ×2, u32 complen.
            Data chunks (non-resident $DATA only): repeated
              [u32 declen][u32 complen][payload]
            payload is zlib, or raw when complen == declen (incompressible).

Metadata stream (decompressed)
  0x0000    file name, UTF-16LE (0x208-byte field)
  0x0208    u64 parent MFT reference        0x0210  u64 file size
  0x0218    FILETIME created/modified/accessed
  0x0230    u32 attribute flags (0x10000000 = directory)
  0x0238    u64 self MFT reference
  0x0240    NTFS FILE record header, then attributes in fixed 0x248-byte slots:
              slot S:      attr header (0x18 resident / 0x40 non-resident form),
                           attr name (UTF-16) at S+0x48 when named
              S+0x248:     u64 self-ref marker, resident content bytes,
                           then the next attr header
            Non-resident attributes contribute no content to the stream and
            their run lists are zeroed; non-resident $DATA content lives in the
            blob's data chunks. Small-file data is resident $DATA content.
```

The directory tree is reconstructed purely from each entry's
name + parent MFT reference (root is MFT #5) — `$I30` indexes are not needed.

## Files

- `pmfview.py` — library + CLI (the actual parser)
- `pmfview_gui.py` — tkinter GUI on top of pmfview

## Limitations

- "Backup All Files" mode images only (file-level backups). Sector-mode
  backups are a different layout and not supported.
- Alternate data streams (ADS) are not extracted — main file content only.
- Read-only by design; nothing is ever written to the image.
