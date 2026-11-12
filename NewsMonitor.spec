# Build with: python -m PyInstaller --clean --noconfirm NewsMonitor.spec
from pathlib import Path
import sys

# The bundled desktop Python distributes Tcl/Tk scripts as ZIP files.
from tk_runtime import prepare_tk
prepare_tk()

extra_data = [(str(Path(SPECPATH) / 'assets'), 'assets')]
if not (Path(sys.base_prefix) / 'tcl' / 'tcl8.6').is_dir():
    import os
    for variable, destination in [('TCL_LIBRARY', '_tcl_data'), ('TK_LIBRARY', '_tk_data')]:
        if os.environ.get(variable):
            extra_data.append((os.environ[variable], destination))

a = Analysis(['app.py'], pathex=[SPECPATH], binaries=[], datas=extra_data,
             hiddenimports=['kakao_editor', 'telegram_editor'], hookspath=[],
             hooksconfig={}, runtime_hooks=[],
             excludes=['numpy', 'scipy', 'sklearn', 'pandas', 'matplotlib', 'torch', 'tensorflow', 'cupy',
                       'dask', 'IPython', 'pytest'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='NewsMonitor',
          # HTTP workers spawn again after idle expiry/timeouts. Keep their
          # shared Python runtime out of TEMP, where periodic cleanup can
          # remove base_library.zip while the GUI is still running. Expansion
          # happens in the Windows bootloader, before Python can execute.
          runtime_tmpdir=r'%LOCALAPPDATA%\NewsMonitor\runtime',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          icon=str(Path(SPECPATH) / 'assets' / 'news-monitor.ico'))
