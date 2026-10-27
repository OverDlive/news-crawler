"""A completed background body match must reach both durable sharing queues."""
import json
from contextlib import closing
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app import NewsMonitor
from kakao_notifications import Notifications
from telegram_notifications import TelegramNotifications
from news_core import Article, Store
from news_similarity import compare_bodies
from news_summary import Summary


BODY = ('OK저축은행은 대출 신청 고객 250명의 개인정보 유출을 확인했다. '
        '이름과 생년월일, 계좌번호와 연락처 등 신청 과정에서 수집한 정보가 포함됐다. '
        '외부 보안 전문 관제업체와 접속 기록을 조사하고 피해 고객들에게 개별 통지했다. '
        '은행은 해당 시스템을 차단하고 접근 권한을 다시 점검했다. '
        '추가 피해를 막기 위한 안내와 고객 상담창구도 운영하고 있다. '
        '확인된 내용을 관계 기관에 신고했으며 조사 결과에 따라 재발 방지 대책을 보완할 예정이다.')


class BodySharingTests(unittest.TestCase):
    def test_send_rechecks_body_cache_without_waiting_for_grouped_snapshot(self):
        """The old sent article can be outside the currently displayed 24h feed."""
        with tempfile.TemporaryDirectory() as directory, closing(Store(Path(directory))) as store:
            config = store.load_config()
            config['sharing_schedule']['enabled'] = False
            store.save_config(config)
            now = time.time() + 1
            a = Article('https://example.com/a', 'OK저축은행 고객 250명 개인정보 유출', 'A', now - 36 * 3600, '')
            b = Article('https://example.com/b', '대출 신청자 정보 노출…외부 접속 경로 추적', 'B', now, '')
            for item in (a, b):
                store.ingest('뉴스', [item], now)
                store.save_summary(item.url, Summary('본문 요약', 'body', '', '', BODY))
            rows = store.list_articles(['뉴스'], hours=48, now=now, grouped=False)
            first = next(r for r in rows if r['url'] == a.url)
            second = next(r for r in rows if r['url'] == b.url)
            services = (Notifications(Path(directory)), TelegramNotifications(Path(directory)))
            services[0].save_user('검증', 'test-token')
            with services[1].connect() as db:
                db.execute("UPDATE settings SET enabled=1,chat_id=-100,registered=0,token='token'")
            counts = {service: [] for service in services}

            def drain(service):
                if service is services[0]:
                    service.deliver(sender=lambda *args: (counts[service].append(1) or ('sent', 'OK')))
                else:
                    with service.connect() as db:
                        db.execute('UPDATE settings SET next_send=0')
                    with patch('telegram_notifications.protect', return_value='token'), patch(
                            'telegram_notifications.telegram_preview_url', return_value=b.url):
                        service.deliver(request=lambda *args: (counts[service].append(1) or {'message_id': 1}))

            for service in services:
                service.enqueue([first])
                drain(service)
                service.enqueue([second])
                self.assertEqual(len(counts[service]), 1)
            store.save_comparisons(compare_bodies(rows, set(), threading.Event()))
            visible = store.list_articles(['뉴스'], now=now)
            self.assertEqual([r['url'] for r in visible], [b.url])
            # No enqueue(grouped) call: the send-time guard must see the cache.
            for service in services:
                drain(service)
                self.assertEqual(len(counts[service]), 1)
                with service.connect() as db:
                    self.assertEqual(db.execute('SELECT state FROM deliveries WHERE url=?', (b.url,)).fetchone()[0], 'skipped')

    def test_store_marks_unfinished_analysis_and_clears_it_after_fallback_or_comparison(self):
        with tempfile.TemporaryDirectory() as directory, closing(Store(Path(directory))) as store:
            now = time.time()
            a = Article('a', 'OK저축은행 고객 250명 개인정보 유출', 'A', now, '')
            b = Article('b', '대출 신청자 정보 노출…외부 접속 경로 추적', 'B', now, '')
            store.ingest('뉴스', [a], now)
            self.assertTrue(store.list_articles(['뉴스'], now=now)[0]['dedup_pending'])
            store.save_summary(a.url, Summary('RSS 요약', 'rss', '', ''))
            self.assertFalse(store.list_articles(['뉴스'], now=now)[0]['dedup_pending'])
            store.ingest('뉴스', [b], now)
            for item in (a, b):
                store.save_summary(item.url, Summary('본문 요약', 'body', '', '', BODY))
            self.assertTrue(all(r['dedup_pending'] for r in store.list_articles(['뉴스'], now=now)))
            rows = store.list_articles(['뉴스'], now=now, grouped=False)
            store.save_comparisons(compare_bodies(rows, set(), threading.Event()))
            grouped = store.list_articles(['뉴스'], now=now)
            self.assertEqual(len(grouped), 1)
            self.assertFalse(grouped[0]['dedup_pending'])

    def test_stale_body_cache_and_explicit_conflicts_do_not_suppress(self):
        from notification_policy import duplicate, stored_body_matches
        with tempfile.TemporaryDirectory() as directory, closing(Store(Path(directory))) as store:
            now = time.time()
            for url in ('a', 'b'):
                store.ingest('뉴스', [Article(url, 'OK저축은행 고객 정보 유출 확인', url, now, '')], now)
                store.save_summary(url, Summary('요약', 'body', '', '', BODY))
            rows = store.list_articles(['뉴스'], now=now, grouped=False)
            store.save_comparisons(compare_bodies(rows, set(), threading.Event()))
            self.assertGreaterEqual(stored_body_matches(directory, rows)[('a', 'b')], .82)
            store.save_summary('b', Summary('수정', 'body', '', '', BODY.replace('250명', '500명')))
            self.assertEqual(stored_body_matches(directory, rows), {})
            rows = store.list_articles(['뉴스'], now=now, grouped=False)
            store.save_comparisons(compare_bodies(rows, set(), threading.Event()))
            evidence = stored_body_matches(directory, rows)
            self.assertLess(evidence[('a', 'b')], 0)
            self.assertFalse(duplicate(*rows, evidence))

    def test_grouped_event_rechecks_sent_story_before_another_url_is_shared(self):
        with tempfile.TemporaryDirectory() as directory, closing(Store(Path(directory))) as store:
            config = store.load_config()
            config['sharing_schedule']['enabled'] = False
            store.save_config(config)
            path = Path(directory)
            now = time.time() + 1
            a = Article('https://example.com/a', 'OK저축은행 고객 250명 개인정보 유출', 'A', now - 2, '')
            b = Article('https://example.com/b', 'OK저축은행 정보 유출 확인…2차 피해 차단 총력', 'B', now - 1, '')
            for item in (a, b):
                store.ingest('뉴스', [item], now)
                store.save_summary(item.url, Summary('본문 요약', 'body', '', '', BODY))
            rows = store.list_articles(['뉴스'], now=now, grouped=False)
            kakao, telegram = Notifications(path), TelegramNotifications(path)
            kakao.save_user('검증', 'test-token')
            with telegram.connect() as db:
                db.execute("UPDATE settings SET enabled=1,chat_id=-100,registered=0,token='token'")
            calls = {kakao: [], telegram: []}

            def drain(service):
                if service is kakao:
                    service.deliver(sender=lambda token, article:
                                    (calls[service].append(article['url']) or ('sent', 'OK')))
                else:
                    with service.connect() as db:
                        db.execute('UPDATE settings SET next_send=0')
                    with patch('telegram_notifications.protect', return_value='token'):
                        service.deliver(request=lambda *args:
                                        (calls[service].append(args) or {'message_id': 1}))

            first = next(row for row in rows if row['url'] == a.url)
            for service in calls:
                service.enqueue([first])
                drain(service)
                self.assertEqual(len(calls[service]), 1)

            # Exercise the real event handler without starting Tk or workers.
            app = NewsMonitor.__new__(NewsMonitor)
            app.store, app.demo, app.selected = store, False, '전체'
            app.events, app.grouping_busy = queue.Queue(), True
            app.reload = lambda: store.list_articles(['뉴스'], now=now)
            app.start_notification_enqueue = Mock()
            app.notification_pending = None
            app.events.put(('grouped', compare_bodies(rows, set(), threading.Event())))
            app.process_events()
            self.assertFalse(app.grouping_busy)
            self.assertIsNotNone(app.notification_pending)
            self.assertEqual(len(app.notification_pending), 1)
            self.assertEqual(app.notification_pending[0]['related_count'], 2)
            for service in calls:
                service.enqueue(app.notification_pending)
                drain(service)
                self.assertEqual(len(calls[service]), 1)
                with service.connect() as db:
                    raw = db.execute('SELECT article,state FROM deliveries WHERE url=?', (b.url,)).fetchone()
                    self.assertEqual(raw[1], 'skipped')
                    self.assertNotIn('body', json.loads(raw[0]))
