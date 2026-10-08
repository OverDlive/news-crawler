"""Telegram bot discovery and durable news delivery, with DPAPI credentials."""
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from kakao_notifications import protect


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        message = {401: '봇 토큰이 유효하지 않습니다.',
                   403: '봇의 방 참여 상태와 메시지 게시 권한을 확인하세요.',
                   409: '다른 프로그램의 봇 업데이트 수신을 중지하세요.',
                   429: '발송 제한으로 잠시 대기합니다.'}.get(code,
                   '텔레그램 연결 또는 요청에 실패했습니다. 설정을 확인하세요.')
        super().__init__(message)


def api(token, method, fields=None):
    # Never expose exception URLs or remote descriptions: the URL contains the token.
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(fields or {}, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        delay = 0
        if exc.code == 429:
            try:
                delay = json.loads(exc.read(8192)).get('parameters', {}).get('retry_after', 30)
            except (ValueError, AttributeError, OSError):
                delay = 30
        raise TelegramError(exc.code, delay if type(delay) is int else 30) from None
    except (OSError, ValueError):
        raise TelegramError() from None
    if not isinstance(result, dict) or result.get('ok') is not True:
        code = result.get('error_code', 0) if isinstance(result, dict) else 0
        parameters = result.get('parameters', {}) if isinstance(result, dict) else {}
        delay = parameters.get('retry_after', 30) if isinstance(parameters, dict) else 30
        raise TelegramError(code, delay if type(delay) is int and delay > 0 else 30)
    return result.get('result')


def article_text(article):
    # Plain text avoids HTML/Markdown injection and UTF-16 length surprises.
    def clipped(value, units):
        return str(value).encode('utf-16-le')[:units * 2].decode('utf-16-le', errors='ignore')
    return (clipped(article.get('title', ''), 500) + '\n' +
            clipped(article.get('source', ''), 100) + '\n\n' +
            clipped(article.get('description', ''), 600) + '\n\n' +
            clipped(article['url'], 1500))


class TelegramNotifications:
    def __init__(self, directory):
        self.path = directory / 'telegram.sqlite3'
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL DEFAULT '',
                    bot_id INTEGER, username TEXT NOT NULL DEFAULT '', chat_id INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 0, registered REAL NOT NULL DEFAULT 0,
                    next_send REAL NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '');
                INSERT OR IGNORE INTO settings(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, title TEXT, kind TEXT);
                CREATE TABLE IF NOT EXISTS deliveries (
                    chat_id INTEGER, url TEXT, article TEXT, state TEXT DEFAULT 'pending',
                    detail TEXT DEFAULT '', claimed REAL DEFAULT 0,
                    PRIMARY KEY(chat_id,url));
            ''')
            if 'update_offset' not in {row[1] for row in db.execute('PRAGMA table_info(settings)')}:
                db.execute('ALTER TABLE settings ADD COLUMN update_offset INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            row = dict(db.execute('SELECT * FROM settings WHERE id=1').fetchone())
        row['has_token'] = bool(row.pop('token'))
        return row

    def chats(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM chats ORDER BY title,id')]

    def discover(self, token='', request=api):
        with self.lock:
            with self.connect() as db:
                previous = db.execute('SELECT token,bot_id,update_offset FROM settings WHERE id=1').fetchone()
            token = token.strip()
            if token:
                if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,200}', token):
                    raise ValueError('BotFather가 발급한 봇 토큰을 입력하세요.')
            elif previous['token']:
                token = protect(previous['token'], True)
            else:
                raise ValueError('먼저 봇 토큰을 입력하세요.')
            bot = request(token, 'getMe')
            if not isinstance(bot, dict) or bot.get('is_bot') is not True or type(bot.get('id')) is not int:
                raise ValueError('봇 계정을 확인할 수 없습니다.')
            webhook = request(token, 'getWebhookInfo')
            if not isinstance(webhook, dict) or webhook.get('url'):
                raise ValueError('다른 서비스의 웹훅이 연결된 봇입니다. 새 봇을 만들어 사용하세요.')
            encrypted = protect(token)
            with self.connect() as db:
                if previous['bot_id'] != bot['id']:
                    db.execute('DELETE FROM chats')
                    db.execute('DELETE FROM deliveries')
                    db.execute("UPDATE settings SET chat_id=NULL,enabled=0,registered=0,next_send=0,update_offset=0,detail='' WHERE id=1")
                db.execute('UPDATE settings SET token=?,bot_id=?,username=? WHERE id=1',
                           (encrypted, bot['id'], bot.get('username', '')))
            updates = request(token, 'getUpdates', {'timeout': 0, 'limit': 100,
                'offset': previous['update_offset'] if previous['bot_id'] == bot['id'] else 0,
                'allowed_updates': ['message', 'channel_post', 'my_chat_member']})
            if not isinstance(updates, list):
                raise ValueError('대화방 목록 응답을 확인할 수 없습니다.')
            # This monitor owns update consumption; use a dedicated bot.
            found = {}
            for update in updates:
                if not isinstance(update, dict):
                    continue
                for name in ('message', 'channel_post', 'my_chat_member'):
                    item = update.get(name, {})
                    chat = item.get('chat', {}) if isinstance(item, dict) else {}
                    if chat.get('type') in ('group', 'supergroup', 'channel') and type(chat.get('id')) is int:
                        found[chat['id']] = chat
            with self.connect() as db:
                for chat in found.values():
                    db.execute('INSERT OR REPLACE INTO chats VALUES(?,?,?)',
                               (chat['id'], chat.get('title', str(chat['id'])), chat['type']))
                ids = [u['update_id'] for u in updates if isinstance(u, dict) and type(u.get('update_id')) is int]
                if ids:
                    db.execute('UPDATE settings SET update_offset=? WHERE id=1', (max(ids) + 1,))
            return self.chats()

    def activate(self, chat_id, request=api):
        with self.lock:
            with self.connect() as db:
                config = db.execute('SELECT * FROM settings WHERE id=1').fetchone()
                chat = db.execute('SELECT * FROM chats WHERE id=?', (chat_id,)).fetchone()
            if not chat or not config['token']:
                raise ValueError('방 찾기를 완료한 뒤 대상 방을 선택하세요.')
            member = request(protect(config['token'], True), 'getChatMember',
                             {'chat_id': chat_id, 'user_id': config['bot_id']})
            if not isinstance(member, dict) or member.get('status') not in ('creator', 'administrator', 'member', 'restricted'):
                raise ValueError('선택한 방에 봇을 초대하세요.')
            if chat['kind'] == 'channel' and not (member.get('status') == 'creator' or
                    member.get('status') == 'administrator' and member.get('can_post_messages') is True):
                raise ValueError('채널에서 봇에게 메시지 게시 관리자 권한을 주세요.')
            if member.get('status') == 'restricted' and (not member.get('is_member') or not member.get('can_send_messages')):
                raise ValueError('봇에게 메시지 전송 권한을 주세요.')
            with self.connect() as db:
                registered = config['registered'] if config['chat_id'] == chat_id else time.time()
                db.execute("UPDATE settings SET chat_id=?,enabled=1,registered=?,next_send=0,detail='' WHERE id=1",
                           (chat_id, registered))

    def pause(self):
        with self.lock, self.connect() as db:
            db.execute('UPDATE settings SET enabled=0 WHERE id=1')

    def enqueue(self, articles):
        # Enqueue runs on the GUI thread; never wait for a network worker's lock.
        with self.connect() as db:
            config = db.execute('SELECT * FROM settings WHERE id=1').fetchone()
            if not config['enabled'] or config['chat_id'] is None:
                return
            for article in articles:
                if article['discovered'] >= config['registered']:
                    db.execute('INSERT OR IGNORE INTO deliveries(chat_id,url,article) VALUES(?,?,?)',
                               (config['chat_id'], article['url'], json.dumps(article, ensure_ascii=False)))

    def status(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT state,detail,url FROM deliveries ORDER BY rowid DESC LIMIT 30')]

    def retry_failed(self):
        with self.lock, self.connect() as db:
            db.execute("UPDATE deliveries SET state='pending',detail='' WHERE chat_id=(SELECT chat_id FROM settings WHERE id=1) AND state='failed'")

    def deliver(self, stopped=lambda: False, request=api, test=False):
        with self.lock:
            if stopped():
                return
            now = time.time()
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                stale = db.execute("UPDATE deliveries SET state='unknown',detail='전송 중 종료: 방에서 수신 확인 필요' WHERE state='sending' AND claimed<?", (now - 60,)).rowcount
                if stale:
                    db.execute("UPDATE settings SET enabled=0,detail='전송 결과 불명: 수신 확인 후 다시 시작하세요.' WHERE id=1")
                config = db.execute('SELECT * FROM settings WHERE id=1').fetchone()
                if not config['enabled'] or now < config['next_send']:
                    return
                if test:
                    article = {'title': '[테스트] 뉴스 모니터 연결 확인', 'description': '새 뉴스가 이 방에 자동 공유됩니다.', 'url': 'https://news.google.com'}
                    key = 'test:' + str(time.time_ns())
                    db.execute('INSERT INTO deliveries(chat_id,url,article) VALUES(?,?,?)',
                               (config['chat_id'], key, json.dumps(article, ensure_ascii=False)))
                else:
                    row = db.execute("SELECT url,article FROM deliveries WHERE chat_id=? AND state='pending' ORDER BY rowid LIMIT 1", (config['chat_id'],)).fetchone()
                    if row is None:
                        return
                    key, article = row['url'], json.loads(row['article'])
                db.execute("UPDATE deliveries SET state='sending',claimed=? WHERE chat_id=? AND url=?", (now, config['chat_id'], key))
                db.execute('UPDATE settings SET next_send=? WHERE id=1', (now + 4,))
            state, detail, wait = 'unknown', '전송 결과 불명: 방에서 확인하세요. 자동 재전송하지 않습니다.', 4
            try:
                response = request(protect(config['token'], True), 'sendMessage',
                    {'chat_id': config['chat_id'], 'text': article_text(article),
                     'link_preview_options': {'is_disabled': True}})
                if isinstance(response, dict) and type(response.get('message_id')) is int:
                    state, detail = 'sent', '텔레그램 API 전송 성공'
            except TelegramError as exc:
                if exc.code == 429:
                    state, detail, wait = 'pending', str(exc), max(4, exc.retry_after)
                elif exc.code in (400, 401, 403, 404, 409):
                    state, detail = 'failed', str(exc)
            except Exception:
                # Do not log raw exceptions or automatically repeat ambiguous deliveries.
                pass
            with self.connect() as db:
                db.execute('UPDATE deliveries SET state=?,detail=? WHERE chat_id=? AND url=?',
                           (state, detail, config['chat_id'], key))
                db.execute('UPDATE settings SET next_send=?,detail=? WHERE id=1', (time.time() + wait, detail))
                if state in ('failed', 'unknown'):
                    db.execute('UPDATE settings SET enabled=0 WHERE id=1')
            return state, detail
