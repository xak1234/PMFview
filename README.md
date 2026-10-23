# pmfview — DiskGenius .pmf image viewer / extractor / creator

Browser, extractor, and creator for **DiskGenius partition backup images**
(`.pmf`, "VIMG" container, created with *Backup Partition → Backup All Files*).
Lets you list, search, extract files from a backup without DiskGenius, and create
new PMF images from local folders.

CLI and GUI require Python 3.10+ and the standard library (GUI uses tkinter).
MCP additionally requires `mcp>=1.12,<2` (tested with 1.27.2 on Windows).

## Usage

```
# CLI
python pmfview.py backup.pmf info
python pmfview.py backup.pmf ls "Users\me\Documents"
python pmfview.py backup.pmf tree "Program Files" --depth 2
python pmfview.py backup.pmf find "*.kdbx"
python pmfview.py backup.pmf extract "Users\me\Documents" --out D:\restored
python pmfview.py backup.pmf cat "Windows\System32\drivers\etc\hosts"
python pmfview.py new-backup.pmf create "D:\Photos" "D:\Documents"
python pmfview.py hot-backup.pmf create "C:\Work" --hot  # Windows VSS; run as administrator

# GUI (tree browser + search + extract + create)
python pmfview_gui.py backup.pmf
```

## Incremental updates in the same image

Open an image in the GUI and select **Incremental Update…**. Add the source
folders that match top-level folders in the image. Enable **Source contents
match image root** for a single source drive/folder whose children correspond
to the image root. Use **Preview changes only** to inspect counts first.
**Mirror deletions** removes missing source paths from the current view; it
keeps their bytes in older revisions. Unselected image folders are preserved.

```powershell
python pmfview.py backup.pmf update "D:\Documents" --dry-run
python pmfview.py backup.pmf update "D:\Documents"
# For a whole partition image whose root matches D:\:
python pmfview.py backup.pmf update "D:\" --image-root --hot
python pmfview.py backup.pmf history
python pmfview.py backup.pmf --revision 0 extract "Documents" --out E:\original-files
```

Updates append changed **whole-file contents** and a checksummed revision
manifest to the existing `.pmf`; they do not rebuild/copy its existing data.
Unchanged file contents retain their blob offsets. Every source file is hashed
and compared to the image, so the scan still reads unchanged files. This is
file-level incremental storage, not changed-sector/block tracking. A no-op
update does not grow the image. The image must be outside the selected sources.

The viewer, CLI and MCP open the latest committed revision. GUI **Versions…**
opens an earlier revision; **Refresh Latest** returns to current contents.
Revision 0 is the original image. Old contents remain stored, so file deletion
does not reclaim disk space. There is no automatic pruning or compaction.

Changed data is read back and SHA-256 verified before a commit footer is flushed.
Full reads/hashes/extractions verify incremental file checksums. Partial previews
do not verify the entire file. Extraction uses a temporary output file and only
replaces the destination after a complete successful read.

If a write fails normally, its uncommitted tail is removed. After a process/power
interruption, readers ignore trailing uncommitted bytes and report a warning.
An `.update.lock` prevents simultaneous PMFViewer writers. An abandoned lock or
unfinished tail requires recovery before another update; stop the writer and
preserve a copy before inspecting it. Do not remove a lock from an active writer
or edit the same image with other tools concurrently. Storage hardware still
needs to honour flush requests; this is not a substitute for a separate backup.

These appended revisions are a **PMFViewer extension**, not the undocumented
DiskGenius incremental format. Use this updated viewer/MCP for the current view;
a different PMF reader may only see the original base. DiskGenius restore and
bootable full-system restore are not validated. Original bytes remain unchanged.

## Optional Windows drive mounting

In the viewer, select **Mount Image…**, choose a free drive letter and click
**Mount**. Read-only is the default. Enable **Read/write** to edit files from
Explorer or other applications. The same dialog shows status, errors, pending
changes, **Flush changes** and **Unmount**. Close open files before unmounting.
Closing the viewer leaves its mounted drives running until explicitly unmounted.

