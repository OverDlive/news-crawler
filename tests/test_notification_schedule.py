"""Clock-controlled coverage for shared scheduling, durability and settings changes."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kakao_notifications import Notifications
from telegram_notifications import TelegramNotifications
from news_core import Store
from notification_schedule import (KST, default_schedule, hold_until, load_schedule,
                                   normalize_schedule, validate_schedule)


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=KST).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_default_boundaries_use_korean_time(self):
        schedule = default_schedule()
        release = at('2026-10-23 09:00:00')
        for clock, expected in [('2026-10-22 23:59:59', 0),
                                ('2026-10-23 00:00:00', release),
                                ('2026-10-23 08:59:59', release),
                                ('2026-10-23 09:00:00', 0)]:
            with self.subTest(clock=clock):
                self.assertEqual(hold_until(schedule, at(clock)), expected)

    def test_custom_minutes_daytime_disabled_and_month_rollover(self):
        schedule = {'enabled': True, 'start': '22:15', 'end': '08:30'}
        release = at('2026-11-01 08:30')
        for clock, expected in [('2026-10-31 22:14:59', 0),
                                ('2026-10-31 22:15:00', release),
                                ('2026-11-01 00:01', release),
                                ('2026-11-01 08:30', 0)]:
            self.assertEqual(hold_until(schedule, at(clock)), expected)
        day = {'enabled': True, 'start': '12:10', 'end': '13:05'}
        self.assertEqual(hold_until(day, at('2026-10-23 12:10')), at('2026-10-23 13:05'))
        self.assertEqual(hold_until({**day, 'enabled': False}, at('2026-10-23 12:10')), 0)

    def test_invalid_schedule_falls_back_without_losing_keywords(self):
        for invalid in (None, [], {'enabled': 'yes'},
                        {'enabled': True, 'start': '24:00', 'end': '09:00'},
                        {'enabled': True, 'start': '00:60', 'end': '09:00'},
                        {'enabled': True, 'start': '09:00', 'end': '09:00'}):
            self.assertEqual(normalize_schedule(invalid), default_schedule())
            with self.assertRaises(ValueError):
                validate_schedule(invalid)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            self.addCleanup(store.close)
            for extra in ({}, {'sharing_schedule': {'enabled': 'broken'}}):
                store.config_path.write_text(json.dumps({
                    'keywords': [{'name': '한전', 'enabled': True}], **extra}), encoding='utf-8')
                config = store.load_config()
                self.assertEqual(config['sharing_schedule'], default_schedule())
                self.assertEqual(config['keywords'][0]['name'], '한전')
            store.close()


class ScheduledDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.addCleanup(self.store.close)
        self.clock = patch('time.time', return_value=at('2026-10-23 01:30'))
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        for target in ('kakao_notifications.protect', 'telegram_notifications.protect'):
            protection = patch(target, side_effect=lambda value, *args, **kwargs: value)
            protection.start()
            self.addCleanup(protection.stop)
        preview = patch('telegram_notifications.telegram_preview_url', side_effect=lambda article: article['url'])
        preview.start()
        self.addCleanup(preview.stop)
        self.services = (Notifications(self.directory), TelegramNotifications(self.directory))
        self.services[0].save_user('검증', 'test-token')
        with self.services[1].connect() as db:
            db.execute("UPDATE settings SET enabled=1,chat_id=-100,registered=0,token='token'")
        self.articles = [dict(url='https://example.com/leak', title='한국전력 개인정보 1000명 유출 발생',
                             description='개인정보 유출 사고 확인', source='신문',
                             published=at('2026-10-23 01:00'), discovered=at('2026-10-23 01:31')),
                         dict(url='https://example.com/outage', title='신한은행 결제 서비스 장애 발생',
                             source='다른신문', published=at('2026-10-23 02:00'),
                             discovered=at('2026-10-23 02:01'))]
        self.calls = []

    def deliver(self, service):
        if isinstance(service, Notifications):
            service.deliver(sender=lambda token, article: (self.calls.append(article) or ('sent', 'OK')))
        else:
            service.deliver(request=lambda token, method, fields: (self.calls.append(fields) or {'message_id': 1}))

    def states(self, service):
        with service.connect() as db:
            return [row[0] for row in db.execute('SELECT state FROM deliveries ORDER BY rowid')]

    def save_schedule(self, **values):
        config = self.store.load_config()
        config['sharing_schedule'] = {**default_schedule(), **values}
        self.store.save_config(config)

    def test_overnight_queue_survives_restart_and_releases_all_at_nine_once(self):
        for service in self.services:
            service.enqueue(self.articles)
            self.deliver(service)
            self.assertEqual(self.states(service), ['pending', 'pending'])
        self.assertEqual(self.calls, [])
        restarted = (Notifications(self.directory), TelegramNotifications(self.directory))
        self.now.return_value = at('2026-10-23 08:59:59')
        for service in restarted:
            self.deliver(service)
        self.assertEqual(self.calls, [])
        self.now.return_value = at('2026-10-23 09:00')
        for service in restarted:
            self.deliver(service)
        self.assertEqual(len(self.calls), 3)  # Kakao drains; Telegram respects its rate limit.
        self.now.return_value += 4
        for service in restarted:
            self.deliver(service)
            self.assertEqual(self.states(service), ['sent', 'sent'])
            service.enqueue(self.articles)
            self.deliver(service)
        self.assertEqual(len(self.calls), 4)

    def test_changes_apply_to_existing_services_and_disabling_releases_backlog(self):
        self.save_schedule(start='22:15', end='10:30')
        self.now.return_value = at('2026-10-23 09:45')
        for service in self.services:
            service.enqueue(self.articles[:1])
            self.deliver(service)
        self.assertEqual(self.calls, [])
        self.save_schedule(enabled=False)
        self.assertFalse(load_schedule(self.directory)['enabled'])
        for service in self.services:
            self.deliver(service)
            self.assertEqual(self.states(service), ['sent'])
        self.assertEqual(len(self.calls), 2)

    def test_test_messages_bypass_hold_and_leave_news_pending(self):
        kakao, telegram = self.services
        for service in self.services:
            service.enqueue(self.articles[:1])
        kakao.send_test(1, sender=lambda token, article: (self.calls.append(article) or ('sent', 'OK')))
        telegram.deliver(test=True, request=lambda token, method, fields:
                         (self.calls.append(fields) or {'message_id': 1}))
        self.assertEqual(len(self.calls), 2)
        for service in self.services:
            self.assertEqual(self.states(service), ['pending', 'sent'])

    def test_new_hold_stops_queue_between_claims(self):
        self.now.return_value = at('2026-10-23 23:59:59')
        kakao = self.services[0]
        kakao.enqueue(self.articles)

        def sender(token, article):
            self.calls.append(article)
            self.now.return_value = at('2026-10-24 00:00')
            return 'sent', 'OK'

        kakao.deliver(sender=sender)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sorted(self.states(kakao)), ['pending', 'sent'])
