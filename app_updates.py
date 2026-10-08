"""GitHub release updates for the portable Windows executable.

Downloads never touch the running EXE. A separate helper applies a verified
pending update on the next launch, after the old process releases its file.
"""
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
import urllib.error
import urllib.parse
import urllib.request

from version import ASSET_NAME, REPOSITORY, VERSION

MAX_EXE_BYTES = 150 * 1024 * 1024


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError("정식 버전은 v1.2.3 형식이어야 합니다.")
    return tuple(map(int, match.groups()))


def app_directory():
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


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


def stage_latest(executable, current=VERSION):
    executable = Path(executable).resolve()
    pending = executable.with_name(executable.name + '.pending.json')
    if pending.exists():
        return None
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
    tag = release['tag_name']
    if release.get('draft') or release.get('prerelease') or version_tuple(tag) <= version_tuple(current):
        return None
    assets = {asset['name']: asset for asset in release.get('assets', [])}
    if ASSET_NAME not in assets or ASSET_NAME + '.sha256' not in assets:
        raise ValueError('릴리스에 실행 파일 또는 검증 파일이 없습니다.')
    # Construct known repository URLs instead of trusting arbitrary API asset URLs.
    descriptor, temporary = tempfile.mkstemp(prefix='.news-update-', suffix='.exe', dir=executable.parent)
    os.close(descriptor)
    staged = Path(temporary)
    checksum = staged.with_suffix('.sha256')
    metadata = pending.with_suffix('.tmp')
    try:
        download(release_url(tag, ASSET_NAME + '.sha256'), checksum, 1024)
        expected = checksum.read_text(encoding='utf-8').strip().split()[0].lower()
        if not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('잘못된 SHA-256 검증 파일입니다.')
        download(release_url(tag, ASSET_NAME), staged, MAX_EXE_BYTES)
        if digest(staged) != expected:
            raise ValueError('업데이트 파일 SHA-256이 일치하지 않습니다.')
        with staged.open('rb') as source:
            if source.read(2) != b'MZ':
                raise ValueError('Windows 실행 파일이 아닙니다.')
        metadata.write_text(json.dumps({'version': tag, 'file': staged.name, 'sha256': expected}), encoding='utf-8')
        metadata.replace(pending)
        return tag
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    finally:
        checksum.unlink(missing_ok=True)
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


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def apply_pending(executable=None, arguments=None):
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return False
    executable = Path(executable or sys.executable).resolve()
    pending = executable.with_name(executable.name + '.pending.json')
    if not pending.exists():
        return False
    metadata = json.loads(pending.read_text(encoding='utf-8'))
    name = metadata['file']
    if not re.fullmatch(r'\.news-update-[a-z0-9_\-]+\.exe', name):
        raise ValueError('잘못된 업데이트 파일 경로입니다.')
    staged = executable.parent / name
    if version_tuple(metadata['version']) <= version_tuple(VERSION):
        staged.unlink(missing_ok=True)
        pending.unlink()
        return False
    if digest(staged) != metadata['sha256']:
        staged.unlink(missing_ok=True)
        pending.unlink()
        raise ValueError('대기 중인 업데이트 파일 검증에 실패했습니다.')
    backup = executable.with_name(executable.name + '.previous')
    log = executable.with_name('update.log')
    # Windows CRT quoting is also the quoting accepted by Start-Process.
    args = subprocess.list2cmdline(list(sys.argv[1:] if arguments is None else arguments))
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
    Move-Item -LiteralPath $staged -Destination $target
    $installed = $true
    Remove-Item -LiteralPath $pending
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    {"Start-Process -FilePath $target -ArgumentList " + ps_literal(args) + " -WindowStyle Hidden" if args else "Start-Process -FilePath $target -WindowStyle Hidden"}
}} catch {{
    $_ | Out-File -LiteralPath $log -Append -Encoding utf8
    if ($installed -and (Test-Path -LiteralPath $target)) {{ Remove-Item -LiteralPath $target }}
    if ($backedUp -and (Test-Path -LiteralPath $backup)) {{ Move-Item -LiteralPath $backup -Destination $target }}
    Remove-Item -LiteralPath $pending -ErrorAction SilentlyContinue
    {"Start-Process -FilePath $target -ArgumentList " + ps_literal(args) + " -WindowStyle Hidden" if args else "Start-Process -FilePath $target -WindowStyle Hidden"}
}} finally {{
    Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue
}}
"""
    descriptor, helper = tempfile.mkstemp(prefix='.news-apply-', suffix='.ps1', dir=executable.parent)
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
