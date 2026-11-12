"""Exercise the packaged GUI and persistent storage without network access."""
from pathlib import Path
import argparse
import os
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--exe', default='dist/NewsMonitor.exe')
executable = Path(parser.parse_args().exe).resolve()
with tempfile.TemporaryDirectory(prefix='news-exe-smoke-') as directory:
    folder = Path(directory)
    temporary = folder / 'system temp'
    temporary.mkdir()
    profile = folder / '사용자 profile'
    environment = dict(os.environ, LOCALAPPDATA=str(profile),
                       TEMP=str(temporary), TMP=str(temporary))
    subprocess.run([str(executable), '--demo', '--smoke-test', '--data-dir', directory],
                   check=True, timeout=90, env=environment)
    log = (Path(directory) / 'monitor.log').read_text(encoding='utf-8')
    runtime_line = next(line for line in log.splitlines()
                        if 'Embedded Python runtime directory: ' in line)
    runtime = Path(runtime_line.split('Embedded Python runtime directory: ', 1)[1])
    assert runtime.parent == profile / 'NewsMonitor' / 'runtime', runtime
    assert runtime.name.startswith('_MEI'), runtime
    assert not runtime.exists(), 'Bootloader did not clean up the private runtime'
    assert 'News monitor started' in log and 'News monitor closing' in log, log
    assert 'Body similarity smoke test passed' in log, log
    assert 'Institution representative preview smoke test passed' in log, log
    assert 'Bounded HTTP process timeout and recovery smoke test passed' in log, log
    assert 'Background article view smoke test passed' in log, log
    assert 'ERROR' not in log and 'Traceback' not in log, log
    assert (Path(directory) / 'news.sqlite3').is_file()
print('Packaged GUI smoke test passed.')
