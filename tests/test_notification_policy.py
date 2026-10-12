import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from kakao_notifications import Notifications
from telegram_notifications import TelegramNotifications


class StoryDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.article = dict(url='https://example.com/a', title='한국전력 개인정보 1000명 유출 발생',
                            description='개인정보 유출 사고 확인', source='신문',
                            published=time.time(), discovered=time.time() + 1)
        self.kakao = Notifications(self.directory)
        self.kakao.save_user('사용자', 'test-token')
        self.telegram = TelegramNotifications(self.directory)
        with self.telegram.connect() as db:
            db.execute("UPDATE settings SET enabled=1,chat_id=-100,registered=0,token='token'")

    def drain(self, service, calls):
        if isinstance(service, Notifications):
            service.deliver(sender=lambda token, article: (calls.append(article['url']) or ('sent', 'OK')))
        else:
            with service.connect() as db:
                db.execute('UPDATE settings SET next_send=0')
            with patch('telegram_notifications.protect', return_value='token'):
                service.deliver(request=lambda token, method, fields: (calls.append(fields) or {'message_id': 1}))

    def test_same_story_new_url_suppressed_after_restart_both_channels(self):
        for service, cls in ((self.kakao, Notifications), (self.telegram, TelegramNotifications)):
            with self.subTest(channel=cls.__name__):
                calls = []
                service.enqueue([self.article])
                self.drain(service, calls)
                restarted = cls(self.directory)
                restarted.enqueue([{**self.article, 'url': 'https://other.com/b',
                                    'title': '[종합] 한국전력 개인정보 1천명 유출 발생'}])
                self.drain(restarted, calls)
                self.assertEqual(len(calls), 1)

    def test_legacy_pending_duplicates_removed_before_send(self):
        for service, owner, value in ((self.kakao, 'recipient', 1), (self.telegram, 'chat_id', -100)):
            with service.connect() as db:
                for article in (self.article, {**self.article, 'url': 'https://other.com/b'}):
                    db.execute(f'INSERT INTO deliveries({owner},url,article) VALUES(?,?,?)',
                               (value, article['url'], json.dumps(article)))
            calls = []
            self.drain(service, calls)
            with service.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries WHERE state='skipped'").fetchone()[0], 1)
            self.assertEqual(len(calls), 1)

    def test_related_member_survives_representative_change(self):
        first = {**self.article, 'related_articles': [self.article,
                 {**self.article, 'url': 'https://example.com/alias', 'title': '다른 표현의 제목'}]}
        self.kakao.enqueue([first])
        calls = []
        self.drain(self.kakao, calls)
        self.kakao.enqueue([{**self.article, 'url': 'https://example.com/alias', 'title': '다른 표현의 제목'}])
        self.drain(self.kakao, calls)
        self.assertEqual(len(calls), 1)

    def test_tracking_urls_are_duplicates_but_other_events_are_sent(self):
        self.kakao.enqueue([self.article,
            {**self.article, 'url': self.article['url'] + '?utm_source=rss#top', 'title': '제목 변경'},
            {**self.article, 'url': 'https://example.com/new', 'title': '한국전력 개인정보 2000명 유출 발생'}])
        calls = []
        self.drain(self.kakao, calls)
        self.assertEqual(len(calls), 2)

    def test_no_importance_filter_and_paid_broadcast_disabled(self):
        article = {**self.article, 'title': '지역 문화 행사 개최'}
        self.telegram.enqueue([article])
        calls = []
        self.drain(self.telegram, calls)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]['allow_paid_broadcast'], False)

    def test_pending_and_unknown_are_duplicate_anchors(self):
        self.telegram.enqueue([self.article, {**self.article, 'url': 'https://other.com/b'}])
        with self.telegram.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries WHERE state='pending'").fetchone()[0], 1)
            db.execute("UPDATE deliveries SET state='unknown' WHERE state='pending'")
        self.telegram.enqueue([{**self.article, 'url': 'https://third.com/c'}])
        calls = []
        self.drain(self.telegram, calls)
        self.assertEqual(calls, [])
        with self.telegram.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries WHERE state='pending'").fetchone()[0], 0)

