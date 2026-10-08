import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from kakao_notifications import Notifications, protect, send_memo, refresh_tokens, RefreshError


class NotificationTests(unittest.TestCase):
    def test_malformed_success_response_is_not_treated_as_success(self):
        import io
        for payload in ('{"result_code":false}', '{"result_code":"0"}', '[]', '{}'):
            with self.subTest(payload=payload):
                with patch('urllib.request.urlopen', return_value=io.BytesIO(payload.encode())):
                    self.assertEqual(send_memo('test-token', self.article)[0], 'failed')

    def test_subscription_toggle_is_persistent_and_only_pauses_selected_user(self):
        self.queue.save_user('둘째', 'second-test-token')
        user = self.queue.users()[0][0]
        self.queue.enqueue([self.article])
        with self.queue.connect() as db:
            before = db.execute('SELECT token,registered,refresh_token,client_id,client_secret,expires_at,next_refresh,auth_error FROM recipients WHERE id=?', (user,)).fetchone()
        self.queue.set_enabled(user, False)
        restarted = Notifications(Path(self.temp.name))
        self.assertEqual([row[2] for row in restarted.users()], [0, 1])
        newer = {**self.article, 'url': 'https://example.com/while-off'}
        restarted.enqueue([newer])
        calls = []
        def sender(token, article):
            calls.append((token, article['url']))
            return 'sent', '전송 성공'
        restarted.deliver(sender=sender)
        self.assertEqual(calls, [('second-test-token', self.article['url']), ('second-test-token', newer['url'])])
        restarted.set_enabled(user, True)
        restarted.deliver(sender=sender)
        self.assertEqual(calls[-1], ('sample-test-token', self.article['url']))
        self.assertEqual(len(calls), 3)
        with restarted.connect() as db:
            after = db.execute('SELECT token,registered,refresh_token,client_id,client_secret,expires_at,next_refresh,auth_error FROM recipients WHERE id=?', (user,)).fetchone()
        self.assertEqual(before, after)

    def test_immediate_test_sends_only_selected_user_and_preserves_news_queue(self):
        user = self.queue.users()[0][0]
        self.queue.save_user('둘째', 'second-test-token')
        self.queue.enqueue([self.article])
        calls = []
        def sender(token, article):
            calls.append((token, article))
            return 'sent', '전송 성공'
        for _ in range(2):
            self.assertEqual(self.queue.send_test(user, self.article['url'], sender=sender)[0], 'sent')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], 'sample-test-token')
        self.assertIn('[테스트]', calls[0][1]['title'])
        self.assertEqual(calls[0][1]['url'], self.article['url'])
        self.assertEqual([row[1] for row in self.queue.status()].count('pending'), 2)
        self.assertEqual([row[1] for row in self.queue.status()].count('sent'), 2)

    def test_immediate_test_failure_is_recorded_and_not_automatically_repeated(self):
        user = self.queue.users()[0][0]
        self.assertEqual(self.queue.send_test(user, sender=lambda *args: ('unknown', '결과 불명'))[0], 'unknown')
        self.assertFalse(self.queue.users()[0][2])
        self.assertIn('테스트 알림', self.queue.status()[0][2])
        self.queue.deliver(sender=lambda *args: self.fail('must not repeat test'))

    def test_immediate_test_respects_disabled_missing_and_stopped_users(self):
        user = self.queue.users()[0][0]
        self.queue.save_user('홍길동', '', False)
        sender = lambda *args: self.fail('must not send')
        self.assertEqual(self.queue.send_test(user, sender=sender)[0], 'failed')
        self.assertEqual(self.queue.send_test(999, sender=sender)[0], 'failed')
        self.assertEqual(self.queue.send_test(user, stopped=lambda: True, sender=sender)[0], 'failed')
        self.assertEqual(self.queue.status(), [])

    def test_immediate_test_uses_refreshed_token(self):
        user = self.configure_refresh()
        def refresh(**kwargs):
            self.assertEqual(kwargs['user'], user)
            Notifications.refresh_due(self.queue, user=user, refresher=lambda *args:
                {'access_token': 'renewed-test-access', 'expires_in': 7200})
        calls = []
        with patch.object(self.queue, 'refresh_due', side_effect=refresh):
            self.queue.send_test(user, sender=lambda token, article: (calls.append(token) or 'sent', 'OK'))
        self.assertEqual(calls, ['renewed-test-access'])

    def test_idle_delivery_cycle_refreshes_without_any_articles(self):
        user = self.configure_refresh()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"access_token":"idle-renewed-access","expires_in":21600}'
        with patch('urllib.request.urlopen', return_value=Response()) as request:
            self.queue.deliver(sender=lambda *args: self.fail('there are no articles to send'))
            self.assertEqual(request.call_args.args[0].full_url, 'https://kauth.kakao.com/oauth/token')
        self.assertGreater(self.queue.refresh_status(user)[1], time.time() + 21000)

    def configure_refresh(self):
        user = self.queue.users()[0][0]
        self.queue.save_refresh(user, 'test-app-key', 'test-refresh-token', 'test-client-secret')
        return user

    def test_refresh_before_expiry_uses_actual_duration_and_survives_restart(self):
        user = self.configure_refresh()
        calls = []
        def refresher(*args):
            calls.append(args)
            return {'access_token': 'renewed-access', 'expires_in': 7200}
        self.queue.refresh_due(refresher=refresher, now=1000)
        self.assertEqual(calls, [('test-app-key', 'test-refresh-token', 'test-client-secret')])
        restarted = Notifications(Path(self.temp.name))
        restarted.refresh_due(refresher=refresher, now=7599)
        self.assertEqual(len(calls), 1)
        restarted.refresh_due(refresher=refresher, now=7600)
        self.assertEqual(len(calls), 2)
        self.assertEqual(restarted.refresh_status(user), (True, 14800, ''))

    def test_rotated_refresh_token_is_saved_and_omitted_token_is_preserved(self):
        self.configure_refresh()
        self.queue.refresh_due(now=1000, refresher=lambda *args:
            {'access_token': 'new-access', 'refresh_token': 'rotated-token', 'expires_in': 7200})
        calls = []
        def refresher(*args):
            calls.append(args)
            return {'access_token': 'another-access', 'expires_in': 7200}
        self.queue.refresh_due(now=7600, refresher=refresher)
        self.queue.refresh_due(now=14200, refresher=refresher)
        self.assertEqual([args[1] for args in calls], ['rotated-token', 'rotated-token'])
        for secret in (b'rotated-token', b'test-app-key', b'test-client-secret', b'another-access'):
            self.assertNotIn(secret, self.queue.path.read_bytes())

    def test_refresh_backoff_keeps_articles_pending_and_recovers(self):
        user = self.configure_refresh()
        self.queue.enqueue([self.article])
        def failed(*args):
            raise RefreshError('갱신 연결 실패')
        self.queue.refresh_due(now=1000, refresher=failed)
        with patch.object(self.queue, 'refresh_due'):
            self.queue.deliver(sender=lambda *args: self.fail('must wait during refresh failure'))
        self.assertEqual(self.queue.status()[0][1], 'pending')
        self.queue.refresh_due(now=1299, refresher=lambda *args: self.fail('backoff must be respected'))
        self.queue.refresh_due(now=1300, refresher=lambda *args:
            {'access_token': 'recovered-access', 'expires_in': 7200})
        self.assertEqual(self.queue.refresh_status(user)[2], '')

    def test_permanent_refresh_failure_stops_until_configuration_is_saved(self):
        user = self.configure_refresh()
        def failed(*args):
            raise RefreshError('다시 로그인 필요', True)
        self.queue.refresh_due(now=1000, refresher=failed)
        self.queue.refresh_due(now=100000, refresher=lambda *args: self.fail('permanent failure must stop'))
        self.queue.save_refresh(user, refresh_token='replacement-refresh')
        self.queue.refresh_due(now=100000, refresher=lambda *args:
            {'access_token': 'recovered-access', 'expires_in': 7200})
        self.assertEqual(self.queue.refresh_status(user)[2], '')

    def test_concurrent_manual_token_change_is_not_overwritten(self):
        self.configure_refresh()
        def refresher(*args):
            self.queue.save_user('홍길동', 'manually-replaced-access')
            return {'access_token': 'stale-refresh-result', 'expires_in': 7200}
        self.queue.refresh_due(now=1000, refresher=refresher)
        with self.queue.connect() as db:
            stored = db.execute('SELECT token FROM recipients').fetchone()[0]
        self.assertEqual(protect(stored, True), 'manually-replaced-access')

    def test_refresh_disabled_and_credentials_can_be_removed(self):
        user = self.configure_refresh()
        self.queue.save_user('홍길동', '', False)
        self.queue.refresh_due(refresher=lambda *args: self.fail('disabled recipient must not refresh'))
        self.queue.save_refresh(user, enabled=False)
        self.assertEqual(self.queue.refresh_status(user), (False, 0, ''))

    def test_refresh_request_and_redacted_auth_failure(self):
        import io
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"access_token":"new-access","expires_in":21600}'
        with patch('urllib.request.urlopen', return_value=Response()) as call:
            self.assertEqual(refresh_tokens('app-key', 'refresh-secret', 'client-secret')['expires_in'], 21600)
            from urllib.parse import parse_qs
            fields = parse_qs(call.call_args.args[0].data.decode())
            self.assertEqual(fields, {'grant_type': ['refresh_token'], 'client_id': ['app-key'],
                                     'refresh_token': ['refresh-secret'], 'client_secret': ['client-secret']})
        error = urllib.error.HTTPError('https://kauth.kakao.com', 400, 'secret', {},
                                      io.BytesIO(b'{"error_description":"refresh-secret","error_code":"KOE322"}'))
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(RefreshError) as caught:
                refresh_tokens('app-key', 'refresh-secret')
            self.assertTrue(caught.exception.permanent)
            self.assertNotIn('refresh-secret', str(caught.exception))

    def test_legacy_recipient_database_migrates_without_losing_users(self):
        import sqlite3
        old_path = Path(self.temp.name) / 'old'
        old_path.mkdir()
        db = sqlite3.connect(old_path / 'kakao.sqlite3')
        db.execute('CREATE TABLE recipients(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,token TEXT NOT NULL,enabled INTEGER NOT NULL,registered REAL NOT NULL)')
        db.execute('INSERT INTO recipients VALUES(1,?,?,1,0)', ('기존 사용자', protect('old-token')))
        db.commit(); db.close()
        migrated = Notifications(old_path)
        self.assertEqual(migrated.users(), [(1, '기존 사용자', 1)])
        self.assertEqual(migrated.refresh_status(1), (False, 0, ''))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = Notifications(Path(self.temp.name))
        self.queue.save_user('홍길동', 'sample-test-token')
        self.article = dict(url='https://example.com/news', title='새 기사', source='신문',
                            description='설명', discovered=time.time() + 1)

    def test_dpapi_roundtrip_and_no_plaintext_storage(self):
        self.assertEqual(protect(protect('test-secret'), decrypt=True), 'test-secret')
        self.assertNotIn(b'sample-test-token', self.queue.path.read_bytes())

    def test_new_only_and_duplicate_delivery_across_restart(self):
        old = {**self.article, 'url': 'https://example.com/old', 'discovered': 0}
        self.queue.enqueue([old, self.article, self.article])
        calls = []
        def sender(token, article):
            calls.append((token, article['url']))
            return 'sent', '전송 성공'
        self.queue.deliver(sender=sender)
        restarted = Notifications(Path(self.temp.name))
        restarted.enqueue([self.article])
        restarted.deliver(sender=sender)
        self.assertEqual(calls, [('sample-test-token', self.article['url'])])

    def test_per_user_token_and_disabled_users(self):
        self.queue.save_user('둘째', 'second-test-token')
        self.queue.save_user('중지', 'disabled-test-token', False)
        self.queue.enqueue([self.article])
        tokens = []
        def sender(token, article):
            tokens.append(token)
            return 'sent', 'OK'
        self.queue.deliver(sender=sender)
        self.assertEqual(tokens, ['sample-test-token', 'second-test-token'])

    def test_failure_pauses_user_and_requires_explicit_retry(self):
        self.queue.enqueue([self.article])
        self.queue.deliver(sender=lambda *args: ('unknown', '결과 불명'))
        self.assertFalse(self.queue.users()[0][2])
        self.queue.save_user('홍길동', 'replacement-token')
        self.queue.deliver(sender=lambda *args: self.fail('must not resend automatically'))
        self.queue.retry(self.queue.users()[0][0])
        self.queue.deliver(sender=lambda *args: ('sent', 'OK'))
        self.assertEqual(self.queue.status()[0][1], 'sent')

    def test_deleting_user_removes_pending_deliveries(self):
        self.queue.enqueue([self.article])
        self.queue.remove(self.queue.users()[0][0])
        self.assertEqual(self.queue.status(), [])

    def test_api_payload_and_redacted_error(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"result_code":0}'
        with patch('urllib.request.urlopen', return_value=Response()) as request:
            self.assertEqual(send_memo('secret', self.article)[0], 'sent')
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_header('Authorization'), 'Bearer secret')
            from urllib.parse import parse_qs
            template = json.loads(parse_qs(sent.data.decode())['template_object'][0])
            self.assertEqual(template['link']['web_url'], self.article['url'])
        error = urllib.error.HTTPError('https://kapi.kakao.com', 401, 'secret', {}, None)
        with patch('urllib.request.urlopen', side_effect=error):
            state, detail = send_memo('secret', self.article)
            self.assertEqual(state, 'failed')
            self.assertNotIn('secret', detail)


if __name__ == '__main__':
    unittest.main()
