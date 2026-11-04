"""Verified updates applied by a hidden helper after graceful shutdown."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from version import REPOSITORY, VERSION
from app_storage import user_directory

MAX_EXE_BYTES = 150 * 1024 * 1024


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError("정식 버전은 v1.2.3 형식이어야 합니다.")
    return tuple(map(int, match.groups()))


def app_directory():
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def update_directory(executable):
    key = hashlib.sha256(str(Path(executable).resolve()).casefold().encode()).hexdigest()[:20]
    directory = user_directory() / 'updates' / key
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def pending_path(executable):
    # Accept downloads staged by versions before the installer migration.
    legacy = Path(executable).with_name(Path(executable).name + '.pending.json')
    return legacy if legacy.exists() else update_directory(executable) / 'pending.json'


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def release_url(tag, filename):
    return f"https://github.com/{REPOSITORY}/releases/download/{urllib.parse.quote(tag, safe='')}/{filename}"


def download(url, path, limit):
    request = urllib.request.Request(url, headers={'User-Agent': f'NewsMonitor/{VERSION}'})
    with urllib.request.urlopen(request, timeout=20) as response, Path(path).open('wb') as output:
        total = 0
        while chunk := response.read(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError('업데이트 파일 크기가 제한을 초과했습니다.')
            output.write(chunk)


def latest_release(current=VERSION):
    request = urllib.request.Request(
        f'https://api.github.com/repos/{REPOSITORY}/releases/latest',
        headers={'User-Agent': f'NewsMonitor/{current}', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            release = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # No public release yet.
        raise
    if release.get('draft') or release.get('prerelease'):
        return None
    version_tuple(release['tag_name'])
    return release


def setup_asset_name(tag):
    version_tuple(tag)
    return f'NewsMonitor-Setup-{tag.removeprefix("v")}.exe'


def stage_latest(executable, current=VERSION, release=None):
    executable = Path(executable).resolve()
    pending = pending_path(executable)
    if pending.exists():
        return None
    release = release if release is not None else latest_release(current)
    if release is None:
        return None
    tag = release['tag_name']
    if release.get('draft') or release.get('prerelease') or version_tuple(tag) <= version_tuple(current):
        return None
    assets = {asset['name']: asset for asset in release.get('assets', [])}
    asset_name = setup_asset_name(tag)
    asset = assets.get(asset_name, {})
    checksum = asset.get('digest') or ''
    if not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', checksum):
        raise ValueError('설치 파일 또는 GitHub SHA-256 검증값이 없습니다.')
    expected = checksum.split(':', 1)[1].lower()
    # Construct known repository URLs instead of trusting arbitrary API asset URLs.
    descriptor, temporary = tempfile.mkstemp(prefix='.news-update-', suffix='.exe', dir=pending.parent)
    os.close(descriptor)
    staged = Path(temporary)
    metadata = pending.with_suffix('.tmp')
    try:
        download(release_url(tag, asset_name), staged, MAX_EXE_BYTES)
        if digest(staged) != expected:
            raise ValueError('업데이트 파일 SHA-256이 일치하지 않습니다.')
        with staged.open('rb') as source:
            if source.read(2) != b'MZ':
                raise ValueError('Windows 실행 파일이 아닙니다.')
        metadata.write_text(json.dumps({'version': tag, 'file': staged.name, 'sha256': expected, 'kind': 'installer'}), encoding='utf-8')
        metadata.replace(pending)
        return tag
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    finally:
        metadata.unlink(missing_ok=True)


def background_check():
    def worker():
        try:
            tag = stage_latest(sys.executable)
            if tag:
                logging.info('Update %s downloaded; will apply on next launch', tag)
        except Exception:
            logging.exception('Automatic update check failed; continuing current version')
    threading.Thread(target=worker, name='release-update', daemon=True).start()


class UpdateChecker:
    """Shared automatic/manual checks; UI reads snapshots on the Tk thread."""
    def __init__(self, executable=None, current=VERSION, downloadable=None):
        self.executable = Path(executable or sys.executable)
        self.current = current
        self.downloadable = getattr(sys, 'frozen', False) if downloadable is None else downloadable
        self.lock = threading.Lock()
        self.state = {'busy': False, 'latest': None, 'checked': 0,
                      'ready': False, 'message': '아직 업데이트를 확인하지 않았습니다.'}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def check(self):
        with self.lock:
            if self.state['busy']:
                return False
            self.state.update(busy=True, ready=False, message='새 버전을 확인하고 있습니다…')
        def worker():
            latest = None
            try:
                pending = pending_path(self.executable) if self.downloadable else None
                if self.downloadable and pending.exists():
                    latest = json.loads(pending.read_text(encoding='utf-8'))['version']
                    version_tuple(latest)
                    message = f'{latest} 다운로드 완료 · 작업이 끝나면 자동으로 종료하고 다시 실행합니다.'
                else:
                    release = latest_release(self.current)
                    latest = release['tag_name'] if release else None
                    if latest is None:
                        message = '배포된 정식 버전이 없습니다.'
                    elif version_tuple(latest) <= version_tuple(self.current):
                        message = '최신 버전을 사용 중입니다.'
                    elif not self.downloadable:
                        message = f'{latest} 업데이트가 있습니다. EXE로 실행하면 자동으로 다운로드됩니다.'
                    else:
                        with self.lock:
                            self.state.update(latest=latest, message=f'{latest} 다운로드 및 검증 중…')
                        stage_latest(self.executable, self.current, release=release)
                        message = f'{latest} 다운로드 완료 · 작업이 끝나면 자동으로 종료하고 다시 실행합니다.'
                with self.lock:
                    self.state.update(latest=latest, message=message, checked=time.time(),
                                      ready=self.downloadable and pending.exists())
            except Exception:
                logging.exception('Update check failed; continuing current version')
                with self.lock:
                    self.state.update(message='업데이트를 확인하지 못했습니다. 인터넷 연결 및 저장 폴더 권한을 확인한 뒤 다시 시도하세요.', checked=time.time())
            finally:
                with self.lock:
                    self.state['busy'] = False
        threading.Thread(target=worker, name='release-update', daemon=True).start()
        return True


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def apply_pending(executable=None, arguments=None):
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return False
    executable = Path(executable or sys.executable).resolve()
    pending = pending_path(executable)
    if not pending.exists():
        return False
    metadata = json.loads(pending.read_text(encoding='utf-8'))
    name = metadata['file']
    if not re.fullmatch(r'\.news-update-[a-z0-9_\-]+\.exe', name):
        raise ValueError('잘못된 업데이트 파일 경로입니다.')
    staged = pending.parent / name
    if version_tuple(metadata['version']) <= version_tuple(VERSION):
        staged.unlink(missing_ok=True)
        pending.unlink()
        return False
    if digest(staged) != metadata['sha256']:
        staged.unlink(missing_ok=True)
        pending.unlink()
        raise ValueError('대기 중인 업데이트 파일 검증에 실패했습니다.')
    backup = executable.with_name(executable.name + '.previous')
    log = pending.parent / 'update.log'
    # Windows CRT quoting is also the quoting accepted by Start-Process.
    args = subprocess.list2cmdline(list(sys.argv[1:] if arguments is None else arguments))
    installer = metadata.get('kind') == 'installer'
    install_args = subprocess.list2cmdline([
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
        '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS',
        f'/DIR={executable.parent}', f'/LOG={pending.parent / "setup.log"}'])
    replacement = (f"$setup = Start-Process -FilePath $staged -ArgumentList {ps_literal(install_args)} -WindowStyle Hidden -Wait -PassThru\n"
                   "    if ($setup.ExitCode -ne 0) { throw ('Setup exit code: ' + $setup.ExitCode) }\n"
                   f"    $launchTarget = {ps_literal(executable.parent / 'NewsMonitor.exe')}\n"
                   "    if (-not (Test-Path -LiteralPath $launchTarget)) { throw 'Installed executable missing' }") if installer else (
                       'Copy-Item -LiteralPath $staged -Destination $target\n    $launchTarget = $target')
    script = f"""$ErrorActionPreference = 'Stop'