The Windows distribution includes the Python runtime and bindings. On another
PC, use the official installer download link in `Optional-Mounting` once, only if
you need mounting. The driver/DLL are installed separately and are not bundled. For a source checkout, use Windows x64 / Python 3.10, install
[WinFsp 2.1](https://github.com/winfsp/winfsp/releases/tag/v2.1), then run:

```powershell
python -m pip install -r requirements-mount.txt
python pmfview.py backup.pmf mount P:                 # read-only
python pmfview.py backup.pmf mount P: --read-write    # choose an unused letter
python pmfview.py backup.pmf mount-status
python pmfview.py backup.pmf flush-mount P:
python pmfview.py backup.pmf unmount P:
python pmfview.py backup.pmf --revision 0 mount P:    # historical read-only view
```

Mounted saves, file creation, folder creation, renames and deletions append
verified PMFViewer revisions. File flush/close commits pending changes; **Flush
changes** provides an explicit confirmation of the saved revision. A writable
mount owns the image writer lock for its lifetime. Direct CLI/GUI/MCP image
updates are refused until it is unmounted; failed MCP requests remain recorded
and can be explicitly retried. Image reads see the last committed revision.
Mount commands themselves use the persistent MCP queue.

The mount caches accessed files on disk under `%LOCALAPPDATA%\PMFViewer\mounts`;
it does not extract the whole image. First access decompresses and verifies that
whole file, so very large files can be slow and require equivalent free cache
space. Random edits still append the whole changed file when committed. Each
image supports up to 10,000 checkpoints, including the original; further writes
are refused at that boundary. Create a new image from the latest extracted view
when necessary. This mount is for file access, not booting or running a live OS.

Status reports the cache/recovery folder. If saving fails, subsequent writes and
unmount are refused, the error remains visible, and `pending.json` plus numbered
`.data` files are retained. An unexpected process/power failure may leave an
`.update.lock`; preserve recovery files and confirm the old process has stopped
before recovery. Pending cache contents are not replayed automatically. Do not
kill a mount while it is saving. `PMF_MOUNT_DIR` can select another local cache
location before starting the application.

MCP clients may stop their server and child mount processes together. Close files
and unmount before stopping that MCP server; use the desktop GUI for mounts that
need to outlive an agent session.

Mounts belong to the Windows user/session that started them. Run a desktop MCP
server as that user to make its mounts accessible from Explorer. Mount permissions
allow that user, administrators and SYSTEM; original NTFS ACLs, ownership, alternate
streams, hard links, reparse points and separate access/change times are not
reproduced. Creation/modification times and basic attributes are supported.
WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos.
Project and licence: https://github.com/winfsp/winfsp

Mounting remains optional; browsing/extraction and ordinary image updates do not
require WinFsp.

## Windows distribution and rebuilding

The separate `PMFViewer` folder is portable: copy the entire folder, keeping
`_internal` beside the executables. Double-click `PMFViewer.exe`. Use `pmf.exe`
for the same CLI commands shown here, replacing `python pmfview.py`.
`pmf.exe --mcp` runs the MCP server and `pmf.exe --queue` runs queue commands.
See `START-HERE.txt` in the distribution. The optional driver installer is signed
by its vendor; the application executables are locally built and unsigned.

Build on Windows with Python 3.10 in an isolated virtual environment:

```powershell
python -m pip install -r requirements-build.txt
python build_distribution.py E:\releases\pmf
```

The spec builds a GUI and console executable sharing the same `_internal` runtime.
The distribution also includes source, dependency versions, third-party notices,
checksums and the official WinFsp installer download link. Personal images, caches, queue data,
local settings and repository metadata are excluded.

## MCP server

`pmfview_mcp.py` exposes the same capabilities as MCP tools (stdio transport),
so an AI agent can read, extract, create and update files inside an image.
Install the supported SDK with `pip install "mcp>=1.12,<2"`. Register with Claude Code:

```
claude mcp add pmfview --scope user -- python path\to\pmfview_mcp.py
```

Tools: `pmf_open`, `pmf_info`, `pmf_ls`, `pmf_tree`, `pmf_find`, `pmf_stat`,
`pmf_read` (inline text/hex preview), `pmf_hash` (md5/sha1/sha256), and
`pmf_extract` (recursive, glob-capable, preserves mtimes). All tools take the
image path and open/index images on first use. Cached handles refresh when an
image changes. Operations are serialized so shared file handles remain safe.

New tools:

| Tool | Operation |
| --- | --- |
| `pmf_mount(image_path, drive, read_only=True, revision=None)` | Start an optional Windows drive mount. |
| `pmf_mount_status(image_path)` | List mounts, revisions, errors and recovery folders. |
| `pmf_flush_mount(image_path, drive)` | Commit pending mount changes. |
| `pmf_unmount(image_path, drive)` | Flush and unmount; refuses while files remain open. |
| `pmf_history(image_path)` | List committed revisions. |
| `pmf_write(image_path, path, content, encoding="utf-8", expected_revision=None)` | Create/replace complete file contents. Use `base64` for binary; up to 1 MiB decoded. |
| `pmf_import(image_path, source_path, path, expected_revision=None)` | Stream a local file into the image; suitable for large files. |
| `pmf_update(image_path, sources, image_root=False, hot_image=False, delete_missing=False, dry_run=True, expected_revision=None)` | Preview or commit a folder backup. Set `dry_run=False` to write. |

`pmf_write` and `pmf_import` require an existing parent folder and preserve other
paths. `expected_revision`, obtained from `pmf_open`, rejects stale edits rather
than overwriting a newer revision. `pmf_read` and `pmf_extract` accept optional
`revision=0` (or another history number); omit it for latest. `pmf_update`
defaults to keeping missing files; explicitly set `delete_missing=True` to mirror
deletions. CLI/GUI folder updates mirror deletions by default.

Example tool calls:

```json
{"name":"pmf_write","arguments":{"image_path":"E:\\backups\\work.pmf","path":"Documents\\notes.txt","content":"Updated notes","expected_revision":0}}
{"name":"pmf_read","arguments":{"image_path":"E:\\backups\\work.pmf","path":"Documents\\notes.txt"}}
```

After a successful first write, expect `Revision 1: ...`; reading the path returns
`Updated notes`. Restart an already-running MCP server to load the new tools.
The stdio server uses its process account's file access and is not a network
service; no drive-letter mount is required.

- The first open builds an index cache (`<image>.pmfindex`, ~1 min for a 1.3M-file
  image); subsequent opens load in seconds. `index --rebuild` forces a refresh.
- `extract` accepts directories (recursive), multiple paths, and full-path globs
  (e.g. `"Users\*\Documents\*.xlsx"`). File modification times are preserved.
- Paths are case-insensitive; a leading `C:\` is accepted and ignored.

## Persistent MCP request queue

Every MCP image operation is persisted in a SQLite FIFO queue before execution.
One worker processes requests in submission order, including reads after writes.
The MCP transport remains available to accept more requests during long work.
Calls wait up to 20 seconds; if still pending/running, the response returns a job
ID. Use `pmf_job(job_id, wait_seconds=0)` to retrieve its status/result/error.

- `pmf_jobs(limit=50, include_acknowledged=False)` lists retained jobs.
- `pmf_acknowledge(job_id)` marks a processed job reviewed; its record remains.
- `pmf_retry(job_id)` creates a new request from a failed/interrupted one.
- Pending jobs resume on MCP server restart (or when the CLI queue worker runs).
- If a worker dies during a request, that job becomes `interrupted`. Its action
  may already have committed; inspect the image revision before retrying.
  Such writes are not automatically repeated. Failed jobs also remain recorded.

Default queue location: `%LOCALAPPDATA%\PMFViewer\queue.sqlite3` on Windows,
`~/.local/state/PMFViewer/queue.sqlite3` elsewhere. Set `PMF_QUEUE_DB` to choose
another local path. Processes using the same database share one worker lock.
The queue retains request arguments (including written text) and read results;
keep it with your private application data. A source-file import reads the file
when its queued job executes. Queue ordering applies to queued requests;
direct CLI/GUI operations use the image's writer lock.

CLI queue access (same database as the MCP server):

```powershell
python pmf_queue.py list
python pmf_queue.py show 12
python pmf_queue.py ack 12
python pmf_queue.py retry 12
python pmf_queue.py submit pmf_read request.json
python pmf_queue.py run
```

`request.json` contains the tool argument object with absolute host paths. The
`run`, `submit` and `retry` commands load the MCP operations and require its SDK.
`run` processes requests until Ctrl+C; pending records survive shutdown.

## Full CLI file access and automatic index checks

```powershell
python pmfview.py backup.pmf write "Documents\notes.txt" "Updated notes" --expected-revision 0
python pmfview.py backup.pmf import E:\input.bin "Documents\input.bin"
python pmfview.py backup.pmf read "Documents\input.bin" --offset 0 --length 4096
python pmfview.py backup.pmf cat "Documents\notes.txt"
python pmfview.py backup.pmf stat "Documents\notes.txt"
python pmfview.py backup.pmf hash "Documents\input.bin" --algorithm sha256
python pmfview.py backup.pmf repair-mft
python pmfview.py backup.pmf repair-mft --apply
```

`write` accepts complete text (or `--encoding base64`); `import` streams large
files. The existing `create`, `ls`, `tree`, `find`, `extract`, `history` and
`update` commands remain available. Use `update --keep-missing` to keep files
missing from the source. Use `--revision N` before a reading command to access
an earlier version. Direct CLI operations complete synchronously; submit through
`pmf_queue.py` when they need to share the persistent MCP request order.

Normal opens validate the MFT-derived file index. Stale, unreadable or structurally
invalid caches rebuild automatically from the image metadata. Ordinary image updates rebuild
and check that metadata before appending. Writable mounts validate it at mount
startup and reopen each committed revision before accepting it. Unreadable records or broken parent
links block writing, with a diagnostic; readable base files remain accessible.

`repair-mft` checks the **PMF viewer index**, not a live NTFS volume's MFT.
`--apply` rebuilds the cache only if metadata validates, without changing image
bytes. `pmf_repair_mft(image_path, repair=False)` exposes the same check through
the queue. Actual damaged/missing metadata is reported as `needs_recovery`;
the command never fabricates file records or claims to repair an NTFS volume.

## Verification

Run integration tests with disposable temporary images only:

```powershell
python -m unittest discover -s tests -v
```

The tests cover unchanged base bytes, small incremental growth, additions,
modifications, deletions, old revisions, interrupted writes, checksum failure,
source changes, MCP cache refresh, read/write/import/extraction and an actual
MCP stdio session. Install the MCP SDK to run those integration cases.
Windows tests additionally mount disposable images on an unused drive, exercise
random writes/truncation, copy/rename/replace/delete, validate earlier revisions,
and test CLI plus queued MCP mount controls. They require WinFsp and its bindings.

Original parser verification (before incremental support):


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

- `pmfview.py` — library + CLI (the parser and PMF creator)
- `pmf_update.py` — append-only revisions, folder updates and file imports
- `pmf_queue.py` — durable request queue and queue CLI
- `pmf_repair.py` — MFT-derived index validation/rebuilding
- `pmfview_gui.py` — tkinter GUI on top of pmfview
- `pmfview_mcp.py` — MCP server on top of pmfview (stdio)
- `pmf_mount.py`, `pmf_mount_store.py`, `pmf_winfsp.py` — optional drive mount and journal adapter
- `pmf_mount_gui.py` — mount controls in the viewer
- `pmf_app.py`, `pmf_windows.spec` — portable Windows application entry point/build

## Limitations

- "Backup All Files" mode images only (file-level backups). Sector-mode
  backups are a different layout and not supported.
- Alternate data streams (ADS) are not extracted — main file content only.
- Created images are written with the PMF metadata subset this tool needs for
  browsing and extraction; compatibility with DiskGenius restore workflows has
  not been validated.
- Hot-image creation uses Windows Volume Shadow Copy, requires administrator
  rights, and supports local Windows volumes rather than network shares.
