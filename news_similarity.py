"""Body-first story evidence, calculated only by the background worker."""
from functools import lru_cache
import hashlib
import re

from news_dedup import WINDOW, _features, _quantities, normalize

VERSION = 'tfidf-body-v2'
MIN_BODY = 150
# Negative evidence is distinct from an inconclusive similarity measurement.
CONFLICT = -1.0
_AUTHORITIES = frozenset(normalize(name) for name in
                         ('금융감독원', '금융위원회', '개인정보보호위원회', '경찰청', '검찰청'))


@lru_cache(maxsize=4096)
def clean_body(text):
    # Some publishers include recommendation lists inside their article container.
    # Their dates, victim counts and unrelated companies must not become evidence.
    text = re.split(r'(?m)^\s*(?:관련\s*기사|추천\s*기사|인기\s*기사|주요\s*뉴스|'
                    r'키워드|다른\s*기사\s*보기|저작권자|내\s*댓글)(?:\s|$)', text, maxsplit=1)[0]
    text = re.split(r'무단\s*전재|재배포\s*금지|저작권|ⓒ|©', text, maxsplit=1)[0]
    lines = re.split(r'\n+|(?<=[.!?。])\s+', text)
    lines = [line for line in lines if not re.search(
        r'^\s*(?:기자명|입력\s|수정\s|발행일|사진[/:=\s]|공유하기|페이스북|'
        r'글자크기|URL\s*복사|구글(?:에서|\s*검색)|네이버(?:에서|\s*검색)|'
        r'회원\s*로그인|자동등록방지)|'
        r'^\s*[가-힣]{2,4}\s*기자(?:\s|$)|구독|기자\s*메일|[\w.+-]+@[\w.-]+', line)]
    return re.sub(r'[^\w]', '', normalize(' '.join(dict.fromkeys(lines))))[:30000]


def fingerprint(body):
    return hashlib.sha256((VERSION + clean_body(body)).encode()).hexdigest()


def eligible(left, right):
    if abs(left['published'] - right['published']) > WINDOW:
        return False
    a, b = (_features(row['title'], row.get('description', '')) for row in (left, right))
    subjects_a, subjects_b = a[5] - _AUTHORITIES, b[5] - _AUTHORITIES
    if subjects_a and subjects_b and subjects_a != subjects_b:
        return False
    # "2차 피해" and "250명" are different facts, not contradictory numbers.
    x, y = _quantities(left['title']), _quantities(right['title'])
    for unit in {unit for _, unit in x} & {unit for _, unit in y}:
        if {n for n, u in x if u == unit}.isdisjoint(n for n, u in y if u == unit):
            return False
    if a[7] != b[7]:
        return False
    events = (r'유출|노출|해킹|랜섬웨어', r'임금|연봉|급여|임단협',
              r'발전소건설|발전소착공')
    kinds = [{i for i, pattern in enumerate(events) if re.search(pattern, normalize(row['title']))}
             for row in (left, right)]
    if all(kinds) and kinds[0].isdisjoint(kinds[1]):
        return False
    # Publication dates and times can vary while the event is identical.
    # Victim counts in the actual article still protect materially different news.
    pattern = r'\d+(?:명|건)'
    x, y = (set(re.findall(pattern, clean_body(row.get('body', ''))))
            for row in (left, right))
    for unit in ('명', '건'):
        ax, by = ({value for value in values if value.endswith(unit)} for values in (x, y))
        if ax and by and ax.isdisjoint(by):
            return False
    return True


@lru_cache(maxsize=4096)
def _shingles(text):
    return frozenset(text[i:i + 3] for i in range(len(text) - 2))


@lru_cache(maxsize=8192)
def body_score(left, right):
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer
    if min(len(left), len(right)) < MIN_BODY:
        return 0.0
    vectors = TfidfVectorizer(analyzer='char', ngram_range=(3, 5),
                             sublinear_tf=True, dtype=np.float32).fit_transform([left, right])
    cosine = float(vectors[0].multiply(vectors[1]).sum())
    a, b = _shingles(left), _shingles(right)
    common = len(a & b)
    coverage = common / min(len(a), len(b))
    balance = min(len(a), len(b)) / max(len(a), len(b))
    # An almost verbatim shortened article can have a low TF-IDF cosine because
    # the longer version contains extra quotes. Require substantial shared text.
    if coverage >= .90 and balance >= .55 and common >= MIN_BODY:
        return max(cosine, coverage)
    return cosine


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
            score = body_score(clean_body(a['body']), clean_body(b['body'])) if eligible(a, b) else CONFLICT
            results.append((*key, score))
    return results
