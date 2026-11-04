"""Build the portable executable and its checksum, locally or in CI."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installer', action='store_true', help='Also compile the Inno Setup installer')
    parser.add_argument('--iscc', help='Path to ISCC.exe')
    parser.add_argument('--version', help='Release tag, e.g. v1.2.3')
    parser.add_argument('--repository', default='OverDlive/news-crawler')
    args = parser.parse_args()
    if args.version:
        if not re.fullmatch(r'v?\d+\.\d+\.\d+', args.version):
            parser.error('Version must be vMAJOR.MINOR.PATCH (stable release).')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository):
            parser.error('Repository must be owner/name.')
        (ROOT / 'version.py').write_text(
            f'VERSION = {args.version.removeprefix("v")!r}\n'
            f'REPOSITORY = {args.repository!r}\nASSET_NAME = "NewsMonitor.exe"\n', encoding='utf-8')
    from version import VERSION
    notes = ROOT / 'releases' / f'v{VERSION}.md'
    if not notes.is_file():
        parser.error(f'Create numbered release notes first: {notes}')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                    str(ROOT / 'NewsMonitor.spec')], cwd=ROOT, check=True)
    executable = ROOT / 'dist' / 'NewsMonitor.exe'
    with executable.open('rb') as source:
        checksum = hashlib.file_digest(source, 'sha256').hexdigest()
    executable.with_suffix('.exe.sha256').write_text(
        f'{checksum}  NewsMonitor.exe\n', encoding='utf-8')
    shutil.copyfile(notes, ROOT / 'dist' / 'RELEASE_NOTES.md')
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'verify_build.py'),
                    '--exe', str(executable)], cwd=ROOT, check=True)
    if args.installer:
        compiler = args.iscc or shutil.which('ISCC')
        if not compiler:
            candidates = [Path(base) / f'Inno Setup {version}/ISCC.exe'
                          for base in (os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'),
                                       os.environ.get('ProgramFiles', 'C:/Program Files'))
                          for version in (6, 7)]
            candidates.append(ROOT / 'tmp/inno/compiler/ISCC.exe')
            compiler = next((str(path) for path in candidates if path.is_file()), None)
        if not compiler:
            parser.error('Inno Setup compiler required: use --iscc PATH')
        subprocess.run([compiler, f'/DAppVersion={VERSION}', str(ROOT / 'installer/NewsMonitor.iss')], check=True)
        # Same installer payload/code, isolated from real uninstall registration and shortcuts.
        subprocess.run([compiler, f'/DAppVersion={VERSION}', '/DSmokeTest',
                        str(ROOT / 'installer/NewsMonitor.iss')], check=True)
        setup = ROOT / 'dist' / f'NewsMonitor-Setup-{VERSION}.exe'
        checksum = hashlib.sha256(setup.read_bytes()).hexdigest()
        setup.with_suffix('.exe.sha256').write_text(f'{checksum}  {setup.name}\n', encoding='utf-8')
        print(setup)
    print(executable)


if __name__ == '__main__':
    main()
