"""Shared durable story suppression for news notifications."""
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from news_dedup import same_story


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


def prepare_queue(db, owner_column, owner):
    """Run inside BEGIN IMMEDIATE before claiming, including legacy pending rows.

    Sent/ambiguous claims remain anchors across restarts. Failed sends are not
    anchors; explicit retries remain possible. Test messages bypass news policy.
    """
    rows = db.execute(f'SELECT url,article,state FROM deliveries WHERE {owner_column}=? ORDER BY rowid',
                      (owner,)).fetchall()
    anchors = [json.loads(row[1]) for row in rows
               if row[2] in ('sent', 'sending', 'unknown') and not row[0].startswith('test:')]
    pending = [(row[0], json.loads(row[1])) for row in rows
               if row[2] == 'pending' and not row[0].startswith('test:')]
    accepted = []
    for url, article in pending:
        if any(duplicate(article, anchor) for anchor in anchors):
            state, detail = 'skipped', '같은 뉴스의 공유 이력 또는 대기 기사가 있어 제외'
        else:
            state, detail = 'pending', ''
            anchors.append(article)
            accepted.append((url, article))
        db.execute(f'UPDATE deliveries SET state=?,detail=? WHERE {owner_column}=? AND url=?',
                   (state, detail, owner, url))
    return accepted
