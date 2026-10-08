"""Run the real Windows updater helper, replacement, backup and GUI restart."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from version import VERSION

with tempfile.TemporaryDirectory(prefix='news-update-smoke-') as directory:
    folder = Path(directory)
    target = folder / "News Monitor's.exe"
    target.write_bytes(b'old version placeholder')
    staged = folder / '.news-update-test.exe'
    shutil.copy2(ROOT / 'dist' / 'NewsMonitor.exe', staged)
    checksum = hashlib.sha256(staged.read_bytes()).hexdigest()
    major, minor, patch = map(int, VERSION.split('.'))
    pending = target.with_name(target.name + '.pending.json')
    pending.write_text(json.dumps({'version': f'v{major}.{minor}.{patch + 1}',
                                   'file': staged.name, 'sha256': checksum}), encoding='utf-8')
    child = 'import sys; sys.frozen=True; from app_updates import apply_pending; assert apply_pending(sys.argv[1], sys.argv[2:])'
    subprocess.run([sys.executable, '-c', child, str(target), '--demo', '--smoke-test',
                    '--data-dir', str(folder / 'data with spaces')], cwd=ROOT, check=True, timeout=30)
    log = folder / 'data with spaces' / 'monitor.log'
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if log.exists() and 'News monitor closing' in log.read_text(encoding='utf-8'):
            break
        time.sleep(.2)
    else:
        failure = folder / 'update.log'
        raise AssertionError(failure.read_text(encoding='utf-8-sig') if failure.exists() else 'Updated GUI did not restart')
    assert hashlib.sha256(target.read_bytes()).hexdigest() == checksum
    assert target.with_name(target.name + '.previous').read_bytes() == b'old version placeholder'
    assert not pending.exists()
    assert not (folder / 'update.log').exists()
    # The one-file bootloader may take a moment to release the EXE after GUI close.
    for attempt in range(50):
        try:
            target.unlink()
            break
        except PermissionError:
            time.sleep(.2)
    else:
        raise AssertionError('Updated process did not exit')
print('Real update replacement, backup and GUI restart passed.')
