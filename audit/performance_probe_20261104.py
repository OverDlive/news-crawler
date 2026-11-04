"""Offline performance investigation; never change production code or live data.

Uses a read-only SQLite backup, temporary settings, transparent Tk windows, and
loopback-only HTTP. Alternative implementations are monkeypatched in memory.
Run: python audit/performance_probe_20261104.py store|gui|http|archive|similarity|old-exe
"""
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / '.build-deps'))


def timings(call, count=3):
    values = []
    for _ in range(count):
        began = time.perf_counter()
        call()
        values.append(round((time.perf_counter() - began) * 1000, 2))
    return values


def config():
    return json.loads((ROOT / 'data/settings.json').read_text(encoding='utf-8-sig'))


def backup(directory):
    import sqlite3
    source = sqlite3.connect((ROOT / 'data/news.sqlite3').as_uri() + '?mode=ro', uri=True)
    target = sqlite3.connect(directory / 'news.sqlite3')
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    (directory / 'settings.json').write_text(json.dumps(config()), encoding='utf-8')


def profiled(call):
    import cProfile
    import io
    import pstats
    profile = cProfile.Profile()
    profile.runcall(call)
    stream = io.StringIO()
    pstats.Stats(profile, stream=stream).strip_dirs().sort_stats('cumulative').print_stats(16)
    return stream.getvalue()


def store_probe(directory):
    from news_core import Store
    from news_collection import NewsCollector
    from search_rules import SearchRule
    from unittest.mock import patch
    store = Store(directory)
    try:
        settings = config()
        rules = settings['keywords']
        active = [r['name'] for r in rules if r['enabled']]
        anchor = store.db.execute('SELECT MAX(succeeded) FROM checks').fetchone()[0]
        def read(grouped=True, now=anchor):
            return store.list_articles(active, settings['hours'], now=now, grouped=grouped, rules=rules)
        result = dict(keywords=len(rules), active=len(active),
                      institutions=len({r['company'] for r in rules if 'company' in r}),
                      actual_query_count=NewsCollector.query_count([r for r in rules if r['enabled']]),
                      snapshot_epoch=anchor, hours=settings['hours'],
                      raw_articles=len(read(False)), grouped_articles=len(read()),
                      list_grouped_ms=timings(read), list_ungrouped_ms=timings(lambda: read(False)),
                      list_empty_today_ms=timings(lambda: read(now=time.time())),
                      summary_candidates_ms=timings(lambda: store.summary_candidates(active, settings['hours'], now=anchor)),
                      list_profile=profiled(read))
        # Exact same rule objects in this immutable probe; production would need
        # invalidation whenever the configuration changes.
        compiled = {json.dumps(r, sort_keys=True): SearchRule(r) for r in rules}
        with patch('news_core.SearchRule', side_effect=lambda r: compiled[json.dumps(r, sort_keys=True)]):
            result['list_with_rule_cache_ms'] = timings(read)
        result['tables'] = {name: store.db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                            for name in ('articles', 'matches', 'summaries', 'article_bodies', 'body_comparisons')}
        return result
    finally:
        store.close()


def gui_probe(directory):
    from unittest.mock import patch
    from app import NewsMonitor
    from tk_runtime import create_root
    from smooth_list import SmoothTermList
    root = create_root()
    root.attributes('-alpha', 0)
    with patch.object(NewsMonitor, 'tick'), patch.object(NewsMonitor, 'seed_demo'):
        app = NewsMonitor(root, directory, demo=True)
    try:
        root.update()
        opened = time.perf_counter()
        app.settings()
        app.settings_window.attributes('-alpha', 0)
        root.update()
        result = {'settings_open_ms': round((time.perf_counter() - opened) * 1000, 2)}
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(app.settings_window))
        entry = next(w for w in widgets if w.winfo_name() == 'company_filter')
        terms = next(w for w in widgets if isinstance(w, SmoothTermList))
        tree = next(w for w in widgets if w.winfo_name() == 'company_list')
        result['company_count'] = len(tree.get_children())
        result['term_count'] = len(terms.get_children())
        result['term_viewport_width'] = terms.viewport.winfo_width()
        assert terms.viewport.winfo_width() > 100, 'Benchmark needs laid-out widgets'
        # Set the actual Tk variable; this invokes precisely the typing trace.
        def search(value):
            root.setvar(entry.cget('textvariable'), value)
            root.update()
        result['search_existing_ms'] = timings(lambda: search('한국'), 5)
        result['search_clear_ms'] = timings(lambda: search(''), 5)
        result['search_profile'] = profiled(lambda: search('한국'))
        original_draw = SmoothTermList._draw
        calls = []
        def counted(widget):
            calls.append(len(widget.rows))
            original_draw(widget)
        with patch.object(SmoothTermList, '_draw', counted):
            search('')
        result['search_clear_draw_calls'] = len(calls)
        result['search_clear_rows_painted'] = sum(calls)
        # Prototype: coalesce all term list mutations into one idle draw.
        scheduled = {}
        def batched(widget):
            if widget in scheduled:
                return
            def flush():
                scheduled.pop(widget, None)
                if widget.winfo_exists():
                    original_draw(widget)
            scheduled[widget] = widget.after_idle(flush)
        with patch.object(SmoothTermList, '_draw', batched):
            result['batched_search_existing_ms'] = timings(lambda: search('한국'), 5)
            result['batched_search_clear_ms'] = timings(lambda: search(''), 5)
        result['after_search_selection_valid'] = bool(tree.selection() and tree.exists(tree.selection()[0]))
        return result
    finally:
        app.close()


