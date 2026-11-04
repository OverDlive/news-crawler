"""Post-change measurements with copied production data and no external I/O."""
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / '.build-deps'))


def gui(directory):
    import tkinter as tk
    from unittest.mock import patch
    from app import NewsMonitor
    from tk_runtime import create_root
    from ui_tasks import DebouncedCall
    from smooth_list import SmoothTermList
    root = create_root()
    root.attributes('-alpha', 0)
    with patch.object(NewsMonitor, 'tick'), patch.object(NewsMonitor, 'seed_demo'):
        app = NewsMonitor(root, directory, demo=True)
    try:
        root.update()
        app.settings()
        app.settings_window.attributes('-alpha', 0)
        root.update()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(app.settings_window))
        entry = next(w for w in widgets if w.winfo_name() == 'company_filter')
        tree = next(w for w in widgets if w.winfo_name() == 'company_list')
        terms = next(w for w in widgets if isinstance(w, SmoothTermList))
        blocking, callbacks, paints = [], [], []
        original_flush, original_draw = DebouncedCall.flush, SmoothTermList._draw
        def flush(task):
            began = time.perf_counter()
            original_flush(task)
            if task.callback.__name__ == 'draw':
                callbacks.append(round((time.perf_counter() - began) * 1000, 3))
        def draw(widget):
            began = time.perf_counter()
            original_draw(widget)
            paints.append(round((time.perf_counter() - began) * 1000, 3))
        with patch.object(DebouncedCall, 'flush', flush), patch.object(SmoothTermList, '_draw', draw):
            for text in ('한국', '', '한국전력', '', '한국', '', '한국전력', ''):
                began = time.perf_counter()
                root.setvar(entry.cget('textvariable'), text)
                root.update_idletasks()
                blocking.append(round((time.perf_counter() - began) * 1000, 3))
                done = tk.BooleanVar(root, False)
                root.after(200, lambda: done.set(True))
                root.wait_variable(done)
                root.update()
                if text == '한국전력':
                    assert any('한국전력' in name for name in tree.get_children())
                assert tree.selection() and tree.exists(tree.selection()[0])
        assert len(callbacks) == 8
        return dict(term_viewport_width=terms.viewport.winfo_width(),
                    entry_change_blocking_ms=blocking, deferred_filter_ms=callbacks,
                    term_paint_ms=paints, intentional_debounce_ms=150,
                    note='Filter callback and deferred paint are separate; waiting 150ms does not block typing.')
    finally:
        app.close()


def http():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import psutil
    from bounded_http import HttpPool
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pool = HttpPool(idle_timeout=.3)
    try:
        counts = []
        for _ in range(8):
            with pool.request(f'http://127.0.0.1:{server.server_port}', timeout=10) as response:
                assert response.read() == b'ok'
            counts.append(sum(s.process is not None for s in pool.slots))
        children = [psutil.Process(s.process.pid) for s in pool.slots if s.process is not None]
        result = dict(process_counts=counts,
                      private_mb=round(sum(p.memory_info().private for p in children) / 1024**2, 1),
                      rss_mb=round(sum(p.memory_info().rss for p in children) / 1024**2, 1))
        deadline = time.monotonic() + 3
        while any(s.process is not None for s in pool.slots) and time.monotonic() < deadline:
            time.sleep(.02)
        result['idle_workers_released'] = all(s.process is None for s in pool.slots)
        assert result['idle_workers_released'] and counts == [1] * 8
        return result
    finally:
        pool.close()
        server.shutdown()
        server.server_close()


def main():
    import tempfile
    from audit.performance_probe_20261104 import backup, store_probe
    with tempfile.TemporaryDirectory(dir=ROOT / 'tmp', prefix='perf-v1110-') as directory:
        directory = Path(directory)
        backup(directory)
        result = dict(version='1.1.10', gui=gui(directory), store=store_probe(directory), http=http())
    destination = ROOT / '.qa/performance-v1110.json'
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({**result, 'store': {k: v for k, v in result['store'].items() if k != 'list_profile'}},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
