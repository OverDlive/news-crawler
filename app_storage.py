"""User data location and non-destructive, transactional legacy migration."""
from pathlib import Path
import json
import os
import shutil
import sqlite3
from contextlib import closing
import time
import uuid


def acquire_instance():
    """Keep one packaged production instance per Windows session until exit."""
    if os.name != 'nt':
        return
    import atexit
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, 'Local\\NewsMonitor.InstalledApp')
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:
        kernel.CloseHandle(handle)
        raise RuntimeError('News Monitor가 이미 실행 중입니다. 기존 창을 확인해 주세요.')
    atexit.register(kernel.CloseHandle, handle)


def user_directory():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'NewsMonitor'


def snapshot(source, target):
    """Copy all files, using SQLite backup to include committed WAL records."""
    source, target = Path(source), Path(target)
    target.mkdir(parents=True)
    for item in source.iterdir():
        if item.is_symlink():
            raise ValueError('데이터 폴더의 심볼릭 링크는 이전할 수 없습니다.')
        destination = target / item.name
        if item.is_dir():
            snapshot(item, destination)
        elif item.name.endswith(('-wal', '-shm')):
            continue
        elif item.suffix in ('.sqlite3', '.sqlite', '.db'):
            with closing(sqlite3.connect(item.resolve().as_uri() + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(destination)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('기사 데이터베이스 검증에 실패했습니다.')
        else:
            shutil.copy2(item, destination)
            if item.read_bytes() != destination.read_bytes():
                raise ValueError('이전 중 데이터가 변경되었습니다. 기존 프로그램을 종료해 주세요.')


def migrate_data(source, destination):
    """Never overwrite existing user data or remove the original/backup."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        return destination
    if not source.is_dir() or not (source / 'settings.json').is_file():
        raise ValueError(f'기존 settings.json을 찾을 수 없습니다: {source}')
    if source == destination or source in destination.parents:
        raise ValueError('기존 데이터 폴더 안으로 이전할 수 없습니다.')
    # Reject broken settings instead of letting the application replace them with defaults.
    settings = json.loads((source / 'settings.json').read_text(encoding='utf-8-sig'))
    if not isinstance(settings, dict):
        raise ValueError('기존 설정 형식이 올바르지 않습니다.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    backup = destination.parent / 'backups' / stamp
    snapshot(source, backup)
    temporary = destination.parent / ('.migration-' + stamp)
    try:
        shutil.copytree(backup, temporary)
        (temporary / 'migration.json').write_text(json.dumps({
            'source': str(source), 'backup': str(backup), 'completed': stamp,
        }, ensure_ascii=False), encoding='utf-8')
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def resolve_data_directory(app_dir, explicit=None, demo=False, frozen=False):
    if explicit is not None:
        return Path(explicit)
    app_dir = Path(app_dir)
    if not frozen:
        return app_dir / ('demo_data' if demo else 'data')
    destination = user_directory() / ('demo_data' if demo else 'data')
    if demo or destination.exists():
        return destination
    marker = app_dir / 'migration-source.txt'
    if marker.exists():
        source = Path(marker.read_text(encoding='utf-8-sig').strip())
        return migrate_data(source, destination)
    source = app_dir / 'data'
    if source.exists():
        return migrate_data(source, destination)
    return destination
