import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from telegram_notifications import TelegramNotifications, TelegramError, api, article_text


TOKEN = '123456:' + 'a' * 30


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.queue = TelegramNotifications(self.directory)
        self.calls = []
        self.updates = [{'update_id': 1, 'message': {'chat': {'id': -100, 'type': 'supergroup', 'title': '뉴스방'}}},
                        {'update_id': 2, 'message': {'chat': {'id': 12, 'type': 'private'}}}]
        self.queue.discover(TOKEN, request=self.request)
        self.queue.activate(-100, request=self.request)
        self.article = {'title': '기사 <제목>', 'source': '언론사', 'description': '설명',
                        'url': 'https://example.com/news', 'discovered': time.time() + 1}

    def request(self, token, method, fields=None):
        self.calls.append((method, fields))
        return {'getMe': {'id': 123456, 'is_bot': True, 'username': 'news_bot'},
                'getWebhookInfo': {'url': ''}, 'getUpdates': self.updates,
                'getChatMember': {'status': 'member'}, 'sendMessage': {'message_id': 10}}[method]

    def ready(self):
        with self.queue.connect() as db:
            db.execute('UPDATE settings SET next_send=0')

    def test_token_encrypted_and_only_group_discovered(self):
        self.assertEqual([c['id'] for c in self.queue.chats()], [-100])
        self.assertNotIn('token', self.queue.settings())
        with self.queue.connect() as db:
            self.assertNotIn(TOKEN, db.execute('SELECT token FROM settings').fetchone()[0])

    def test_discovery_offset_and_rooms_persist(self):
        self.updates = []
        restarted = TelegramNotifications(self.directory)
        restarted.discover(request=self.request)
        self.assertEqual(self.calls[-1][1]['offset'], 3)
        self.assertEqual(len(restarted.chats()), 1)

    def test_old_articles_and_duplicates_not_sent(self):
        self.queue.enqueue([{**self.article, 'url': 'old', 'discovered': 0}, self.article, self.article])
        self.assertEqual(len(self.queue.status()), 1)
        self.queue.deliver(request=self.request)
        restarted = TelegramNotifications(self.directory)
        restarted.enqueue([self.article])
        self.ready()
        restarted.deliver(request=self.request)
        self.assertEqual(sum(c[0] == 'sendMessage' for c in self.calls), 1)

    def test_failure_pauses_and_manual_retry_resumes(self):
        self.queue.enqueue([self.article])
        def fail(*args):
            raise TelegramError(403)
        self.assertEqual(self.queue.deliver(request=fail)[0], 'failed')
        self.assertFalse(self.queue.settings()['enabled'])
        self.queue.retry_failed()
        self.queue.activate(-100, request=self.request)
        self.assertEqual(self.queue.deliver(request=self.request)[0], 'sent')

    def test_ambiguous_response_never_retried(self):
        self.queue.enqueue([self.article])
        self.assertEqual(self.queue.deliver(request=lambda *args: {})[0], 'unknown')
        self.queue.retry_failed()
        self.queue.activate(-100, request=self.request)
        self.queue.deliver(request=lambda *args: self.fail('must not resend'))
        self.assertEqual(self.queue.status()[0]['state'], 'unknown')

    def test_rate_limit_retries_after_server_delay(self):
        self.queue.enqueue([self.article])
        def limited(*args):
            raise TelegramError(429, 120)
        self.assertEqual(self.queue.deliver(request=limited)[0], 'pending')
        self.assertTrue(self.queue.settings()['enabled'])
        self.assertGreater(self.queue.settings()['next_send'], time.time() + 110)
        self.queue.deliver(request=lambda *args: self.fail('too early'))

    def test_channel_requires_post_permission(self):
        self.updates = [{'update_id': 3, 'channel_post': {'chat': {'id': -200, 'type': 'channel', 'title': '채널'}}}]
        self.queue.discover(request=self.request)
        with self.assertRaises(ValueError):
            self.queue.activate(-200, request=self.request)
        self.assertEqual(self.queue.settings()['chat_id'], -100)

    def test_new_bot_resets_destinations_and_queue(self):
        self.queue.enqueue([self.article])
        def other(token, method, fields=None):
            if method == 'getMe':
                return {'id': 999, 'is_bot': True, 'username': 'other'}
            if method == 'getUpdates':
                return []
            return self.request(token, method, fields)
        self.queue.discover('999:' + 'b' * 30, request=other)
        self.assertEqual(self.queue.chats(), [])
        self.assertEqual(self.queue.status(), [])
        self.assertFalse(self.queue.settings()['enabled'])

    def test_pause_and_stopped_worker_do_not_send(self):
        self.queue.enqueue([self.article])
        self.queue.deliver(stopped=lambda: True, request=lambda *args: self.fail('stopped'))
        self.queue.pause()
        self.queue.deliver(request=lambda *args: self.fail('paused'))
        self.queue.enqueue([{**self.article, 'url': 'off'}])
        self.assertEqual(len(self.queue.status()), 1)

    def test_stale_sending_recovers_as_unknown(self):
        self.queue.enqueue([self.article])
        with self.queue.connect() as db:
            db.execute("UPDATE deliveries SET state='sending',claimed=?", (time.time() - 100,))
        TelegramNotifications(self.directory).deliver(request=lambda *args: self.fail('unsafe resend'))
        self.assertEqual(self.queue.status()[0]['state'], 'unknown')
        self.assertFalse(self.queue.settings()['enabled'])

    def test_webhook_is_not_removed(self):
        calls = []
        def webhook(token, method, fields=None):
            calls.append(method)
            return {'url': 'https://example.com/webhook'} if method == 'getWebhookInfo' else self.request(token, method, fields)
        with self.assertRaises(ValueError):
            self.queue.discover(request=webhook)
        self.assertNotIn('deleteWebhook', calls)

    def test_api_redacts_url_and_rejects_malformed_success(self):
        for body in (b'{}', b'[]', b'{"ok":1}', b'not-json'):
            with patch('urllib.request.urlopen', return_value=io.BytesIO(body)):
                with self.assertRaises(TelegramError) as error:
                    api(TOKEN, 'sendMessage')
                self.assertNotIn(TOKEN, str(error.exception))
        remote = urllib.error.HTTPError('https://api.telegram.org/bot' + TOKEN, 401, TOKEN, {}, None)
        with patch('urllib.request.urlopen', side_effect=remote):
            with self.assertRaises(TelegramError) as error:
                api(TOKEN, 'getMe')
            self.assertNotIn(TOKEN, str(error.exception))

    def test_text_fits_utf16_and_test_does_not_consume_news(self):
        text = article_text({**self.article, 'title': '😀' * 10000, 'description': '😀' * 10000})
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)
        self.queue.enqueue([self.article])
        self.queue.deliver(test=True, request=self.request)
        self.assertEqual(sorted(r['state'] for r in self.queue.status()), ['pending', 'sent'])


if __name__ == '__main__':
    unittest.main()
