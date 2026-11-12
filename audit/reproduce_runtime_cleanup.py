"""Reproduce lost stdlib on worker respawn, then verify the EXE's fix.

Builds small console probes so the legacy failure cannot open a modal dialog.
Only a generated base_library.zip under this test's fake TEMP is deleted.
Requires PyInstaller and a freshly built dist/NewsMonitor.exe.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
PROBE = '''import multiprocessing as mp
if __name__ == '__main__':
    mp.freeze_support()
import json, sys, time
from pathlib import Path

if __name__ == '__main__':
    folder = Path(sys.argv[1])
    context = mp.get_context('spawn')
    first = context.Process(target=time.sleep, args=(.01,))
    first.start()
    first.join(10)
    assert first.exitcode == 0, first.exitcode
    first.close()
    (folder / 'ready.json').write_text(json.dumps({'runtime': sys._MEIPASS}))
    deadline = time.monotonic() + 20
    while not (folder / 'resume').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('Probe was not resumed')
        time.sleep(.02)
    second = context.Process(target=time.sleep, args=(.01,))
    second.start()
    second.join(10)
    (folder / 'result.json').write_text(json.dumps({'exitcode': second.exitcode}))
    if second.is_alive():
        second.kill()
        second.join()
    second.close()
'''


def main():
    options = CArchiveReader(str(ROOT / 'dist/NewsMonitor.exe')).options
    runtime_option = next(option.removeprefix('pyi-runtime-tmpdir ')
                          for option in options if option.startswith('pyi-runtime-tmpdir '))
    with tempfile.TemporaryDirectory(prefix='news-runtime-regression-') as directory:
        folder = Path(directory)
        source = folder / 'probe.py'
        source.write_text(PROBE, encoding='utf-8')
        for name, runtime in [('legacy', None), ('protected', runtime_option)]:
            command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile',
                       '--console', '--name', name, '--distpath', str(folder / 'dist'),
                       '--workpath', str(folder / 'build'), '--specpath', str(folder)]
            if runtime:
                command.extend(['--runtime-tmpdir', runtime])
            subprocess.run(command + [str(source)], check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=120)
            run = folder / name
            temporary = run / 'system temp'
            temporary.mkdir(parents=True)
            profile = run / '사용자 profile'
            environment = dict(os.environ, LOCALAPPDATA=str(profile),
                               TEMP=str(temporary), TMP=str(temporary))
            with subprocess.Popen([str(folder / 'dist' / f'{name}.exe'), str(run)],
                                  env=environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) as process:
                try:
                    deadline = time.monotonic() + 20
                    while not (run / 'ready.json').exists():
                        if process.poll() is not None or time.monotonic() >= deadline:
                            process.kill()
                            stdout, stderr = process.communicate()
                            raise AssertionError(f'{name}: initial worker failed: {stdout!r} {stderr!r}')
                        time.sleep(.02)
                    unpacked = Path(json.loads((run / 'ready.json').read_text())['runtime'])
                    expected = profile / 'NewsMonitor' / 'runtime' if runtime else temporary
                    assert unpacked.parent == expected, unpacked
                    # Simulate cleanup of stdlib ZIPs inside this test's TEMP only.
                    removed = 0
                    for archive in temporary.rglob('base_library.zip'):
                        assert archive.resolve().is_relative_to(temporary.resolve())
                        archive.unlink()
                        removed += 1
                    (run / 'resume').touch()
                    stdout, stderr = process.communicate(timeout=25)
                    assert process.returncode == 0, (stdout, stderr)
                    result = json.loads((run / 'result.json').read_text())
                    if runtime:
                        assert removed == 0
                        assert result['exitcode'] == 0, (result, stderr)
                    else:
                        assert removed == 1
                        assert result['exitcode'] != 0, result
                        assert b'Failed to start embedded python interpreter' in stderr, stderr
                    assert not unpacked.exists(), 'Probe runtime was not cleaned up'
                    print(f'{name}: cleanup removed {removed} stdlib ZIP; '
                          f'respawn exit code={result["exitcode"]}', flush=True)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
    print('Legacy interpreter failure reproduced; protected worker respawn passed.')


if __name__ == '__main__':
    main()
