import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from kakao_notifications import Notifications
from telegram_notifications import TelegramNotifications
from notification_policy import duplicate, ANALYSIS_WAIT


class StoryDeliveryTests(unittest.TestCase):
    def test_reported_title_republication_is_suppressed_for_seven_days(self):
        original = {**self.article, 'title': '바다위를 막는다...KISA, 선박용 침해사고 성능 시험장 공개'}
        repeated = {**original, 'url': 'https://other.com/repeat', 'source': '다른신문',
                    'title': '[종합] 바다 위를 막는다…한국인터넷진흥원, 선박용 침해사고 성능시험장 공개 - 다른신문',
                    'published': original['published'] + 3 * 86400}
        self.assertTrue(duplicate(original, repeated))
        self.assertFalse(duplicate(original, {**repeated, 'published': original['published'] + 8 * 86400}))
        for service, cls in ((self.kakao, Notifications), (self.telegram, TelegramNotifications)):
            calls = []
            service.enqueue([original])
            self.drain(service, calls)
            restarted = cls(self.directory)
            restarted.enqueue([repeated])
            self.drain(restarted, calls)
            self.assertEqual(len(calls), 1)

    def test_resolved_original_url_suppresses_different_feed_links(self):
        a = {**self.article, 'content_url': 'https://publisher.com/story?utm_source=a'}
        b = {**self.article, 'url': 'https://feed.com/other', 'title': '다른 표현의 기사',
             'content_url': 'https://publisher.com/story#top'}
        self.assertTrue(duplicate(a, b))

    def test_decimal_fact_changes_are_not_identical_titles(self):
        a = {**self.article, 'title': 'KISA 침해사고 탐지 비율 6.8% 발표'}
        b = {**self.article, 'url': 'https://example.com/new', 'title': 'KISA 침해사고 탐지 비율 68% 발표'}
        self.assertFalse(duplicate(a, b))

    def test_analysis_wait_survives_restart_and_refresh_without_extending(self):
        for service, cls in ((self.kakao, Notifications), (self.telegram, TelegramNotifications)):
            with self.subTest(channel=cls.__name__):
                article = {**self.article, 'dedup_pending': True}
                calls = []
                clock = time.time()
                with patch('notification_policy.time.time', return_value=clock):
                    service.enqueue([article])
                    self.drain(service, calls)
                self.assertEqual(calls, [])
                restarted = cls(self.directory)
                with patch('notification_policy.time.time', return_value=clock + ANALYSIS_WAIT - 1):
                    restarted.enqueue([article])
                    self.drain(restarted, calls)
                self.assertEqual(calls, [])
                with patch('notification_policy.time.time', return_value=clock + ANALYSIS_WAIT):
                    self.drain(restarted, calls)
                self.assertEqual(len(calls), 1)

    def test_completed_analysis_releases_early_and_does_not_block_other_stories(self):
        for service in (self.kakao, self.telegram):
            calls = []
            waiting = {**self.article, 'dedup_pending': True}
            ready = {**self.article, 'url': 'https://example.com/ready', 'title': '은행 결제 서비스 장애 발생'}
            service.enqueue([waiting, ready])
            self.drain(service, calls)
            self.assertEqual(len(calls), 1)
            service.enqueue([{**waiting, 'dedup_pending': False}])
            self.drain(service, calls)
            self.assertEqual(len(calls), 2)

    def setUp(self):
        for module in ('kakao_notifications', 'telegram_notifications'):
            schedule = patch(module + '.load_schedule', return_value={'enabled': False})
            schedule.start()
            self.addCleanup(schedule.stop)
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
            service.deliver(sender=lambda token, article:
                            (calls.extend(item['url'] for item in article.get('digest_articles', [article]))
                             or ('sent', 'OK')))
        else:
            with service.connect() as db:
                db.execute('UPDATE settings SET next_send=0')
            with patch('telegram_notifications.protect', return_value='token'):
                service.deliver(request=lambda token, method, fields: (calls.append(fields) or {'message_id': 1}))

    def test_reset_survives_restart_blocks_backfill_and_shares_only_new_articles(self):
        cutoff = time.time() + 100
        for service, cls in ((self.kakao, Notifications), (self.telegram, TelegramNotifications)):
            with self.subTest(channel=cls.__name__):
                service.enqueue([self.article])
                with patch('notification_policy.time.time', return_value=cutoff):
                    count = service.reset_queue(1) if service is self.kakao else service.reset_queue()
                self.assertEqual(count, 1)
                restarted = cls(self.directory)
                # Include an old article not previously enqueued, and the exact boundary.
                restarted.enqueue([self.article,
                    {**self.article, 'url': 'https://example.com/backfill'},
                    {**self.article, 'url': 'https://example.com/boundary', 'discovered': cutoff}])
                calls = []
                self.drain(restarted, calls)
                self.assertEqual(calls, [])
                with restarted.connect() as db:
                    self.assertEqual(db.execute('SELECT state FROM deliveries').fetchone()[0], 'reset')
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 1)
                # A new URL for even the same story is eligible after a reset if never sent.
                restarted.enqueue([{**self.article, 'url': 'https://example.com/new',
                                    'discovered': cutoff + 1}])
                self.drain(restarted, calls)
                self.assertEqual(len(calls), 1)

    def test_reset_keeps_sent_and_uncertain_history_as_duplicate_anchors(self):
        cutoff = time.time() + 100
        for service, owner, value in ((self.kakao, 'recipient', 1), (self.telegram, 'chat_id', -100)):
            for state in ('sent', 'sending', 'unknown', 'failed', 'filtered', 'pending'):
                article = {**self.article, 'url': f'https://example.com/{state}'}
                with service.connect() as db:
                    db.execute(f'INSERT INTO deliveries({owner},url,article,state) VALUES(?,?,?,?)',
                               (value, article['url'], json.dumps(article), state))
            with patch('notification_policy.time.time', return_value=cutoff):
                service.reset_queue(1) if service is self.kakao else service.reset_queue()
            with service.connect() as db:
                rows = dict(db.execute('SELECT url,state FROM deliveries'))
            for state in ('sent', 'sending', 'unknown'):
                self.assertEqual(rows[f'https://example.com/{state}'], state)
            for state in ('failed', 'filtered', 'pending'):
                self.assertEqual(rows[f'https://example.com/{state}'], 'reset')
            service.enqueue([{**self.article, 'url': 'https://example.com/new', 'discovered': cutoff + 1}])
            with service.connect() as db:
                self.assertEqual(db.execute("SELECT state FROM deliveries WHERE url='https://example.com/new'").fetchone()[0], 'skipped')

    def test_reset_is_scoped_to_selected_recipient_and_preserves_subscription(self):
        self.kakao.save_user('다른 사용자', 'test-other')
        self.kakao.enqueue([self.article])
        before = self.kakao.users()
        self.kakao.reset_queue(1)
        self.assertEqual(self.kakao.users(), before)
        with self.kakao.connect() as db:
            self.assertEqual(list(db.execute('SELECT recipient,state FROM deliveries ORDER BY recipient')),
                             [(1, 'reset'), (2, 'pending')])
        with self.assertRaises(ValueError):
            self.kakao.reset_queue(999)

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

    def test_tracking_urls_are_duplicates_and_distinct_events_remain_representative_candidates(self):
        self.kakao.enqueue([self.article,
            {**self.article, 'url': self.article['url'] + '?utm_source=rss#top', 'title': '제목 변경'},
            {**self.article, 'url': 'https://example.com/new', 'title': '한국전력 개인정보 2000명 유출 발생'}])
        calls = []
        self.drain(self.kakao, calls)
        self.assertEqual(len(calls), 1)
        with self.kakao.connect() as db:
            rows = dict(db.execute('SELECT url,state FROM deliveries'))
        self.assertEqual(rows[self.article['url'] + '?utm_source=rss#top'], 'skipped')
        self.assertEqual(rows['https://example.com/new'], 'skipped')

    def test_low_importance_filtered_and_paid_broadcast_disabled(self):
        article = {**self.article, 'title': '지역 문화 행사 개최'}
        self.telegram.enqueue([article])
        calls = []
        self.drain(self.telegram, calls)
        self.assertEqual(calls, [])
        self.assertEqual(self.telegram.status()[0]['state'], 'filtered')
        self.telegram.enqueue([self.article])
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

    def test_importance_gate_both_channels_and_priority(self):
        for service in (self.kakao, self.telegram):
            low = {**self.article, 'url': 'https://example.com/low', 'title': '사이버 공격 방어 대회 개최'}
            outage = {**self.article, 'url': 'https://example.com/outage', 'title': '은행 결제 서비스 장애 발생'}
            service.enqueue([low, self.article, outage])
            calls = []
            self.drain(service, calls)
            if service is self.kakao:
                self.assertEqual(calls, [outage['url'], self.article['url']])
            else:
                self.assertIn(outage['title'], calls[0]['text'])
                self.drain(service, calls)
                self.assertEqual(len(calls), 2)
            with service.connect() as db:
                self.assertEqual(db.execute("SELECT state FROM deliveries WHERE url=?", (low['url'],)).fetchone()[0], 'filtered')

    def test_body_group_evidence_arriving_after_send_survives_restart(self):
        for service, cls in ((self.kakao, Notifications), (self.telegram, TelegramNotifications)):
            alias = {**self.article, 'url': 'https://other.com/body', 'title': '당국 후속 대응 현황'}
            service.enqueue([self.article])
            calls = []
            self.drain(service, calls)
            grouped = {**self.article, 'related_articles': [self.article, alias]}
            for _ in range(4):
                service.enqueue([grouped])
            restarted = cls(self.directory)
            restarted.enqueue([alias])
            self.drain(restarted, calls)
            self.assertEqual(len(calls), 1)
            with restarted.connect() as db:
                raw = db.execute('SELECT article FROM deliveries WHERE url=?', (self.article['url'],)).fetchone()[0]
                self.assertLess(len(raw), 4000)
                members = json.loads(raw)['related_articles']
                self.assertEqual(len(members), 2)
                self.assertTrue(all('related_articles' not in member for member in members))

    def test_legacy_low_importance_pending_is_filtered_before_send(self):
        for service, owner, value in ((self.kakao, 'recipient', 1), (self.telegram, 'chat_id', -100)):
            low = {**self.article, 'title': '지역 문화 행사 개최'}
            with service.connect() as db:
                db.execute(f'INSERT INTO deliveries({owner},url,article) VALUES(?,?,?)',
                           (value, low['url'], json.dumps(low)))
            calls = []
            self.drain(service, calls)
            self.assertEqual(calls, [])
            with service.connect() as db:
                self.assertEqual(db.execute('SELECT state FROM deliveries').fetchone()[0], 'filtered')

