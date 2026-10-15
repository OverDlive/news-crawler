import json
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from kakao_notifications import Notifications, memo_template, send_memo
from telegram_notifications import TelegramNotifications, TelegramError
from news_digest import institution, select_digest, telegram_text


def article(index, title, source='검증신문'):
    return dict(url=f'https://example.com/{index}', title=title, source=source,
                description='', discovered=time.time() + 1, published=time.time())


class InstitutionTests(unittest.TestCase):
    def test_aliases_regulator_attribution_and_ambiguous_subjects(self):
        for title in ('OK저축은행 개인정보 유출', 'ok 저축은행 고객 안내',
                      '금감원, OK저축은행 개인정보 유출 조사'):
            self.assertEqual(institution({'title': title}), 'OK저축은행')
        self.assertEqual(institution({'title': '한전 개인정보 유출'}), '한국전력')
        self.assertEqual(institution({'title': '피해 고객 안내',
                                     'keywords': ['OK저축은행 · 공격 유형']}), 'OK저축은행')
        self.assertEqual(institution({'title': 'OK저축은행·신한은행 공동 점검'}), '')
        self.assertEqual(institution({'title': '기관이 없는 소식'}), '')

    def test_unknown_and_multi_institution_stories_stay_individual(self):
        for title in ('기관이 없는 개인정보 유출', 'OK저축은행·신한은행 개인정보 유출'):
            items = [(str(i), article(i, title)) for i in range(2)]
            batch, payload = select_digest(items, max_articles=3)
            self.assertEqual(len(batch), 1)
            self.assertNotIn('digest_articles', payload)

    def test_kakao_list_request_contains_each_original_article_link(self):
        stories = [article(i, f'OK저축은행 고객 {1000+i}명 개인정보 유출') for i in range(3)]
        _, payload = select_digest([(item['url'], item) for item in stories], max_articles=3)
        with patch('kakao_notifications.urllib.request.urlopen',
                   return_value=io.BytesIO(b'{"result_code":0}')) as request:
            self.assertEqual(send_memo('test-token', payload)[0], 'sent')
        fields = parse_qs(request.call_args.args[0].data.decode())
        template = json.loads(fields['template_object'][0])
        self.assertEqual(template['object_type'], 'list')
        self.assertEqual([item['link']['web_url'] for item in template['contents']],
                         [item['url'] for item in stories])


