"""Conservative body matching. Called only by the background worker."""
from functools import lru_cache
import hashlib
import re

from news_dedup import WINDOW, _features, normalize

VERSION = 'tfidf-char-v1'
MIN_BODY = 150


@lru_cache(maxsize=4096)
def clean_body(text):
    lines = re.split(r'\n+|(?<=[.!?。])\s+', text)
    lines = [line for line in lines if not re.search(
        r'무단\s*전재|재배포\s*금지|저작권|ⓒ|구독|기자\s*메일|[\w.+-]+@[\w.-]+', line)]
    return normalize(' '.join(dict.fromkeys(lines)))[:30000]


def fingerprint(body):
    return hashlib.sha256((VERSION + clean_body(body)).encode()).hexdigest()


def eligible(left, right):
    if abs(left['published'] - right['published']) > WINDOW:
        return False
    a, b = (_features(row['title'], row.get('description', '')) for row in (left, right))
    if a[5] and b[5] and a[5] != b[5]:
        return False
    if a[6] and b[6] and not a[6] & b[6]:
        return False
    if a[7] != b[7]:
        return False
    events = (r'유출|노출|해킹|랜섬웨어', r'임금|연봉|급여|임단협',
              r'보호대책|보안강화|보호강화', r'발전소건설|발전소착공')
    kinds = [{i for i, pattern in enumerate(events) if re.search(pattern, normalize(row['title']))}
             for row in (left, right)]
    if all(kinds) and kinds[0].isdisjoint(kinds[1]):
        return False
    # Dates and incident sizes are evidence, not arbitrary numbers in footers.
    pattern = r'\d+(?:년|월|일|명|건|원|%|곳|개사)'
    x, y = (set(re.findall(pattern, clean_body(row.get('body', ''))))
            for row in (left, right))
    for unit in ('년', '월', '일', '명', '건', '원', '%', '곳', '개사'):
        ax, by = ({value for value in values if value.endswith(unit)} for values in (x, y))
        if ax and by and ax.isdisjoint(by):
            return False
    return True


@lru_cache(maxsize=8192)
def body_score(left, right):
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer
    if min(len(left), len(right)) < MIN_BODY:
        return 0.0
    vectors = TfidfVectorizer(analyzer='char', ngram_range=(3, 5),
                             sublinear_tf=True, dtype=np.float32).fit_transform([left, right])
    return float(vectors[0].multiply(vectors[1]).sum())


def compare_bodies(rows, cached, stopped):
    """Cache positive AND negative decisions by URL and cleaned-body fingerprint."""
    rows = sorted((r for r in rows if len(clean_body(r.get('body', ''))) >= MIN_BODY),
                  key=lambda r: r['published'], reverse=True)
    results = []
    for index, left in enumerate(rows):
        for right in rows[index + 1:]:
            if stopped.is_set():
                return results
            if left['published'] - right['published'] > WINDOW:
                break
            a, b = sorted((left, right), key=lambda r: r['url'])
            key = (a['url'], b['url'], fingerprint(a['body']), fingerprint(b['body']))
            if key in cached:
                continue
            score = body_score(clean_body(a['body']), clean_body(b['body'])) if eligible(a, b) else 0.0
            results.append((*key, score))
    return results
