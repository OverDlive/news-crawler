"""Verify the EXE identity and embedded application code against this checkout."""
import argparse
import dis
import hashlib
import json
import marshal
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def shape(code):
    # PyInstaller rewrites source filenames; executable behavior must still match.
    return (code.co_code, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_argcount, code.co_kwonlyargcount, code.co_flags,
            tuple(shape(value) if isinstance(value, types.CodeType) else value
                  for value in code.co_consts))


def verify(executable):
    from PyInstaller.archive.readers import CArchiveReader
    from version import VERSION
    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive('PYZ.pyz')
    instructions = list(dis.get_instructions(pyz.extract('version')))
    version = next(a.argval for a, b in zip(instructions, instructions[1:])
                   if a.opname == 'LOAD_CONST' and b.opname == 'STORE_NAME' and b.argval == 'VERSION')
    assert version == VERSION, (version, VERSION)
    matched = []
    for name in ('app', 'bounded_http', 'news_core', 'news_views', 'group_editor',
                 'app_storage', 'app_updates', 'smooth_list', 'ui_tasks', 'ui_theme', 'version'):
        embedded = marshal.loads(archive.extract(name)) if name == 'app' else pyz.extract(name)
        source = compile((ROOT / f'{name}.py').read_bytes(), f'{name}.py', 'exec', dont_inherit=True)
        assert shape(source) == shape(embedded), f'EXE has stale code: {name}'
        matched.append(name)
    with executable.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert executable.with_suffix('.exe.sha256').read_text(encoding='utf-8').split()[0] == checksum
    return dict(version=version, sha256=checksum, size=executable.stat().st_size,
                source_matched_modules=matched)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path, default=ROOT / 'dist/NewsMonitor.exe')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = json.dumps(verify(args.exe.resolve()), ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(result + '\n', encoding='utf-8')
    print(result)