class DigestDeliveryTests(unittest.TestCase):
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

    def test_institution_roundup_keeps_different_events_and_separate_delivery_records(self):
        other = article('other', '신한은행 고객 400명 개인정보 유출')
        low = article('promo', 'OK저축은행 보안 교육 개최')
        duplicate = {**self.stories[0], 'url': 'https://another.com/leak'}
        for service in (self.kakao, self.telegram):
            with self.subTest(channel=type(service).__name__):
                service.enqueue([*self.stories, other, low, duplicate])
                calls = []
                self.drain(service, calls)
                if service is self.telegram:
                    self.assertEqual(len(calls), 1)
                    self.drain(service, calls)
                self.assertEqual(len(calls), 2)
                if service is self.kakao:
                    payload = calls[0]
                    self.assertEqual(payload['institution'], 'OK저축은행')
                    self.assertEqual(len(payload['digest_articles']), 3)
                    template = memo_template(payload)
                    self.assertEqual(template['object_type'], 'list')
                    self.assertEqual({item['link']['web_url'] for item in template['contents']},
                                     {item['url'] for item in self.stories})
                else:
                    text = calls[0]['text']
                    self.assertIn('[OK저축은행] 주요 뉴스 3건', text)
                    for item in self.stories:
                        self.assertIn(item['title'], text)
                        self.assertIn(item['url'], text)
                    self.assertLess(text.index('장애 발생'), text.index('과징금'))
                    self.assertIs(calls[0]['allow_paid_broadcast'], False)
                    self.assertNotIn(other['url'], text)
                states = self.states(service)
                self.assertTrue(all(states[item['url']] == 'sent' for item in [*self.stories, other]))
                self.assertEqual(states[duplicate['url']], 'skipped')
                self.assertEqual(states[low['url']], 'filtered')
                with service.connect() as db:
                    raw, detail = db.execute('SELECT article,detail FROM deliveries WHERE url=?',
                                             (self.stories[0]['url'],)).fetchone()
                    self.assertNotIn('digest_articles', json.loads(raw))
                    self.assertIn('기관 묶음 3건', detail)

    def test_unknown_batch_is_not_automatically_retried_after_restart(self):
        for service, cls in ((self.kakao, Notifications), (self.telegram, TelegramNotifications)):
            service.enqueue(self.stories)
            calls = []
            self.drain(service, calls, state='unknown')
            self.assertEqual(len(calls), 1)
            self.assertEqual(set(self.states(service).values()), {'unknown'})
            restarted = cls(self.path)
            self.enable(restarted)
            restarted.enqueue([{**item, 'url': item['url'] + '?utm_source=rss'} for item in self.stories])
            self.drain(restarted, calls)
            self.assertEqual(len(calls), 1)

    def test_telegram_rate_limit_and_explicit_failure_apply_to_every_batch_member(self):
        self.telegram.enqueue(self.stories)
        with patch('telegram_notifications.protect', return_value='token'):
            def limited(*args):
                raise TelegramError(429, 120)
            self.assertEqual(self.telegram.deliver(request=limited)[0], 'pending')
        self.assertEqual(set(self.states(self.telegram).values()), {'pending'})
        self.assertGreater(self.telegram.settings()['next_send'], time.time() + 110)
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
        self.assertEqual(set(self.states(self.telegram).values()), {'sent'})

    def test_kakao_claims_all_items_atomically_and_caps_lists_at_three(self):
        stories = [article(i, f'OK저축은행 고객 {1000+i}명 개인정보 유출') for i in range(5)]
        self.kakao.enqueue(stories)
        calls = []
        def sender(token, payload):
            items = payload['digest_articles']
            states = self.states(self.kakao)
            self.assertTrue(all(states[item['url']] == 'sending' for item in items))
            calls.append(payload)
            return 'sent', '결과'
        self.kakao.deliver(sender=sender)
        self.assertEqual([len(item['digest_articles']) for item in calls], [3, 2])
        self.assertEqual(set(self.states(self.kakao).values()), {'sent'})

    def test_kakao_explicit_retry_resends_the_failed_roundup_once(self):
        self.kakao.enqueue(self.stories)
        calls = []
        self.drain(self.kakao, calls, state='failed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(self.states(self.kakao).values()), {'failed'})
        self.enable(self.kakao)
        self.drain(self.kakao, calls)
        self.assertEqual(len(calls), 1)
        self.kakao.retry(1)
        self.drain(self.kakao, calls)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[-1]['digest_articles']), 3)
        self.assertEqual(set(self.states(self.kakao).values()), {'sent'})

    def test_telegram_splits_long_messages_without_losing_links_or_marking_unsent_articles_sent(self):
        stories = [{**article(i, f'OK저축은행 고객 {1000+i}명 개인정보 유출'),
                    'url': 'https://example.com/' + 'x' * 1400 + str(i)} for i in range(4)]
        self.telegram.enqueue(stories)
        calls = []
        self.drain(self.telegram, calls)
        self.assertEqual(list(self.states(self.telegram).values()).count('sent'), 2)
        self.assertEqual(list(self.states(self.telegram).values()).count('pending'), 2)
        self.drain(self.telegram, calls)
        self.assertEqual(len(calls), 2)
        for fields in calls:
            self.assertLessEqual(len(fields['text'].encode('utf-16-le')) // 2, 4096)
        text = '\n'.join(fields['text'] for fields in calls)
        for item in stories:
            self.assertIn(item['url'], text)
        self.assertEqual(set(self.states(self.telegram).values()), {'sent'})

    def test_same_institution_news_is_not_combined_across_recipients(self):
        self.kakao.save_user('둘째', 'second-token')
        self.kakao.enqueue(self.stories)
        calls = []
        self.kakao.deliver(sender=lambda token, payload: (calls.append((token, payload)) or ('sent', 'OK')))
        self.assertEqual(len(calls), 2)
        self.assertEqual({token for token, _ in calls}, {'test-token', 'second-token'})
        self.assertTrue(all(len(payload['digest_articles']) == 3 for _, payload in calls))
