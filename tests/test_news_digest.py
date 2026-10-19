from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from kakao_notifications import Notifications
from telegram_notifications import TelegramNotifications, TelegramError
from news_digest import institution, select_representative


def article(index, title, published=100):
    return dict(url=f'https://example.com/{index}', title=title, source='검증신문',
                description='', discovered=time.time() + 1, published=published)


class InstitutionTests(unittest.TestCase):
    def test_aliases_and_ambiguous_subjects(self):
        for title in ('OK저축은행 개인정보 유출', 'ok 저축은행 고객 안내',
                      '금감원, OK저축은행 개인정보 유출 조사'):
            self.assertEqual(institution({'title': title}), 'OK저축은행')
        self.assertEqual(institution({'title': '한전 개인정보 유출'}), '한국전력')
        self.assertEqual(institution({'title': '피해 고객 안내',
                                     'keywords': ['OK저축은행 · 공격 유형']}), 'OK저축은행')
        self.assertEqual(institution({'title': 'OK저축은행·신한은행 공동 점검'}), '')

    def test_unknown_and_multi_institution_stories_stay_individual(self):
        for title in ('기관이 없는 개인정보 유출', 'OK저축은행·신한은행 개인정보 유출'):
            batch, payload = select_representative([(str(i), article(i, title)) for i in range(2)])
            self.assertEqual(len(batch), 1)
            self.assertNotIn('representative_count', payload)

    def test_highest_importance_wins_and_latest_publication_breaks_ties(self):
        low = article('leak', 'OK저축은행 개인정보 유출', published=300)
        older = article('old', 'OK저축은행 전산 서비스 장애 발생', published=100)
        latest = article('new', 'OK저축은행 전산 서비스 장애 발생', published=200)
        other = article('other', '신한은행 개인정보 유출')
        batch, payload = select_representative([(a['url'], a) for a in [low, older, other, latest]])
        self.assertEqual(payload['url'], latest['url'])
        self.assertEqual(len(batch), 3)
        self.assertNotIn(other['url'], [key for key, _ in batch])


class RepresentativeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.kakao = Notifications(self.path)
        self.kakao.save_user('검증', 'test-token')
        self.telegram = TelegramNotifications(self.path)
        self.enable(self.telegram)
        self.stories = [article('leak', 'OK저축은행 고객 250명 개인정보 유출'),
                        article('sanction', 'OK저축은행 과징금 3억원 부과'),
                        article('outage', 'OK저축은행 전산 서비스 장애 발생')]

    def enable(self, service):
        if isinstance(service, TelegramNotifications):
            with service.connect() as db:
                db.execute("UPDATE settings SET enabled=1,chat_id=-100,registered=0,token='token',next_send=0")
        else:
            service.set_enabled(1, True)

    def drain(self, service, calls, state='sent'):
        if isinstance(service, Notifications):
            service.deliver(sender=lambda token, payload: (calls.append(payload) or (state, '결과')))
        else:
            with service.connect() as db:
                db.execute('UPDATE settings SET next_send=0')
            with patch('telegram_notifications.protect', return_value='token'):
                service.deliver(request=lambda token, method, fields:
                                (calls.append(fields) or ({'message_id': 1} if state == 'sent' else {})))

    def states(self, service):
        with service.connect() as db:
            return dict(db.execute('SELECT url,state FROM deliveries'))

    def test_only_highest_is_sent_and_other_institutions_remain_independent(self):
        other = article('other', '신한은행 고객 400명 개인정보 유출')
        low = article('promo', 'OK저축은행 보안 교육 개최')
        for service in (self.kakao, self.telegram):
            with self.subTest(channel=type(service).__name__):
                service.enqueue([*self.stories, other, low])
                calls = []
                self.drain(service, calls)
                if service is self.telegram:
                    self.drain(service, calls)
                    self.assertEqual(calls[0]['link_preview_options'], {
                        'is_disabled': False, 'url': self.stories[2]['url'], 'prefer_large_media': True})
                    self.assertIn(self.stories[2]['title'], calls[0]['text'])
                    self.assertNotIn(self.stories[0]['url'], calls[0]['text'])
                else:
                    self.assertEqual(calls[0]['url'], self.stories[2]['url'])
                    self.assertNotIn('digest_articles', calls[0])
                self.assertEqual(len(calls), 2)
                states = self.states(service)
                self.assertEqual(states[self.stories[2]['url']], 'sent')
                self.assertTrue(all(states[a['url']] == 'skipped' for a in self.stories[:2]))
                self.assertEqual(states[other['url']], 'sent')
                self.assertEqual(states[low['url']], 'filtered')

    def test_success_suppression_survives_restart_and_future_news_can_send(self):
        for service, cls in ((self.kakao, Notifications), (self.telegram, TelegramNotifications)):
            service.enqueue(self.stories)
            calls = []
            self.drain(service, calls)
            restarted = cls(self.path)
            self.enable(restarted)
            restarted.enqueue(self.stories)
            restarted.enqueue([{**a, 'url': a['url'] + '?utm_source=rss'} for a in self.stories])
            self.drain(restarted, calls)
            self.assertEqual(len(calls), 1)
            new = article('new', 'OK저축은행 고객 9000명 개인정보 유출', published=200)
            restarted.enqueue([new])
            self.drain(restarted, calls)
            self.assertEqual(len(calls), 2)

    def test_unknown_is_not_automatically_retried_after_restart(self):
        for service, cls in ((self.kakao, Notifications), (self.telegram, TelegramNotifications)):
            service.enqueue(self.stories)
            calls = []
            self.drain(service, calls, state='unknown')
            self.assertEqual(set(self.states(service).values()), {'unknown'})
            restarted = cls(self.path)
            self.enable(restarted)
            restarted.enqueue(self.stories)
            self.drain(restarted, calls)
            self.assertEqual(len(calls), 1)

    def test_rate_limit_preserves_candidates_and_explicit_retry_selects_one(self):
        self.telegram.enqueue(self.stories)
        with patch('telegram_notifications.protect', return_value='token'):
            def limited(*args):
                raise TelegramError(429, 120)
            self.assertEqual(self.telegram.deliver(request=limited)[0], 'pending')
        self.assertEqual(set(self.states(self.telegram).values()), {'pending'})
        self.telegram.deliver(request=lambda *args: self.fail('must respect retry delay'))
        self.enable(self.telegram)
        def failed(*args):
            raise TelegramError(403)
        with patch('telegram_notifications.protect', return_value='token'):
            self.assertEqual(self.telegram.deliver(request=failed)[0], 'failed')
        self.assertEqual(set(self.states(self.telegram).values()), {'failed'})
        self.telegram.retry_failed()
        self.enable(self.telegram)
        calls = []
        self.drain(self.telegram, calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.states(self.telegram).values()).count('sent'), 1)

    def test_kakao_claims_all_candidates_before_sending_and_has_no_three_item_cap(self):
        stories = [article(i, f'OK저축은행 고객 {1000+i}명 개인정보 유출') for i in range(5)]
        self.kakao.enqueue(stories)
        calls = []
        def sender(token, payload):
            self.assertEqual(set(self.states(self.kakao).values()), {'sending'})
            calls.append(payload)
            return 'sent', '결과'
        self.kakao.deliver(sender=sender)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.states(self.kakao).values()).count('skipped'), 4)

    def test_kakao_failure_preserves_explicit_retry_and_recipient_isolation(self):
        self.kakao.save_user('둘째', 'second-token')
        self.kakao.enqueue(self.stories)
        calls = []
        self.kakao.deliver(sender=lambda token, payload: (calls.append((token, payload)) or ('failed', '실패')))
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(self.states(self.kakao).values()), {'failed'})
        self.kakao.retry(1)
        self.kakao.set_enabled(1, True)
        self.drain(self.kakao, calls)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1]['url'], self.stories[2]['url'])
