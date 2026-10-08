"""Bounded concurrent RSS collection with cancellation and HTTP revalidation."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from news_core import RSS_HEADERS, parse_feed
from search_rules import SearchRule


class ServerCoolingDown(OSError):
    pass


@dataclass
class CollectionReport:
    results: list
    errors: list
    requests: int
    seconds: float
    cooldown: float = 0
    cancelled: bool = False
    provider: str = 'google'
    fallback_reason: str = ''


class NewsCollector:
    def __init__(self, workers=4, timeout=12, cache_size=128, fallback=False, google_request_interval=5,
                 cycle_timeout=600):
        self.workers = max(1, min(workers, 4))
        self.timeout = timeout
        self.cycle_timeout = max(.1, cycle_timeout)
        self.cache_size = cache_size
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.cooldown_until = 0
        self.bing_cooldown_until = 0
        self.fallback = fallback
        self.google_request_interval = max(0, google_request_interval)
        self.google_request_lock = threading.Lock()
        self.google_next_request = 0
        self.context = threading.local()
        self.failures = {'google': 0, 'bing': 0}

    def cooldown_remaining(self, provider='google'):
        with self.lock:
            until = self.cooldown_until if provider == 'google' else self.bing_cooldown_until
            return max(0, until - time.monotonic())

    def _cool_down(self, headers, provider='google'):
        retry = (headers or {}).get('Retry-After', '')
        try:
            seconds = float(retry)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(retry).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        with self.lock:
            self.failures[provider] = min(5, self.failures[provider] + 1)
            backoff = min(3600, 300 * 2 ** (self.failures[provider] - 1))
            attribute = 'cooldown_until' if provider == 'google' else 'bing_cooldown_until'
            setattr(self, attribute, max(getattr(self, attribute), time.monotonic() + max(backoff, seconds)))

    def fetch(self, query, hours):
        """Cache only validated feeds; failures never turn old data into success."""
        params = urllib.parse.urlencode({'q': f'({query}) when:{hours}h', 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'})
        url = 'https://news.google.com/rss/search?' + params
        return self._fetch_url(url, 'google')

    def fetch_bing(self, query, hours):
        # Bing has no Google `when:` operator. The time window is checked locally.
        params = urllib.parse.urlencode({'q': query, 'format': 'rss', 'setlang': 'ko-KR',
                                        'cc': 'KR', 'sortby': 'date', 'count': 50})
        return self._fetch_url('https://www.bing.com/news/search?' + params, 'bing')

    def _fetch_url(self, url, provider):
        if provider != 'google' or not self.google_request_interval:
            return self._read_url(url, provider)
        cancelled = getattr(self.context, 'cancelled', lambda: False)
        while not self.google_request_lock.acquire(timeout=.1):
            if cancelled():
                raise ServerCoolingDown('뉴스 수집 취소')
            if self.cooldown_remaining(provider):
                raise ServerCoolingDown(provider + ' 뉴스 서버 일시 중단 · 잠시 후 재시도')
        try:
            while True:
                if cancelled():
                    raise ServerCoolingDown('뉴스 수집 취소')
                if self.cooldown_remaining(provider):
                    raise ServerCoolingDown(provider + ' 뉴스 서버 일시 중단 · 잠시 후 재시도')
                remaining = self.google_next_request - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(.1, remaining))
            self.google_next_request = time.monotonic() + self.google_request_interval
            return self._read_url(url, provider)
        finally:
            self.google_request_lock.release()

    def _read_url(self, url, provider):
        if self.cooldown_remaining(provider):
            raise ServerCoolingDown(provider + ' 뉴스 서버 일시 중단 · 잠시 후 재시도')
        with self.lock:
            cached = self.cache.get(url)
        headers = dict(RSS_HEADERS)
        if cached:
            if cached[0]:
                headers['If-None-Match'] = cached[0]
            if cached[1]:
                headers['If-Modified-Since'] = cached[1]
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                etag = response.headers.get('ETag')
                modified = response.headers.get('Last-Modified')
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached:
                exc.close()
                with self.lock:
                    self.failures[provider] = 0
                return cached[2]
            if exc.code in (429, 503):
                self._cool_down(exc.headers, provider)
            exc.close()
            raise
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('뉴스 응답 크기가 너무 큽니다.')
        articles = parse_feed(data)
        with self.lock:
            self.failures[provider] = 0
            self.cache[url] = (etag, modified, articles)
            self.cache.move_to_end(url)
            while len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        return articles

    def collect(self, entries, hours=24, cancelled=lambda: False, on_result=None, on_progress=None):
        primary = self._collect_provider(entries, hours, cancelled, on_result, on_progress, 'google')
        if not self.fallback or not primary.errors or primary.cancelled or cancelled():
            return primary
        failed = {name for name, error in primary.errors}
        alternate = self._collect_provider([entry for entry in entries if entry['name'] in failed],
                                           hours, cancelled, on_result, on_progress, 'bing')
        recovered = {name for name, articles in alternate.results}
        primary.results.extend(alternate.results)
        primary.errors = [(name, error) for name, error in primary.errors if name not in recovered]
        alternate_errors = dict(alternate.errors)
        primary.errors = [(name, alternate_errors.get(name, error)) for name, error in primary.errors]
        primary.requests += alternate.requests
        primary.seconds += alternate.seconds
        primary.cancelled = primary.cancelled or alternate.cancelled
        primary.provider = 'google+bing' if len(primary.results) > len(alternate.results) else 'bing'
        primary.fallback_reason = 'Google RSS 연결 실패 · Bing RSS 대체 수집'
        # Google's cooldown must not prevent the healthy alternate from being used.
        primary.cooldown = alternate.cooldown
        return primary

    def _collect_provider(self, entries, hours, cancelled, on_result, on_progress, provider):
        """Share identical queries, filter per rule, and publish complete rules early.

        Only this coordinator calls callbacks. SQLite/Tk remain on the UI thread.
        No more than workers tasks are submitted, so cancellation drops queued work.
        """
        started = time.monotonic()
        deadline = started + self.cycle_timeout
        stop = threading.Event()
        def stopped():
            return stop.is_set() or cancelled() or time.monotonic() >= deadline
        checked_at = time.time()
        rules = {entry['name']: SearchRule(entry) for entry in entries}
        queries = {}
        remaining = {}
        matches = {name: {} for name in rules}
        failures, results = {}, []
        for name, rule in rules.items():
            # Grouped company/topic OR queries can yield a valid but empty Google
            # feed while simple company queries return matching current news.
            # Fetch candidates per alias and apply the complete rule locally.
            candidates = rule.bing_queries if provider == 'bing' or hasattr(rule, 'company') else rule.queries
            unique = list(dict.fromkeys(candidates))
            remaining[name] = len(unique)
            for query in unique:
                queries.setdefault(query, []).append(name)
        queue = iter(queries)
        pending = {}
        count, finished = 0, 0
        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='rss')
        was_cancelled = False
        timed_out = False

        def fetch_query(query):
            self.context.cancelled = stopped
            try:
                method = self.fetch if provider == 'google' else self.fetch_bing
                return method(query, hours)
            finally:
                del self.context.cancelled

        def fill():
            nonlocal count
            while len(pending) < self.workers and not stopped() and not self.cooldown_remaining(provider):
                query = next(queue, None)
                if query is None:
                    break
                # Failed conditions need no additional requests; shared users still do.
                if all(name in failures for name in queries[query]):
                    continue
                pending[executor.submit(fetch_query, query)] = query
                count += 1

        try:
            fill()
            while pending:
                if cancelled():
                    was_cancelled = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                done, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    query = pending.pop(future)
                    finished += 1
                    if cancelled():
                        was_cancelled = True
                        break
                    try:
                        articles = future.result()
                    except Exception as exc:
                        for name in queries[query]:
                            failures.setdefault(name, exc)
                    else:
                        for name in queries[query]:
                            if name not in failures:
                                rule = rules[name]
                                for article in articles:
                                    if not checked_at - hours * 3600 <= article.published <= checked_at + 300:
                                        continue
                                    accepted = rule.matches(article.title, article.description) if provider == 'bing' else rule.accepts(article)
                                    if accepted:
                                        matches[name][article.url] = article
                    for name in queries[query]:
                        remaining[name] -= 1
                        if remaining[name] == 0 and name not in failures:
                            articles = sorted(matches[name].values(), key=lambda a: a.published, reverse=True)
                            results.append((name, articles))
                            if on_result:
                                on_result(name, articles)
                    if on_progress:
                        on_progress(finished, len(queries), queries[query][0])
                if was_cancelled:
                    break
                fill()
            if cancelled():
                was_cancelled = True
            if time.monotonic() >= deadline:
                timed_out = True
            if not was_cancelled:
                for name, amount in remaining.items():
                    if amount and name not in failures:
                        failures[name] = (TimeoutError(provider + ' 뉴스 수집 대기 시간 초과') if timed_out else
                                          ServerCoolingDown(provider + ' 뉴스 서버 일시 중단 · 잠시 후 재시도'))
        finally:
            stop.set()
            for future in pending:
                future.cancel()
            # Socket timeouts do not bound DNS or slowly trickling responses.
            # Abandon stuck requests; only this coordinator publishes results.
            executor.shutdown(wait=not (was_cancelled or timed_out), cancel_futures=True)
            if timed_out:
                # Let Bing handle subsequent cycles while the old Google request
                # releases its pacing lock, without accumulating blocked workers.
                self._cool_down({}, provider)
        return CollectionReport(results, list(failures.items()), count, time.monotonic() - started,
                                self.cooldown_remaining(provider), was_cancelled, provider)
