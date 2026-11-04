"""Body-first story evidence, calculated only by the background worker."""
from functools import lru_cache
from collections import Counter, OrderedDict
import hashlib
import math
import re
import threading

from news_dedup import WINDOW, _features, _quantities, normalize

VERSION = 'tfidf-body-v3'
MIN_BODY = 150
# Negative evidence is distinct from an inconclusive similarity measurement.
CONFLICT = -1.0
_AUTHORITIES = frozenset(normalize(name) for name in
                         ('금융감독원', '금융위원회', '개인정보보호위원회', '경찰청', '검찰청'))


# Bodies can be 60,000 characters, and each shingle set contains thousands
# of Python strings. Bound the working set; durable pair scores live in SQLite.
@lru_cache(maxsize=128)
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


_FEATURE_LIMIT = 120000
_feature_cache = OrderedDict()
_feature_size = 0
_feature_lock = threading.Lock()
_UNIQUE_IDF_SQUARED = (1 + math.log(3 / 2)) ** 2


def _body_features(text):
    """Reuse document features, bounded by actual vocabulary size, not body count."""
    global _feature_size
    with _feature_lock:
        if text in _feature_cache:
            _feature_cache.move_to_end(text)
            return _feature_cache[text]
    # Match TfidfVectorizer's char analyzer, lowercase and whitespace handling.
    normalized = re.sub(r'\s\s+', ' ', text.lower())
    counts = Counter(normalized[i:i + n] for n in (3, 4, 5)
                     for i in range(len(normalized) - n + 1))
    weights = {gram: 1 + math.log(count) for gram, count in counts.items()}
    shingles = frozenset(text[i:i + 3] for i in range(len(text) - 2))
    result = (weights, sum(value * value for value in weights.values()), shingles)
    cost = len(weights) + len(shingles)
    with _feature_lock:
        if text in _feature_cache:
            return _feature_cache[text]
        if cost <= _FEATURE_LIMIT:
            while _feature_cache and (_feature_size + cost > _FEATURE_LIMIT or len(_feature_cache) >= 64):
                old = _feature_cache.popitem(last=False)[1]
                _feature_size -= len(old[0]) + len(old[2])
            _feature_cache[text] = result
            _feature_size += cost
    return result


def body_score(left, right):
    if min(len(left), len(right)) < MIN_BODY:
        return 0.0
    x, xx, a = _body_features(left)
    y, yy, b = _body_features(right)
    if len(x) > len(y):
        x, y, xx, yy = y, x, yy, xx
    # In a two-document corpus, shared grams have IDF 1; unique grams have
    # IDF 1 + log(3/2). This is the same smoothed, sublinear, L2 TF-IDF cosine
    # without rebuilding and sorting a sparse matrix for every pair.
    dot = common_x = common_y = 0.0
    for gram, weight in x.items():
        other = y.get(gram)
        if other is not None:
            dot += weight * other
            common_x += weight * weight
            common_y += other * other
    norm_x = common_x + _UNIQUE_IDF_SQUARED * max(0, xx - common_x)
    norm_y = common_y + _UNIQUE_IDF_SQUARED * max(0, yy - common_y)
    cosine = min(1.0, dot / math.sqrt(norm_x * norm_y)) if norm_x and norm_y else 0.0
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
    hashes = {}
    for row in rows:
        if stopped.is_set():
            return results
        hashes[row['url']] = fingerprint(row['body'])
    for index, left in enumerate(rows):
        for other_index in range(index + 1, len(rows)):
            right = rows[other_index]
            if stopped.is_set():
                return results
            if left['published'] - right['published'] > WINDOW:
                break
            a, b = sorted((left, right), key=lambda r: r['url'])
            key = (a['url'], b['url'], hashes[a['url']], hashes[b['url']])
            if key in cached:
                continue
            score = body_score(clean_body(a['body']), clean_body(b['body'])) if eligible(a, b) else CONFLICT
            results.append((*key, score))
    return results
