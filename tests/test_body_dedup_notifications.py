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