def http_probe():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import queue
    import threading
    import psutil
    from bounded_http import HttpPool
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'offline-probe')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    result = {}
    try:
        for mode in ('fifo_current', 'lifo_prototype'):
            pool = HttpPool(workers=8)
            if mode == 'lifo_prototype':
                pool.available = queue.LifoQueue()
                for slot in pool.slots:
                    pool.available.put(slot)
            try:
                counts, durations = [], []
                for _ in range(8):
                    started = time.perf_counter()
                    with pool.request(f'http://127.0.0.1:{server.server_port}/', timeout=15) as response:
                        assert response.read() == b'offline-probe'
                    durations.append(round((time.perf_counter() - started) * 1000, 2))
                    counts.append(sum(s.process is not None and s.process.is_alive() for s in pool.slots))
                children = [psutil.Process(s.process.pid) for s in pool.slots if s.process is not None]
                result[mode] = dict(processes_after_each_sequential_request=counts, request_ms=durations,
                                    child_rss_mb=round(sum(p.memory_info().rss for p in children)/1024**2, 1),
                                    child_private_mb=round(sum(p.memory_info().private for p in children)/1024**2, 1))
            finally:
                pool.close()
            result[mode]['all_probe_workers_closed'] = all(s.process is None for s in pool.slots)
        return result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def archive_probe():
    import dis
    import hashlib
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(ROOT / 'NewsMonitor.exe'))
    pyz = archive.open_embedded_archive('PYZ.pyz')
    instructions = list(dis.get_instructions(pyz.extract('version')))
    version = next(a.argval for a,b in zip(instructions, instructions[1:])
                   if a.opname == 'LOAD_CONST' and b.opname == 'STORE_NAME' and b.argval == 'VERSION')
    with (ROOT / 'NewsMonitor.exe').open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    # Compare executable modules to source without executing archived code.
    def shape(code):
        import types
        return (code.co_code, code.co_names, code.co_varnames,
                tuple(shape(c) if isinstance(c, types.CodeType) else c for c in code.co_consts))
    matching = {}
    for module in ('group_editor', 'smooth_list', 'bounded_http', 'news_core', 'news_similarity'):
        source = compile((ROOT / f'{module}.py').read_bytes(), f'{module}.py', 'exec', dont_inherit=True)
        try:
            matching[module] = shape(source) == shape(pyz.extract(module))
        except KeyError:
            matching[module] = 'module absent in executable'
    return dict(version=version, sha256=digest, source_matches_exe=matching)


def similarity_probe(directory):
    import threading
    from news_core import Store
    from news_similarity import compare_bodies, clean_body, MIN_BODY
    store = Store(directory)
    try:
        settings = config()
        active = [r['name'] for r in settings['keywords'] if r['enabled']]
        anchor = store.db.execute('SELECT MAX(succeeded) FROM checks').fetchone()[0]
        rows = store.list_articles(active, settings['hours'], now=anchor, grouped=False, rules=settings['keywords'])
        rows = [r for r in rows if len(clean_body(r['body'])) >= MIN_BODY][:30]
        began = time.perf_counter()
        results = compare_bodies(rows, set(), threading.Event())
        elapsed = time.perf_counter() - began
        cached = {row[:4] for row in results}
        return dict(sample_bodies=len(rows), pairs=len(results), uncached_seconds=round(elapsed,3),
                    cached_ms=timings(lambda: compare_bodies(rows, cached, threading.Event())),
                    note='Uncached timing includes first sklearn import; not a full production workload.')
    finally:
        store.close()


def old_exe_probe():
    from audit.reproduce_longrun_stalls import archive_info, blocked_request, repeated_timeouts
    from news_collection import NewsCollector
    old, info = archive_info(ROOT / 'NewsMonitor.exe', '_performance_old')
    return dict(executable=info, old_blocked_request=blocked_request(old),
                source_blocked_request=blocked_request(NewsCollector),
                old_repeated_timeouts=repeated_timeouts(old),
                source_repeated_timeouts=repeated_timeouts(NewsCollector))


def main():
    import tempfile
    section = sys.argv[1]
    if section == 'http':
        result = http_probe()
    elif section == 'archive':
        result = archive_probe()
    elif section == 'old-exe':
        result = old_exe_probe()
    else:
        (ROOT / 'tmp').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='perf-probe-', dir=ROOT / 'tmp') as directory:
            directory = Path(directory)
            backup(directory)
            result = {'store': store_probe, 'gui': gui_probe, 'similarity': similarity_probe}[section](directory)
    destination = ROOT / 'audit' / f'performance-{section}-20261104.json'
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
