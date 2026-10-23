"""Build a clean portable folder and ZIP on Windows/Python 3.10.

Usage: python build_distribution.py E:\\releases\\pmf
The output folder must not already contain PMFViewer or the release ZIP.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

SOURCE_FILES = (
    '.gitignore', 'README.md', 'START-HERE.txt', 'requirements-mount.txt',
    'requirements-build.txt', 'pmf_windows.spec', 'build_distribution.py',
    'pmf_app.py', 'pmfview.py', 'pmfview_gui.py', 'pmfview_mcp.py', 'pmf_update.py',
    'pmf_queue.py', 'pmf_repair.py', 'pmf_mount.py', 'pmf_mount_store.py',
    'pmf_mount_gui.py', 'pmf_winfsp.py', 'pmfview.ico', 'pmfview.png',
)


def populate(folder: Path, source: Path) -> Path:
    """Add only explicit application sources; never copy a working tree wholesale."""
    for name in ('README.md', 'START-HERE.txt'):
        shutil.copy2(source / name, folder / name)
    exported = folder / 'Source'
    exported.mkdir()
    for name in SOURCE_FILES:
        item = source / name
        if item.is_file():
            shutil.copy2(item, exported / name)
    for relative in ('tests', 'third_party'):
        destination = exported / relative
        destination.mkdir()
        for item in (source / relative).glob('*'):
            if item.is_file() and item.suffix in ('.py', '.txt', '.md'):
                shutil.copy2(item, destination / item.name)
    optional = folder / 'Optional-Mounting'
    optional.mkdir()
    (optional / 'Download-WinFsp.url').write_text(
        '[InternetShortcut]\nURL=https://github.com/winfsp/winfsp/releases/tag/v2.1\n', encoding='utf-8')
    (optional / 'README.txt').write_text(
        'Optional Windows drive mounting requires WinFsp 2.1 installed separately.\n'
        'Open Download-WinFsp.url and use the official signed MSI.\n'
        'WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos\n'
        'https://github.com/winfsp/winfsp\n'
        'The WinFsp driver and DLL are not part of this package.\n', encoding='utf-8')
    notices = folder / 'Third-Party'
    notices.mkdir()
    versions = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata['Name']
        versions[name] = dist.version
        for package_file in dist.files or []:
            licence_path = Path(str(dist.locate_file(package_file)))
            if (package_file.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE'))
                    and licence_path.is_file()):
                # Metadata entries can contain ../ paths; restrict output to this package.
                destination = notices / name / str(package_file).replace('../', '').replace('..\\', '')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(licence_path, destination)
    for item in (source / 'third_party').glob('*'):
        if item.is_file():
            shutil.copy2(item, notices / item.name)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.exists():
        shutil.copy2(python_license, notices / 'Python-LICENSE.txt')
    (notices / 'versions.json').write_text(json.dumps({
        'python': sys.version.split()[0], 'packages': dict(sorted(versions.items()))}, indent=2), encoding='utf-8')
    (notices / 'README.txt').write_text(
        'Dependency copyright/licence texts are preserved here and in _internal.\n'
        'Python: https://www.python.org/\n'
        'PyInstaller: https://github.com/pyinstaller/pyinstaller\n'
        'MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk\n'
        'winfspy 0.8.4 (upstream licence included): https://github.com/Scille/winfspy\n'
        'WinFsp (separate installation): https://github.com/winfsp/winfsp\n', encoding='utf-8')
    lines = []
    for item in sorted(folder.rglob('*')):
        if item.is_file():
            with item.open('rb') as stream:
                checksum = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1 << 20), b''):
                    checksum.update(chunk)
            digest = checksum.hexdigest()
            lines.append(f'{digest}  {item.relative_to(folder).as_posix()}')
    (folder / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    archive = folder.parent / 'PMFViewer-Windows-x64.zip'
    if archive.exists():
        raise FileExistsError(archive)
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as z:
        for item in sorted(folder.rglob('*')):
            if item.is_file():
                z.write(item, str(Path(folder.name) / item.relative_to(folder)))
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or sys.version_info[:2] != (3, 10):
        parser.error('Build with Windows x64 and Python 3.10')
    output = args.output.resolve()
    if (output / 'PMFViewer').exists() or (output / 'PMFViewer-Windows-x64.zip').exists():
        parser.error('Choose an unused output directory')
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='pmf-build-') as work:
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                        '--distpath', str(output), '--workpath', work,
                        str(source / 'pmf_windows.spec')], check=True)
    archive = populate(output / 'PMFViewer', source)
    print(f'Distribution: {output / "PMFViewer"}\nZIP: {archive}')


if __name__ == '__main__':
    main()
