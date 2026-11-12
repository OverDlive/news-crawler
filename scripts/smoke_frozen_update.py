"""Update a real one-file process, including its parent bootloader.

All binaries, profiles and data are isolated. No bot credentials or keywords
are configured; close the restarted window before its update-check timer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_updates import ps_literal
from security_profiles import CATALOG_VERSION, FINANCIAL_CATALOG_VERSION
from version import VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-exe', type=Path, default=ROOT / 'dist/NewsMonitor.exe')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='news-frozen-update-') as directory:
        folder = Path(directory)
        target = folder / "한글 App's" / 'NewsMonitor.exe'
        target.parent.mkdir()
        shutil.copy2(args.source_exe, target)
        old_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        data = folder / 'data'
        data.mkdir()
        (data / 'settings.json').write_text(json.dumps({
            'keywords': [], 'institution_catalog_version': CATALOG_VERSION,
            'financial_catalog_version': FINANCIAL_CATALOG_VERSION}), encoding='utf-8')
        staged = target.parent / '.news-update-fixture.exe'
        shutil.copy2(ROOT / 'tmp/installer-smoke/NewsMonitor-Setup-Smoke.exe', staged)
        major, minor, patch = map(int, VERSION.split('.'))
        pending = target.with_name(target.name + '.pending.json')
        pending.write_text(json.dumps({
            'version': f'v{major}.{minor}.{patch + 1}', 'kind': 'installer',
            'file': staged.name, 'sha256': hashlib.sha256(staged.read_bytes()).hexdigest()}),
            encoding='utf-8')
        environment = dict(os.environ, LOCALAPPDATA=str(folder / 'profile'))
        log = data / 'monitor.log'
        process = subprocess.Popen([str(target), '--data-dir', str(data)], env=environment)
        close = ('Get-Process -Name NewsMonitor -ErrorAction SilentlyContinue | '
                 f'Where-Object {{ $_.Path -ieq {ps_literal(target)} }} | '
                 'ForEach-Object { [void]$_.CloseMainWindow() }')
        stop = ('Get-Process -Name NewsMonitor -ErrorAction SilentlyContinue | '
                f'Where-Object {{ $_.Path -ieq {ps_literal(target)} }} | Stop-Process -Force')
        def powershell(command):
            return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                   '-Command', command], check=True, timeout=10)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                text = log.read_text(encoding='utf-8') if log.exists() else ''
                if 'News monitor started' in text:
                    powershell(close)
                    break
                time.sleep(.05)
            else:
                error = target.parent / 'update.log'
                raise AssertionError(error.read_text(encoding='utf-8-sig') if error.exists()
                                     else 'Frozen update did not restart')
            process.wait(timeout=10)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (not pending.exists() and not Path(str(pending) + '.applying').exists()
                        and 'News monitor closing' in log.read_text(encoding='utf-8')):
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Update metadata was not finalized or restarted window did not close')
            assert target.read_bytes() == (ROOT / 'dist/NewsMonitor.exe').read_bytes()
            assert hashlib.sha256(target.with_name(target.name + '.previous').read_bytes()).hexdigest() == old_digest
            assert 'ERROR' not in log.read_text(encoding='utf-8')
            assert not (target.parent / 'update.log').exists()
        finally:
            powershell(stop)
            if process.poll() is None:
                process.kill()
                process.wait()
    print('Real frozen parent exit, installer readiness and GUI restart passed.')


if __name__ == '__main__':
    main()
