"""Shared durable story suppression for news notifications."""
import json
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from news_dedup import same_story
from news_importance import MIN_SHARE_SCORE, share_priority


def initialize_cutoffs(db):
    db.execute('CREATE TABLE IF NOT EXISTS sharing_cutoffs (owner TEXT PRIMARY KEY, cutoff REAL NOT NULL)')


def sharing_cutoff(db, owner):
    row = db.execute('SELECT cutoff FROM sharing_cutoffs WHERE owner=?', (str(owner),)).fetchone()
    return row[0] if row else None


def reset_queue(db, owner_column, owner):
    """Caller holds BEGIN IMMEDIATE; retain sent and uncertain delivery evidence."""
    db.execute('INSERT OR REPLACE INTO sharing_cutoffs(owner,cutoff) VALUES(?,?)',
               (str(owner), time.time()))
    return db.execute(f"UPDATE deliveries SET state='reset',detail='대기열 초기화로 공유 제외' "
                      f"WHERE {owner_column}=? AND state IN ('pending','filtered','failed','skipped')",
                      (owner,)).rowcount


def canonical_url(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and key.lower() not in ('fbclid', 'gclid')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       urlencode(sorted(query)), ''))


def members(article):
    return [article, *article.get('related_articles', [])]


def duplicate(left, right):
    for a in members(left):
        for b in members(right):
            if canonical_url(a['url']) == canonical_url(b['url']):
                return True
            a = {**a, 'description': a.get('description', ''),
                 'published': a.get('published', a.get('discovered', 0))}
            b = {**b, 'description': b.get('description', ''),
                 'published': b.get('published', b.get('discovered', 0))}
            if same_story(a, b):
                return True
    return False


def remember_article(db, owner_column, owner, article):
    """Refresh evidence as background grouping improves, preserving delivery state."""
    row = db.execute(f'SELECT article,state FROM deliveries WHERE {owner_column}=? AND url=?',
                     (owner, article['url'])).fetchone()
    cutoff = sharing_cutoff(db, owner)
    if not row and cutoff is not None and article.get('discovered', 0) <= cutoff:
        return
    if row:
        old = json.loads(row[0])
        snapshot = dict(article if row[1] in ('pending', 'filtered') else old)
        # Retain body-derived group membership even if later keyword views shrink.
        related = {member['url']: {key: value for key, value in member.items()
                                  if key not in ('related_articles', 'body')}
                   for member in [*members(old), *members(article)]}
        snapshot['related_articles'] = list(related.values())
        db.execute(f'UPDATE deliveries SET article=? WHERE {owner_column}=? AND url=?',
                   (json.dumps(snapshot, ensure_ascii=False), owner, article['url']))
    else:
        db.execute(f'INSERT INTO deliveries({owner_column},url,article) VALUES(?,?,?)',
                   (owner, article['url'], json.dumps(article, ensure_ascii=False)))


def prepare_queue(db, owner_column, owner):
    """Run inside BEGIN IMMEDIATE before claiming, including legacy pending rows.

    Sent/ambiguous claims remain anchors across restarts. Failed sends are not
    anchors; explicit retries remain possible. Test messages bypass news policy.
    """
    rows = db.execute(f'SELECT url,article,state,detail FROM deliveries WHERE {owner_column}=? ORDER BY rowid',
                      (owner,)).fetchall()
    anchors = [json.loads(row[1]) for row in rows
               if (row[2] in ('sent', 'sending', 'unknown') or
                   row[2] == 'skipped' and row[3].startswith('기관 대표 기사 발송으로 공유 제외'))
               and not row[0].startswith('test:')]
    pending = [(row[0], json.loads(row[1])) for row in rows
               if row[2] in ('pending', 'filtered') and not row[0].startswith('test:')]
    pending.sort(key=lambda item: share_priority(item[1])[0], reverse=True)
    accepted = []
    cutoff = sharing_cutoff(db, owner)
    for url, article in pending:
        if cutoff is not None and article.get('discovered', 0) <= cutoff:
            state, detail = 'reset', '대기열 초기화 이전 기사로 공유 제외'
        elif any(duplicate(article, anchor) for anchor in anchors):
            state, detail = 'skipped', '같은 뉴스의 공유 이력 또는 대기 기사가 있어 제외'
        else:
            score, reason = share_priority(article)
            if score < MIN_SHARE_SCORE:
                state, detail = 'filtered', f'중요도 제외 ({score}점): {reason}'
            else:
                state, detail = 'pending', f'공유 중요도 {score}점: {reason}'
                anchors.append(article)
                accepted.append((url, article))
        db.execute(f'UPDATE deliveries SET state=?,detail=? WHERE {owner_column}=? AND url=?',
                   (state, detail, owner, url))
    return accepted
