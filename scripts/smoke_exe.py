"""Exercise the packaged GUI and persistent storage without network access."""
from pathlib import Path
import subprocess
import tempfile

executable = Path('dist/NewsMonitor.exe').resolve()
with tempfile.TemporaryDirectory(prefix='news-exe-smoke-') as directory:
    subprocess.run([str(executable), '--demo', '--smoke-test', '--data-dir', directory],
                   check=True, timeout=90)
    log = (Path(directory) / 'monitor.log').read_text(encoding='utf-8')
    assert 'News monitor started' in log and 'News monitor closing' in log, log
    assert 'Body similarity smoke test passed' in log, log
    assert 'ERROR' not in log and 'Traceback' not in log, log
    assert (Path(directory) / 'news.sqlite3').is_file()
print('Packaged GUI smoke test passed.')
