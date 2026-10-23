# Build on Windows using Python 3.10. Both executables share one runtime folder.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH)
datas = collect_data_files('mcp') + copy_metadata('mcp', recursive=True)
for name in ('pmfview.png',):
    if (root / name).exists():
        datas.append((str(root / name), '.'))
a = Analysis([str(root / 'pmf_app.py')], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=['pmf_winfsp', 'winfspy.plumbing._bindings', '_cffi_backend'],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['pytest'], noarchive=False)
# WinFsp itself is installed separately through its vendor installer.
a.binaries = [item for item in a.binaries if not Path(item[0]).name.lower().startswith('winfsp-')]
pyz = PYZ(a.pure)
icon = str(root / 'pmfview.ico') if (root / 'pmfview.ico').exists() else None
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name='pmf', console=True,
          debug=False, strip=False, upx=False, icon=icon)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='PMFViewer', console=False,
          debug=False, strip=False, upx=False, icon=icon)
collection = COLLECT(cli, gui, a.binaries, a.datas, strip=False, upx=False, name='PMFViewer')
