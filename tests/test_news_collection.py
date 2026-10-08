from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from news_collection import NewsCollector, ServerCoolingDown
from news_core import Article, Store
from security_profiles import COMPANIES, TOPICS, make_entries

FEED = b'<rss><channel></channel></rss>'


class Response(BytesIO):
    def __init__(self, body=FEED, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class CollectionTests(unittest.TestCase):
    def test_stuck_primary_times_out_and_bing_recovers_without_late_callbacks(self):
        release, finished = threading.Event(), threading.Event()
        client = NewsCollector(workers=1, fallback=True, cycle_timeout=.15, google_request_interval=0)
        published = []
        def hung_fetch(query, hours):
            release.wait(3)
            finished.set()
            return []
        try:
            with patch.object(client, 'fetch', side_effect=hung_fetch), patch.object(client, 'fetch_bing', return_value=[]):
                start = time.monotonic()
                report = client.collect([{'name': 'news'}], on_result=lambda n, a: published.append(n))
                self.assertLess(time.monotonic() - start, 1)
                self.assertFalse(report.errors)
                self.assertEqual(report.provider, 'bing')
                self.assertEqual(published, ['news'])
                self.assertGreater(client.cooldown_remaining(), 0)
                again = client.collect([{'name': 'news'}])
                self.assertFalse(again.errors)
                self.assertEqual(again.provider, 'bing')
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        self.assertEqual(published, ['news'])

    def test_stuck_request_reports_timeout_and_cancellation_returns_promptly(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                release, started, finished = threading.Event(), threading.Event(), threading.Event()
                stop = threading.Event()
                client = NewsCollector(workers=1, cycle_timeout=.15, google_request_interval=0)
                published = []
                def hung_fetch(query, hours):
                    started.set()
                    release.wait(3)
                    finished.set()
                    return []
                try:
                    with patch.object(client, 'fetch', side_effect=hung_fetch), ThreadPoolExecutor(max_workers=1) as controller:
                        task = controller.submit(client.collect, [{'name': 'news'}], cancelled=stop.is_set,
                                                 on_result=lambda n, a: published.append(n))
                        self.assertTrue(started.wait(1))
                        if cancel:
                            stop.set()
                        report = task.result(timeout=1)
                    self.assertEqual(report.cancelled, cancel)
                    self.assertEqual(report.results, [])
                    if not cancel:
                        self.assertIsInstance(report.errors[0][1], TimeoutError)
                finally:
                    release.set()
                    self.assertTrue(finished.wait(2))
                self.assertEqual(published, [])

    def test_google_headers_and_spacing_across_cycles(self):
        client = NewsCollector(google_request_interval=.05)
        starts = []
        active, peak = 0, 0

        def network(request, timeout):
            nonlocal active, peak
            self.assertIn('Mozilla/5.0', request.get_header('User-agent'))
            self.assertIn('ko-KR', request.get_header('Accept-language'))
            starts.append(time.monotonic())
            active += 1
            peak = max(peak, active)
            time.sleep(.01)
            active -= 1
            return Response()

        with patch('news_collection.urllib.request.urlopen', side_effect=network):
            first = client.collect([{'name': str(i)} for i in range(4)])
            second = client.collect([{'name': 'next'}])
        self.assertFalse(first.errors or second.errors)
        self.assertEqual(peak, 1)
        self.assertEqual(len(starts), 5)
        self.assertTrue(all(b - a >= .045 for a, b in zip(starts, starts[1:])))

    def test_paced_google_503_stops_waiting_requests(self):
        client = NewsCollector(google_request_interval=.05)
        error = urllib.error.HTTPError('url', 503, 'busy', {}, None)
        with patch('news_collection.urllib.request.urlopen', side_effect=error) as network:
            report = client.collect([{'name': str(i)} for i in range(20)])
        self.assertEqual(network.call_count, 1)
        self.assertEqual(len(report.errors), 20)
        self.assertGreater(report.cooldown, 290)

    def test_cancellation_interrupts_google_spacing_wait(self):
        client = NewsCollector(google_request_interval=5)
        cancel = threading.Event()
        published = []
        with patch('news_collection.urllib.request.urlopen', return_value=Response()) as network:
            report = client.collect([{'name': str(i)} for i in range(8)], cancelled=cancel.is_set,
                                    on_result=lambda name, articles: (published.append(name), cancel.set()))
        self.assertTrue(report.cancelled)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(len(published), 1)
        self.assertLess(report.seconds, 1)

    def test_backoff_escalates_resets_and_honors_long_retry_after(self):
        client = NewsCollector(google_request_interval=0)
        with patch('news_collection.time.monotonic', return_value=100):
            client._cool_down({})
            self.assertEqual(client.cooldown_remaining(), 300)
            client._cool_down({})
            self.assertEqual(client.cooldown_remaining(), 600)
            client._cool_down({'Retry-After': '7200'})
            self.assertEqual(client.cooldown_remaining(), 7200)
            client.cooldown_until = 0
            with patch('news_collection.urllib.request.urlopen', return_value=Response()):
                client.fetch('recovery', 24)
            client._cool_down({})
            self.assertEqual(client.cooldown_remaining(), 300)

    def test_parallel_bound_shared_request_and_independent_filters(self):
        entries = [{'name': str(i), 'condition': 'company AND news', 'scope': 'title' if i % 2 else 'title_description'} for i in range(8)]
        entries += [{'name': 'other' + str(i), 'condition': 'other' + str(i)} for i in range(7)]
        client = NewsCollector(google_request_interval=0)
        lock = threading.Lock()
        active, peak, calls = 0, 0, 0
        article = Article('https://example.com/a', 'company announcement', '', time.time(), 'news')
        def fetch(query, hours):
            nonlocal active, peak, calls
            with lock:
                active += 1
                calls += 1
                peak = max(peak, active)
            time.sleep(.025)
            with lock:
                active -= 1
            return [article]
        with patch.object(client, 'fetch', side_effect=fetch):
            report = client.collect(entries)
        self.assertEqual(calls, 8)  # Eight duplicate rules share one network request.
        self.assertEqual(peak, 4)
        self.assertFalse(report.errors)
        results = dict(report.results)
        self.assertEqual(len(results['0']), 1)
        self.assertEqual(results['1'], [])  # Scope still applied independently.

    def test_completed_rules_are_published_without_waiting_for_slow_request(self):
        release = threading.Event()
        published = []
        client = NewsCollector(google_request_interval=0)
        entries = [{'name': 'slow'}, {'name': 'fast'}]
        def fetch(query, hours):
            if 'slow' in query:
                self.assertTrue(release.wait(2))
            return []
        def result(name, articles):
            published.append(name)
            if name == 'fast':
                release.set()
        with patch.object(client, 'fetch', side_effect=fetch):
            report = client.collect(entries, on_result=result)
        self.assertEqual(published, ['fast', 'slow'])
        self.assertEqual(len(report.results), 2)

    def test_failed_company_alias_never_publishes_partial_success(self):
        entries = make_entries('한국전력', COMPANIES['한국전력'], TOPICS)
        client = NewsCollector(google_request_interval=0)
        published = []
        def fetch(query, hours):
            if query == '"한전"':
                raise TimeoutError('failed company alias')
            return [Article('https://example.com/a', '한전 해킹 과징금', '', 100, '')]
        with patch.object(client, 'fetch', side_effect=fetch):
            report = client.collect(entries, on_result=lambda n, a: published.append(n))
        self.assertEqual(published, [])
        self.assertCountEqual([name for name, error in report.errors], [entry['name'] for entry in entries])

    def test_google_company_candidates_avoid_empty_or_queries_and_preserve_filters(self):
        entries = make_entries('한국전력', COMPANIES['한국전력'], TOPICS)
        client = NewsCollector(google_request_interval=0)
        now = time.time()
        articles = [Article('https://example.com/' + str(i), title, '신문', published, '')
                    for i, (title, published) in enumerate([
                        ('한전 개인정보 노출 과징금', now),
                        ('한전KDN 개인정보 노출', now),
                        ('한국전력 발전소 건설', now),
                        ('한전 개인정보 노출', now - 25 * 3600),
                        ('한전 개인정보 노출', now + 3600)])]
        def fetch(query, hours):
            return [] if ' OR ' in query else articles
        with patch.object(client, 'fetch', side_effect=fetch) as google:
            report = client.collect(entries)
        self.assertFalse(report.errors)
        self.assertEqual(google.call_count, len(COMPANIES['한국전력']))
        self.assertEqual({call.args[0] for call in google.call_args_list},
                         {'"한국전력"', '"한국전력공사"', '"한전"'})
        for name, matches in report.results:
            self.assertEqual([article.url for article in matches], ['https://example.com/0'])

    def test_cancellation_stops_unscheduled_requests_and_callbacks(self):
        cancel = threading.Event()
        started = threading.Event()
        client = NewsCollector(workers=2, google_request_interval=0)
        calls, published = [], []
        def fetch(query, hours):
            calls.append(query)
            started.set()
            cancel.wait(2)
            return []
        with patch.object(client, 'fetch', side_effect=fetch), ThreadPoolExecutor(max_workers=1) as controller:
            pending = controller.submit(client.collect, [{'name': str(i)} for i in range(30)],
                                        cancelled=cancel.is_set, on_result=lambda n, a: published.append(n))
            self.assertTrue(started.wait(1))
            cancel.set()
            report = pending.result(timeout=2)
        self.assertTrue(report.cancelled)
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(published, [])

    def test_503_opens_circuit_and_honors_retry_after(self):
        client = NewsCollector(google_request_interval=0)
        error = urllib.error.HTTPError('https://news.google.com', 503, 'unavailable', {'Retry-After': '120'}, None)
        with patch('news_collection.urllib.request.urlopen', side_effect=error) as network:
            report = client.collect([{'name': str(i)} for i in range(22)])
            self.assertLessEqual(network.call_count, 4)
            before = network.call_count
            again = client.collect([{'name': 'new'}])
            self.assertEqual(network.call_count, before)
        self.assertEqual(len(report.errors), 22)
        self.assertGreater(report.cooldown, 110)
        self.assertIsInstance(again.errors[0][1], ServerCoolingDown)

    def test_conditional_cache_304_and_failed_refresh(self):
        client = NewsCollector(google_request_interval=0)
        body = '''<rss><channel><item><title>한전 해킹</title><link>https://example.com/a</link><pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate></item></channel></rss>'''.encode()
        first = Response(body, {'ETag': '"v1"', 'Last-Modified': 'Sun, 04 Oct 2026 03:00:00 GMT'})
        not_modified = urllib.error.HTTPError('url', 304, 'not modified', {}, None)
        with patch('news_collection.urllib.request.urlopen', side_effect=[first, not_modified]) as network:
            original = client.fetch('one', 24)
            refreshed = client.fetch('one', 24)
        self.assertEqual(original, refreshed)
        self.assertEqual(network.call_args.args[0].get_header('If-none-match'), '"v1"')
        with patch('news_collection.urllib.request.urlopen', side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                client.fetch('one', 24)
        with patch('news_collection.urllib.request.urlopen', return_value=Response()) as network:
            client.fetch('one', 48)
        self.assertIsNone(network.call_args.args[0].get_header('If-none-match'))

    def test_all_default_profiles_keep_two_tags_and_do_not_cross_companies(self):
        entries = [entry for company, aliases in COMPANIES.items() for entry in make_entries(company, aliases, TOPICS)]
        client = NewsCollector(google_request_interval=0)
        article = Article('https://example.com/a', '한국남동발전 랜섬웨어 피해 과징금', '신문', time.time(), '')
        with patch.object(client, 'fetch', return_value=[article]):
            report = client.collect(entries)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                for name, articles in report.results:
                    store.ingest(name, articles)
                rows = store.list_articles([entry['name'] for entry in entries])
                self.assertEqual(len(rows), 1)
                self.assertEqual(set(rows[0]['keywords']), {'한국남동발전 · 공격 유형', '한국남동발전 · 개인정보위·정부 대응'})
            finally:
                store.close()

    def test_bing_fallback_filters_boolean_conditions_scope_and_time(self):
        client = NewsCollector(fallback=True, google_request_interval=0)
        now = time.time()
        articles = [Article('https://example.com/' + str(i), title, '', published, description)
                    for i, (title, description, published) in enumerate([
                        ('company news', '', now), ('company alert', '', now),
                        ('company stock news', '', now), ('other news', '', now),
                        ('company announcement', 'news', now),
                        ('company news', '', now - 25 * 3600),
                        ('company news', '', now + 3600)])]
        entry = {'name': 'monitor', 'condition': 'company AND (news OR alert) NOT stock', 'scope': 'title'}
        error = urllib.error.HTTPError('https://news.google.com', 503, 'unavailable', {}, None)
        with patch.object(client, 'fetch', side_effect=error), patch.object(client, 'fetch_bing', return_value=articles) as bing:
            report = client.collect([entry])
        error.close()
        self.assertFalse(report.errors)
        self.assertEqual({a.url for a in dict(report.results)['monitor']},
                         {'https://example.com/0', 'https://example.com/1'})
        self.assertEqual({call.args[0] for call in bing.call_args_list},
                         {'"company" "news"', '"company" "alert"'})
        self.assertEqual(report.provider, 'bing')
        self.assertIn('Bing', report.fallback_reason)

    def test_fallback_recovers_only_failed_rules_and_publishes_once(self):
        client = NewsCollector(fallback=True, google_request_interval=0)
        published = []
        def fetch(query, hours):
            if 'bad' in query:
                raise TimeoutError('Google unavailable')
            return [Article('https://example.com/good', 'good news', '', time.time(), '')]
        with patch.object(client, 'fetch', side_effect=fetch), patch.object(client, 'fetch_bing',
                return_value=[Article('https://example.com/bad', 'bad news', '', time.time(), '')]) as bing:
            report = client.collect([{'name': 'good'}, {'name': 'bad'}],
                                    on_result=lambda name, articles: published.append(name))
        self.assertCountEqual(published, ['good', 'bad'])
        self.assertFalse(report.errors)
        self.assertEqual(report.provider, 'google+bing')
        self.assertEqual(bing.call_args.args[0], '"bad"')

    def test_google_cooldown_does_not_block_bing_and_each_provider_cools_independently(self):
        client = NewsCollector(fallback=True, google_request_interval=0)
        def network(request, timeout):
            if urllib.parse.urlsplit(request.full_url).hostname == 'news.google.com':
                raise urllib.error.HTTPError(request.full_url, 503, 'unavailable', {'Retry-After': '120'}, None)
            return Response()
        with patch('news_collection.urllib.request.urlopen', side_effect=network) as opened:
            first = client.collect([{'name': 'one'}])
            again = client.collect([{'name': 'two'}])
        self.assertFalse(first.errors)
        self.assertFalse(again.errors)
        self.assertEqual(opened.call_count, 3)  # Google once, Bing twice.
        self.assertGreater(client.cooldown_remaining('google'), 110)
        self.assertEqual(client.cooldown_remaining('bing'), 0)
        self.assertEqual(again.cooldown, 0)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened.call_args.args[0].full_url).query)['q'][0]
        self.assertNotIn('when:', query)
        error = urllib.error.HTTPError('https://www.bing.com', 429, 'busy', {'Retry-After': '120'}, None)
        with patch('news_collection.urllib.request.urlopen', side_effect=error):
            failed = client.collect([{'name': 'three'}])
        self.assertEqual(failed.results, [])
        self.assertEqual(len(failed.errors), 1)
        self.assertGreater(failed.cooldown, 110)

    def test_cancelled_primary_does_not_start_bing(self):
        client = NewsCollector(fallback=True, google_request_interval=0)
        cancel = threading.Event()
        def fetch(query, hours):
            cancel.set()
            raise TimeoutError('cancelled')
        with patch.object(client, 'fetch', side_effect=fetch), patch.object(client, 'fetch_bing') as bing:
            report = client.collect([{'name': 'one'}], cancelled=cancel.is_set)
        self.assertTrue(report.cancelled)
        bing.assert_not_called()

    def test_bing_shares_company_alias_requests_and_keeps_topic_and_company_filters(self):
        entries = (make_entries('한국전력', COMPANIES['한국전력'], TOPICS)
                   + make_entries('한전KDN', COMPANIES['한전KDN'], TOPICS))
        client = NewsCollector(fallback=True, google_request_interval=0)
        articles = [Article('https://example.com/kdn', '한전KDN 해킹 과징금', '', time.time(), ''),
                    Article('https://example.com/kepco', '한전 개인정보 노출', '', time.time(), '')]
        with patch.object(client, 'fetch', side_effect=TimeoutError()), patch.object(client, 'fetch_bing', return_value=articles) as bing:
            report = client.collect(entries)
        self.assertFalse(report.errors)
        self.assertEqual(bing.call_count, len(set(COMPANIES['한국전력'] + COMPANIES['한전KDN'])))
        results = dict(report.results)
        self.assertEqual([a.url for a in results['한국전력 · 공격 유형']], ['https://example.com/kepco'])
        self.assertEqual(results['한국전력 · 개인정보위·정부 대응'], [])
        self.assertEqual([a.url for a in results['한전KDN · 개인정보위·정부 대응']], ['https://example.com/kdn'])