# Launching from PowerShell 7 can otherwise inherit incompatible module paths.
$env:PSModulePath = "$env:SystemRoot\\System32\\WindowsPowerShell\\v1.0\\Modules"
$target = {ps_literal(executable)}
$staged = {ps_literal(staged)}
$backup = {ps_literal(backup)}
$pending = {ps_literal(pending)}
$log = {ps_literal(log)}
Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue
$installed = $false
$backedUp = $false
try {{
    if ((Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash.ToLower() -ne {ps_literal(metadata['sha256'])}) {{ throw 'SHA256 mismatch' }}
    for ($attempt = 0; $attempt -lt 60; $attempt++) {{
        try {{
            if (Test-Path -LiteralPath $backup) {{ Remove-Item -LiteralPath $backup }}
            Move-Item -LiteralPath $target -Destination $backup
            $backedUp = $true
            break
        }} catch {{
            if ($attempt -eq 59) {{ throw }}
            Start-Sleep -Seconds 1
        }}
    }}
    {replacement}
    Remove-Item -LiteralPath $staged
    $installed = $true
    Remove-Item -LiteralPath $pending
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    {"Start-Process -FilePath $launchTarget -ArgumentList " + ps_literal(args) + " -WindowStyle Hidden" if args else "Start-Process -FilePath $launchTarget -WindowStyle Hidden"}
}} catch {{
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    $_ | Out-File -LiteralPath $log -Append -Encoding utf8
    if ($backedUp -and (Test-Path -LiteralPath $target)) {{ Remove-Item -LiteralPath $target }}
    if ($backedUp -and (Test-Path -LiteralPath $backup)) {{ Move-Item -LiteralPath $backup -Destination $target }}
    Remove-Item -LiteralPath $pending -ErrorAction SilentlyContinue
    {"Start-Process -FilePath $target -ArgumentList " + ps_literal(args) + " -WindowStyle Hidden" if args else "Start-Process -FilePath $target -WindowStyle Hidden"}
}} finally {{
    Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue
}}
"""
    descriptor, helper = tempfile.mkstemp(prefix='.news-apply-', suffix='.ps1', dir=pending.parent)
    os.close(descriptor)
    Path(helper).write_text(script, encoding='utf-8-sig')
    try:
        powershell = str(Path(os.environ['SystemRoot']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')
        subprocess.Popen([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                          '-File', helper], creationflags=subprocess.CREATE_NO_WINDOW,
                         cwd=executable.parent)
    except BaseException:
        Path(helper).unlink(missing_ok=True)
        raise
    return True
