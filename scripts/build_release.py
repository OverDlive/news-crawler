"""Build the portable executable and its checksum, locally or in CI."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
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
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                    str(ROOT / 'NewsMonitor.spec')], cwd=ROOT, check=True)
    executable = ROOT / 'dist' / 'NewsMonitor.exe'
    with executable.open('rb') as source:
        checksum = hashlib.file_digest(source, 'sha256').hexdigest()
    executable.with_suffix('.exe.sha256').write_text(
        f'{checksum}  NewsMonitor.exe\n', encoding='utf-8')
    print(executable)


if __name__ == '__main__':
    main()
