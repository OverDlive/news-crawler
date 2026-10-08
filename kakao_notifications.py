"""Persistent, per-user Kakao memo delivery; tokens protected by Windows DPAPI."""
import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def protect(value, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value.encode('utf-8')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    function = api.CryptUnprotectData if decrypt else api.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows 토큰 암호화/복호화 실패')
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        kernel = ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(target.data)


def send_memo(token, article):
    url = article['url']
    text = (article['title'] + '\n' + article.get('source', '') + '\n' +
            article.get('description', ''))[:200]
    template = {'object_type': 'text', 'text': text,
                'link': {'web_url': url, 'mobile_web_url': url}, 'button_title': '기사 보기'}
    payload = urllib.parse.urlencode({'template_object': json.dumps(template, ensure_ascii=False)}).encode()
    request = urllib.request.Request('https://kapi.kakao.com/v2/api/talk/memo/default/send',
        data=payload, headers={'Authorization': 'Bearer ' + token,
                             'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.load(response)
        if not isinstance(result, dict) or type(result.get('result_code')) is not int or result['result_code'] != 0:
            return 'failed', '카카오 응답 오류: 전송 결과를 확인하세요'
        return 'sent', '카카오 API 전송 성공 (result_code=0) · 휴대폰 수신 여부는 별도 확인 필요'
    except urllib.error.HTTPError as error:
        # Never store or display remote bodies, which can contain credentials.
        if error.code == 401:
            return 'failed', '토큰 만료/인증 실패: 토큰을 교체하세요'
        if error.code == 403:
            return 'failed', '권한 오류: talk_message 동의를 확인하세요'
        return 'failed', f'카카오 HTTP {error.code}: 권한·웹 도메인·쿼터를 확인하세요'
    except (OSError, ValueError):
        # A timeout may occur after delivery. Do not automatically resend.
        return 'unknown', '전송 결과 불명: 카카오톡 확인 후 수동 재시도하세요'


class RefreshError(Exception):
    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def refresh_tokens(client_id, refresh_token, client_secret=''):
    fields = {'grant_type': 'refresh_token', 'client_id': client_id, 'refresh_token': refresh_token}
    if client_secret:
        fields['client_secret'] = client_secret
    request = urllib.request.Request('https://kauth.kakao.com/oauth/token',
        data=urllib.parse.urlencode(fields).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8'})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(8192))
            limited = isinstance(body, dict) and body.get('error_code') == 'KOE237'
        except (ValueError, TypeError, OSError):
            limited = False
        if limited or error.code == 429:
            raise RefreshError('갱신 요청 제한: 5분 후 다시 시도합니다') from None
        if error.code in (400, 401, 403):
            raise RefreshError('갱신 인증 실패: 리프레시 토큰·REST API 키·시크릿을 다시 등록하세요', True) from None
        raise RefreshError('갱신 서버 오류: 5분 후 다시 시도합니다') from None
    except (OSError, ValueError):
        raise RefreshError('갱신 연결 실패: 5분 후 다시 시도합니다') from None
    if not isinstance(result, dict) or not isinstance(result.get('access_token'), str) or not result['access_token']:
        raise RefreshError('갱신 응답 오류: 5분 후 다시 시도합니다')
    if type(result.get('expires_in')) is not int or result['expires_in'] <= 0:
        raise RefreshError('갱신 응답에 유효한 만료 시간이 없습니다')
    if 'refresh_token' in result and (not isinstance(result['refresh_token'], str) or not result['refresh_token']):
        raise RefreshError('갱신 응답에 유효한 리프레시 토큰이 없습니다')
    return result


class Notifications:
    def __init__(self, directory):
        self.path = directory / 'kakao.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS recipients (
                    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                    token TEXT NOT NULL, enabled INTEGER NOT NULL, registered REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    recipient INTEGER NOT NULL REFERENCES recipients(id) ON DELETE CASCADE,
                    url TEXT NOT NULL, article TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(recipient, url));
            ''')
            db.execute("UPDATE deliveries SET state='unknown', detail='프로그램 종료로 결과 불명: 카카오톡 확인 필요' WHERE state='sending'")
            columns = {row[1] for row in db.execute('PRAGMA table_info(recipients)')}
            for column, declaration in [('refresh_token', "TEXT NOT NULL DEFAULT ''"),
                    ('client_id', "TEXT NOT NULL DEFAULT ''"), ('client_secret', "TEXT NOT NULL DEFAULT ''"),
                    ('expires_at', 'REAL NOT NULL DEFAULT 0'), ('next_refresh', 'REAL NOT NULL DEFAULT 0'),
                    ('auth_error', "TEXT NOT NULL DEFAULT ''")]:
                if column not in columns:
                    db.execute(f'ALTER TABLE recipients ADD COLUMN {column} {declaration}')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def users(self):
        with self.connect() as db:
            return db.execute('SELECT id, name, enabled FROM recipients ORDER BY id').fetchall()

    def set_enabled(self, user, enabled):
        """Change only this recipient's subscription, preserving credentials and history."""
        with self.connect() as db:
            result = db.execute('UPDATE recipients SET enabled=? WHERE id=?', (bool(enabled), user))
            if not result.rowcount:
                raise ValueError('사용자를 찾을 수 없습니다')

    def save_user(self, name, token, enabled=True):
        name, token = name.strip(), token.strip()
        if not name or len(name) > 80:
            raise ValueError('사용자 이름을 1~80자로 입력하세요')
        if token and (len(token) > 4096 or any(c.isspace() for c in token)):
            raise ValueError('공백 없이 액세스 토큰만 입력하세요')
        encrypted = protect(token) if token else None
        with self.connect() as db:
            found = db.execute('SELECT id FROM recipients WHERE name=?', (name,)).fetchone()
            if found:
                if encrypted:
                    db.execute("UPDATE recipients SET token=?, enabled=?, expires_at=0, next_refresh=0, auth_error='' WHERE id=?", (encrypted, enabled, found[0]))
                else:
                    db.execute('UPDATE recipients SET enabled=? WHERE id=?', (enabled, found[0]))
            elif encrypted:
                db.execute('INSERT INTO recipients(name,token,enabled,registered) VALUES(?,?,?,?)',
                           (name, encrypted, enabled, time.time()))
            else:
                raise ValueError('새 사용자는 액세스 토큰이 필요합니다')

    def refresh_status(self, user):
        with self.connect() as db:
            row = db.execute('SELECT refresh_token,expires_at,auth_error FROM recipients WHERE id=?', (user,)).fetchone()
        if not row:
            return False, 0, ''
        return bool(row[0]), row[1], row[2]

    def save_refresh(self, user, client_id='', refresh_token='', client_secret='', enabled=True):
        values = [value.strip() for value in (client_id, refresh_token, client_secret)]
        if any(len(value) > 4096 or any(char.isspace() for char in value) for value in values):
            raise ValueError('키와 토큰은 공백 없이 입력하세요')
        with self.connect() as db:
            old = db.execute('SELECT client_id,refresh_token,client_secret FROM recipients WHERE id=?', (user,)).fetchone()
            if old is None:
                raise ValueError('사용자를 먼저 등록하세요')
            if not enabled:
                encrypted = ['', '', '']
            else:
                encrypted = [protect(value) if value else previous for value, previous in zip(values, old)]
                if not encrypted[0] or not encrypted[1]:
                    raise ValueError('REST API 키와 리프레시 토큰을 입력하세요')
            db.execute("""UPDATE recipients SET client_id=?,refresh_token=?,client_secret=?,
                expires_at=0,next_refresh=0,auth_error='' WHERE id=?""", (*encrypted, user))

    def refresh_due(self, stopped=lambda: False, refresher=refresh_tokens, now=None, user=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            candidates = db.execute("""SELECT id FROM recipients WHERE enabled=1
                AND refresh_token!='' AND client_id!='' AND expires_at<=? AND next_refresh<=?""",
                (now + 600, now)).fetchall()
        if user is not None:
            candidates = [row for row in candidates if row[0] == user]
        for (user,) in candidates:
            if stopped():
                break
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("""SELECT token,refresh_token,client_id,client_secret FROM recipients
                    WHERE id=? AND enabled=1 AND refresh_token!='' AND client_id!=''
                    AND expires_at<=? AND next_refresh<=?""", (user, now + 600, now)).fetchone()
                if row is None:
                    continue
                # Short lease prevents simultaneous refreshes from multiple program instances.
                db.execute('UPDATE recipients SET next_refresh=? WHERE id=?', (now + 60, user))
            try:
                result = refresher(protect(row[2], True), protect(row[1], True), protect(row[3], True) if row[3] else '')
                access = protect(result['access_token'])
                refresh = protect(result['refresh_token']) if result.get('refresh_token') else row[1]
                expires = now + result['expires_in']
                with self.connect() as db:
                    db.execute("""UPDATE recipients SET token=?,refresh_token=?,expires_at=?,next_refresh=0,auth_error=''
                        WHERE id=? AND token=? AND refresh_token=? AND client_id=? AND client_secret=?""",
                        (access, refresh, expires, user, *row))
            except Exception as error:
                detail = str(error) if isinstance(error, RefreshError) else '갱신 정보 처리 실패: 설정을 다시 확인하세요'
                retry = now + 300 if not isinstance(error, RefreshError) or not error.permanent else 1e20
                with self.connect() as db:
                    db.execute("""UPDATE recipients SET next_refresh=?,auth_error=?
                        WHERE id=? AND token=? AND refresh_token=? AND client_id=? AND client_secret=?""",
                        (retry, detail, user, *row))

    def remove(self, user):
        with self.connect() as db:
            db.execute('DELETE FROM recipients WHERE id=?', (user,))

    def send_test(self, user, url='https://news.google.com', stopped=lambda: False, sender=send_memo):
        """Send one explicit test immediately, without draining the news queue."""
        if stopped():
            return 'failed', '프로그램 종료로 테스트를 취소했습니다'
        self.refresh_due(stopped=stopped, user=user)
        with self.connect() as db:
            row = db.execute('SELECT token,enabled,auth_error,refresh_token,expires_at FROM recipients WHERE id=?',
                             (user,)).fetchone()
            if row is None:
                return 'failed', '사용자를 먼저 등록하세요'
            if not row[1]:
                return 'failed', '새 기사 수신을 켜고 저장한 뒤 테스트하세요'
            if row[2] or (row[3] and row[4] <= time.time()):
                return 'failed', row[2] or '토큰 자동 갱신이 완료되지 않았습니다. 잠시 후 다시 시도하세요'
            if stopped():
                return 'failed', '프로그램 종료로 테스트를 취소했습니다'
            key = 'test:' + uuid.uuid4().hex
            article = dict(url=url, title='[테스트] 뉴스 모니터 카카오톡 알림', source='',
                           description='즉시 알람 보내기 테스트입니다.\n발송 시각: ' + time.strftime('%Y-%m-%d %H:%M:%S'))
            db.execute("INSERT INTO deliveries(recipient,url,article,state,detail) VALUES(?,?,?,'sending',?)",
                       (user, key, json.dumps(article, ensure_ascii=False), '테스트 알림 전송 중'))
        try:
            state, detail = sender(protect(row[0], decrypt=True), article)
        except Exception:
            state, detail = 'failed', '토큰을 읽거나 전송하지 못했습니다: 토큰을 다시 등록하세요'
        with self.connect() as db:
            db.execute('UPDATE deliveries SET state=?,detail=? WHERE recipient=? AND url=?',
                       (state, '테스트 알림 · ' + detail, user, key))
            if state != 'sent':
                db.execute('UPDATE recipients SET enabled=0 WHERE id=?', (user,))
        return state, detail

    def enqueue(self, articles):
        with self.connect() as db:
            users = db.execute('SELECT id, registered FROM recipients WHERE enabled=1').fetchall()
            for user, registered in users:
                for article in articles:
                    if article['discovered'] >= registered:
                        db.execute('INSERT OR IGNORE INTO deliveries(recipient,url,article) VALUES(?,?,?)',
                                   (user, article['url'], json.dumps(article, ensure_ascii=False)))

    def status(self):
        with self.connect() as db:
            return db.execute('''SELECT r.name, d.state, d.detail FROM deliveries d
                JOIN recipients r ON r.id=d.recipient ORDER BY d.rowid DESC LIMIT 30''').fetchall()

    def retry(self, user):
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state='pending', detail='' WHERE recipient=? AND state IN ('failed','unknown')", (user,))

    def deliver(self, stopped=lambda: False, sender=send_memo):
        self.refresh_due(stopped=stopped)
        # Each GUI instance launches only one worker; claim also guards other instances.
        for _ in range(20):
            if stopped():
                break
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('''SELECT d.recipient,d.url,d.article,r.token FROM deliveries d
                    JOIN recipients r ON r.id=d.recipient
                    WHERE d.state='pending' AND r.enabled=1 AND r.auth_error=''
                    AND (r.refresh_token='' OR r.expires_at>?) ORDER BY d.rowid LIMIT 1''', (time.time(),)).fetchone()
                if row is None:
                    break
                db.execute("UPDATE deliveries SET state='sending' WHERE recipient=? AND url=?", row[:2])
            try:
                state, detail = sender(protect(row[3], decrypt=True), json.loads(row[2]))
            except Exception:
                state, detail = 'failed', '토큰을 읽거나 전송하지 못했습니다: 토큰을 다시 등록하세요'
            with self.connect() as db:
                db.execute('UPDATE deliveries SET state=?,detail=? WHERE recipient=? AND url=?',
                           (state, detail, row[0], row[1]))
                if state != 'sent':
                    # Stop this user's queue until the operator fixes the token/configuration.
                    db.execute('UPDATE recipients SET enabled=0 WHERE id=?', (row[0],))
