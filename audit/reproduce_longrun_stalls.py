"""Offline diagnostic probes; never open live databases or contact a server.

Run from the repository with Python 3.12 and bundled .build-deps available.
The EXE is inspected as a PyInstaller archive, never launched.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import dis
import hashlib
import json
import marshal
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / '.build-deps'))

from PyInstaller.archive.readers import CArchiveReader
from news_collection import NewsCollector
from search_rules import SearchRule
from telegram_notifications import TelegramError, TelegramNotifications


def code_objects(code):
    yield code
    for child in code.co_consts:
        if isinstance(child, types.CodeType):
            yield from code_objects(child)


def archive_info(path, module_name):
    archive = CArchiveReader(str(path))
    pyz = archive.open_embedded_archive('PYZ.pyz')
    version_code = list(dis.get_instructions(pyz.extract('version')))
    version = next(left.argval for left, right in zip(version_code, version_code[1:])
                   if left.opname == 'LOAD_CONST' and right.opname == 'STORE_NAME'
                   and right.argval == 'VERSION')
    # These are locally built, trusted application modules. Only definitions run.
    module = types.ModuleType(module_name)
    sys.modules[module_name] = module
    exec(pyz.extract('news_collection'), module.__dict__)
    app = {code.co_name: code for code in code_objects(marshal.loads(archive.extract('app')))}
    similarity_code = pyz.extract('news_similarity')
    similarity_instructions = list(dis.get_instructions(similarity_code))
    similarity = [left.argval for left, right in zip(similarity_instructions, similarity_instructions[1:])
                  if left.opname == 'LOAD_CONST' and right.opname == 'KW_NAMES'
                  and similarity_code.co_consts[right.arg] == ('maxsize',)]
    with path.open('rb') as source:
        sha256 = hashlib.file_digest(source, 'sha256').hexdigest()
    return module.NewsCollector, dict(
        path=str(path.relative_to(ROOT)), sha256=sha256, version=version,
        per_request_deadline=hasattr(module.NewsCollector(), 'request_timeout'),
        inflight_limit=hasattr(module.NewsCollector(), 'inflight'),
        timer_exception_guard=bool(app['tick'].co_exceptiontable),
        background_enqueue='start_notification_enqueue' in app,
        similarity_cache_capacities=similarity,
    )


def blocked_request(collector_class):
    release, entered = threading.Event(), threading.Event()
    collector = collector_class(workers=1, cycle_timeout=3600, google_request_interval=0)
    if hasattr(collector, 'request_timeout'):
        collector.request_timeout = .1

    def blocked(*args):
        entered.set()
        release.wait(3)
        return []

    with patch.object(collector, 'fetch', side_effect=blocked), ThreadPoolExecutor(1) as controller:
        started = time.monotonic()
        task = controller.submit(collector.collect, [{'name': 'probe', 'enabled': True}])
        try:
            assert entered.wait(1), 'Worker did not start'
            try:
                task.result(timeout=.35)
                returned = True
            except FutureTimeout:
                returned = False
            elapsed = time.monotonic() - started
        finally:
            release.set()
        report = task.result(timeout=2)
    return dict(returned_within_350ms=returned, observed_seconds=round(elapsed, 3),
                errors=len(report.errors))


def repeated_timeouts(collector_class):
    release = threading.Event()
    counter_lock = threading.Lock()
    counts = [0, 0]
    collector = collector_class(workers=2, cycle_timeout=.15, google_request_interval=0)
    if hasattr(collector, 'request_timeout'):
        collector.request_timeout = .1

    def blocked(*args):
        with counter_lock:
            counts[0] += 1
        try:
            release.wait(3)
            return []
        finally:
            with counter_lock:
                counts[1] += 1

    observed = []
    try:
        with patch.object(collector, 'fetch', side_effect=blocked):
            for _ in range(3):
                # Compress the normal five-minute cooldown to reproduce retries.
                with collector.lock:
                    collector.cooldown_until = 0
                collector.collect([{'name': 'probe-a'}, {'name': 'probe-b'}])
                with counter_lock:
                    observed.append(counts[0] - counts[1])
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with counter_lock:
                if counts[0] == counts[1]:
                    break
            time.sleep(.01)
    assert counts[0] == counts[1], 'Probe workers failed to exit'
    return dict(still_running_requests_after_each_cycle=observed)


def sharing_probes():
    (ROOT / 'tmp').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
        queue = TelegramNotifications(Path(directory))
        with queue.connect() as db:
            db.execute("UPDATE settings SET token='offline-placeholder', chat_id=-100, enabled=1, registered=0")
        article = dict(title='한국전력 개인정보 유출 사고 확인', source='진단용', description='',
                       url='https://example.com/offline-probe', discovered=time.time())
        queue.enqueue([article])

        def disconnected(*args):
            raise TelegramError()

        with patch('telegram_notifications.protect', return_value='offline-placeholder'), \
                patch('telegram_notifications.telegram_preview_url', return_value=article['url']):
            state, _ = queue.deliver(request=disconnected)
        restarted = TelegramNotifications(Path(directory))
        calls = []
        restarted.deliver(request=lambda *args: calls.append(True))
        timeout_result = dict(state=state, enabled_after_timeout=bool(queue.settings()['enabled']),
                              enabled_after_restart=bool(restarted.settings()['enabled']),
                              subsequent_api_calls=len(calls))
        assert state == 'unknown' and not restarted.settings()['enabled'] and not calls

        with queue.connect() as db:
            db.execute('UPDATE settings SET enabled=1, next_send=0')
        queue.enqueue([{**article, 'url': article['url'] + '-second', 'title': '카카오 서비스 장애 중단 발생'}])
        entered, release = threading.Event(), threading.Event()
        api_calls = []

        def stalled_preview(article):
            entered.set()
            release.wait(3)
            return article['url']

        def success(*args):
            api_calls.append(True)
            return {'message_id': 1}

        with patch('telegram_notifications.protect', return_value='offline-placeholder'), \
                patch('telegram_notifications.telegram_preview_url', side_effect=stalled_preview), \
                ThreadPoolExecutor(1) as controller:
            task = controller.submit(queue.deliver, request=success)
            try:
                assert entered.wait(1), 'Preview worker did not start'
                acquired = queue.lock.acquire(blocking=False)
                if acquired:
                    queue.lock.release()
                preview_result = dict(worker_blocked_before_send=not task.done(),
                                      delivery_lock_held=not acquired, api_calls=len(api_calls),
                                      stored_states=[row['state'] for row in queue.status()])
                assert not task.done() and not acquired and not api_calls
            finally:
                release.set()
            task.result(timeout=2)
    return dict(transient_send_error=timeout_result, stalled_preview=preview_result)


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('Use Python 3.12 to inspect these Python 3.12 EXE archives.')
    old_class, old_info = archive_info(ROOT / 'NewsMonitor.exe', '_audit_old_collection')
    _, fixed_info = archive_info(ROOT / 'NewsMonitor-장시간실행수정.exe', '_audit_fixed_collection')
    config = json.loads((ROOT / 'data/settings.json').read_text(encoding='utf-8-sig'))
    queries = {query for entry in config['keywords'] if entry['enabled']
               for query in SearchRule(entry).queries}
    actual_queries = set()
    for entry in config['keywords']:
        if entry['enabled']:
            rule = SearchRule(entry)
            actual_queries.update(rule.bing_queries if hasattr(rule, 'company') else rule.queries)
    results = dict(
        incident='2026-10-22 after 10:00 KST on a different PC; incident logs unavailable locally',
        offline=True,
        executable_inspection=[old_info, fixed_info],
        local_config_unique_queries=len(queries),
        local_config_actual_google_queries=len(actual_queries),
        local_config_provider_deadline_seconds=max(600, len(queries) * (5 + 12 / 4) + 120),
        old_exe=dict(blocked_request=blocked_request(old_class), retries=repeated_timeouts(old_class)),
        current_source=dict(blocked_request=blocked_request(NewsCollector), retries=repeated_timeouts(NewsCollector)),
        sharing=sharing_probes(),
    )
    assert not results['old_exe']['blocked_request']['returned_within_350ms']
    assert results['current_source']['blocked_request']['returned_within_350ms']
    assert results['old_exe']['retries']['still_running_requests_after_each_cycle'] == [2, 4, 6]
    assert results['current_source']['retries']['still_running_requests_after_each_cycle'] == [2, 2, 2]
    destination = ROOT / 'audit/longrun-stall-probes-20261023.json'
    destination.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
