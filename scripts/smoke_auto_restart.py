"""Exercise main's timer: download -> graceful GUI close -> real EXE restart.

The initial GUI uses demo data and a local release fixture; no bot/network calls.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory(prefix='news-auto-restart-') as directory:
    folder = Path(directory)
    target = folder / 'NewsMonitor.exe'
    target.write_bytes(b'old version fixture')
    data = folder / 'data'
    environment = dict(os.environ, LOCALAPPDATA=str(folder / 'profile'))
    child = r'''
import hashlib, sys
from pathlib import Path
import app, app_updates
from version import VERSION
target, data, binary = map(Path, sys.argv[1:])
original_app = app.NewsMonitor
original_checker = app.UpdateChecker
original_apply = app.apply_pending
app.NewsMonitor = lambda root, directory, demo: original_app(root, directory, demo=True)
app.UpdateChecker = lambda: original_checker(target, downloadable=True)
app.apply_pending = lambda: original_apply(target, ['--demo', '--smoke-test', '--data-dir', str(data)])
major, minor, patch = map(int, VERSION.split('.'))
app_updates.latest_release = lambda current: {
    'tag_name': f'v{major}.{minor}.{patch + 1}',
    'assets': [{'name': app_updates.setup_asset_name(f'v{major}.{minor}.{patch + 1}'),
                'digest': 'sha256:' + hashlib.sha256(binary.read_bytes()).hexdigest()}]}
def download(url, destination, limit):
    if url.endswith('.sha256'):
        destination.write_text(hashlib.sha256(binary.read_bytes()).hexdigest())
    else:
        destination.write_bytes(binary.read_bytes())
app_updates.download = download
sys.frozen = True
sys.argv = ['app', '--data-dir', str(data)]
raise SystemExit(app.main())
'''
    subprocess.run([sys.executable, '-c', child, str(target), str(data),
                    str(ROOT / 'tmp/installer-smoke/NewsMonitor-Setup-Smoke.exe')], env=environment,
                   cwd=ROOT, check=True, timeout=45)
    log = data / 'monitor.log'
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if log.exists() and log.read_text(encoding='utf-8').count('News monitor closing') >= 2:
            break
        time.sleep(.2)
    else:
        raise AssertionError('Automatic timer did not close and restart the GUI')
    assert target.read_bytes() == (ROOT / 'dist/NewsMonitor.exe').read_bytes()
    for attempt in range(50):
        try:
            target.unlink()
            break
        except PermissionError:
            time.sleep(.2)
    else:
        raise AssertionError('Restarted GUI did not exit')
print('Automatic download, GUI shutdown, replacement and EXE restart passed.')
