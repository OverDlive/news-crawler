"""Conservative RSS story matching; original articles stay in the database."""
from functools import lru_cache
from decimal import Decimal
import html
import re
import unicodedata

from security_profiles import INSTITUTIONS


WINDOW = 48 * 3600
_ALIASES = {re.sub(r'\s+', '', alias).casefold(): company.casefold()
            for company, aliases in INSTITUTIONS.items() for alias in [company, *aliases]}
_COMPANY = re.compile('|'.join(re.escape(alias) for alias in
                             sorted(_ALIASES, key=len, reverse=True)))
_LABEL = re.compile(r'\[(?:속보|단독|종합\d*|영상|포토|뉴스|(?:\d{4}\s*)?국(?:정감사|감))[^\]]*\]|'
                    r'\((?:종합\d*|속보|단독|영상|포토)\)')


def _amount(match):
    return str(int(Decimal(match[1]) * 10000) + int(match[2] or 0) * 1000 + int(match[3] or 0))


def normalize(value):
    value = unicodedata.normalize('NFKC', html.unescape(value)).casefold()
    value = re.sub(r'[\u200b-\u200d\ufeff]', '', value)
    value = _LABEL.sub('', value)
    value = re.sub(r'(?<=\d),(?=\d)', '', value)
    value = re.sub(r'\s+', '', value)
    value = re.sub(r'(\d+(?:\.\d+)?)만(?:(\d+)천)?(\d+)?', _amount, value)
    value = re.sub(r'(\d+(?:\.\d+)?)천', lambda m: str(int(Decimal(m[1]) * 1000)), value)
    value = re.sub(r'(?<=\d)여(?=명|건)', '', value)
    return _COMPANY.sub(lambda m: _ALIASES[m[0]], value)


def headline(article):
    """Remove a known publisher suffix without dropping substantive headline text."""
    return _headline(article['title'], article.get('source', ''))


@lru_cache(maxsize=8192)
def _headline(title, source):
    title = html.unescape(title).strip()
    source = source.strip()
    if source:
        title = re.sub(r'\s+[-–—|]\s*' + re.escape(source) + r'\s*$', '', title, flags=re.I)
    return title


def title_key(article):
    return _title_key(headline(article))


@lru_cache(maxsize=8192)
def _title_key(title):
    value = re.sub(r'(?<=\d)\.(?=\d)', '_decimal_', normalize(title))
    return re.sub(r'[^\w]', '', value)


def _grams(value):
    return frozenset(value[i:i + 2] for i in range(len(value) - 1))


def _dice(left, right):
    return 2 * len(left & right) / (len(left) + len(right)) if left and right else 0


@lru_cache(maxsize=8192)
def _event_features(title):
    value = normalize(title)
    for pattern, replacement in (
        (r'개인정보|고객정보|회원정보|직원정보', '개인정보'),
        (r'외부노출|외부유출|노출|정보탈취', '유출'),
        (r'사이버공격|침해사고|해킹사고', '해킹'),
        (r'점검회의|대책회의|긴급회의', '점검회의'),
        (r'비번', '비밀번호'),
    ):
        value = re.sub(pattern, replacement, value)
    kinds = frozenset(i for i, pattern in enumerate((
        r'유출|해킹|랜섬웨어|디도스', r'과징금|과태료|시정명령|행정처분',
        r'점검회의', r'대회|훈련|세미나', r'임금|연봉|급여|임단협',
        r'건설|착공', r'대응지연|늑장|자료요구|초기대응',
    )) if re.search(pattern, value))
    return _grams(re.sub(r'[^\w]', '', value)), kinds


@lru_cache(maxsize=8192)
def _quantities(title):
    title = normalize(title)
    # A changed count is identified by the new total, not its historical value.
    title = re.sub(r'\d+(명|건)?(?:→|->|에서)(\d+)(명|건)',
                   lambda m: m[2] + m[3] if not m[1] or m[1] == m[3] else m[0], title)
    values = re.findall(r'(\d+(?:\.\d+)?)(명|건|개사|곳|억원|만원|원|%)', title)
    return frozenset((number, '기관' if unit in ('개사', '곳') else unit)
                     for number, unit in values)


@lru_cache(maxsize=8192)
def _delays(title):
    return frozenset(re.findall(r'(\d+)일(?:뒤|간|만|후)', normalize(title)))


@lru_cache(maxsize=8192)
def _features(title, description):
    lead = re.split(r'[\s,，:…]+', _LABEL.sub('', title).strip(), maxsplit=1)[0]
    title = normalize(title)
    # A quote after an ellipsis usually adds a publisher's angle to the same event.
    core = re.split(r'…|\.{2,}', title, maxsplit=1)[0]
    if len(core) < 12:
        core = title
    clean = lambda text: re.sub(r'[^\w]', '', text)
    core = clean(core)
    return (clean(title), _grams(clean(title)), core, _grams(core),
            _grams(clean(normalize(description))),
            frozenset(_COMPANY.findall(title)), frozenset(re.findall(r'\d+', core)),
            bool(re.search(r'(?:노출|유출|해킹|발생).{0,6}(?:없|않|아니)', core)), normalize(lead))


def same_story(left, right):
    if abs(left['published'] - right['published']) > WINDOW:
        return False
    left = {**left, 'title': headline(left)}
    right = {**right, 'title': headline(right)}
    a = _features(left['title'], left.get('description', ''))
    b = _features(right['title'], right.get('description', ''))
    # Related group companies and differently sized incidents are separate stories.
    if a[5] and b[5] and a[5] != b[5]:
        return False
    x, y = _quantities(left['title']), _quantities(right['title'])
    for unit in {unit for _, unit in x} & {unit for _, unit in y}:
        if {n for n, u in x if u == unit}.isdisjoint(n for n, u in y if u == unit):
            return False
    if a[7] != b[7]:
        return False
    # A cancelled opening or a different explicitly named year is an update,
    # even when the original headline is contained in the new one.
    changed = r'(?:공개|개소|개최|협약|행사|발표).{0,6}(?:취소|연기|철회)'
    if bool(re.search(changed, normalize(left['title']))) != bool(re.search(changed, normalize(right['title']))):
        return False
    years = [set(re.findall(r'(20\d{2})년', normalize(row['title']))) for row in (left, right)]
    if all(years) and years[0].isdisjoint(years[1]):
        return False
    # Formatting can change the first word, even when the full title is identical.
    if a[0] and a[0] == b[0]:
        return True
    contained = min(len(a[0]), len(b[0])) >= 18 and (a[0] in b[0] or b[0] in a[0])
    if not a[5] and not b[5] and a[8] != b[8] and not contained:
        return False
    if contained:
        return True
    if min(len(a[2]), len(b[2])) < 12:
        return False
    title_score = _dice(a[1], b[1])
    core_score = _dice(a[3], b[3])
    description_score = _dice(a[4], b[4]) if min(len(a[4]), len(b[4])) >= 30 else 0
    ea, ka = _event_features(left['title'])
    eb, kb = _event_features(right['title'])
    # Entity + event + wording evidence. A shared institution alone is not an issue.
    event_match = bool(a[5] and a[5] == b[5] and ka and ka == kb and (
        _dice(ea, eb) >= .62
        or (_dice(ea, eb) >= .46 and a[6] & b[6] and description_score >= .28)
        # Matching structured facts survive very different publishers' headlines.
        or (0 in ka and x & y and _dice(ea, eb) >= .40)
        or (6 in ka and _delays(left['title']) & _delays(right['title'])
            and _dice(ea, eb) >= .32)))
    return (title_score >= .82 or core_score >= .78
            or event_match
            or (a[5] and a[5] == b[5] and a[6] & b[6] and core_score >= .64)
            or (a[5] and a[5] == b[5] and description_score >= .28
                and (core_score >= .62 or (a[6] & b[6] and title_score >= .48)))
            or (max(title_score, core_score) >= .48
                and description_score >= .68))


def deduplicate(rows, body_matches=None):
    """Rows arrive newest first. Match representatives, avoiding similarity chains."""
    representatives = []
    body_matches = body_matches or {}
    def matches(left, right):
        key = tuple(sorted((left['url'], right['url'])))
        if key in body_matches:
            score = body_matches[key]
            if score < 0:  # Explicit conflict in company, victim count or event.
                return False
            if score >= .82:
                return True
            # Low text overlap is inconclusive for paraphrased reports. It must
            # not split articles already established as the same titled event.
        return same_story(left, right)
    for row in rows:
        # Full bodies belong in the extraction cache, not cards/notification JSON.
        row = {key: value for key, value in row.items() if key != 'body'}
        match = next((item for item in representatives
                      if all(matches(member, row) for member in item['related_articles'])), None)
        if match is None:
            representatives.append({**row, 'keywords': list(row['keywords']),
                                    'related_count': 1, 'sources': [row['source']],
                                    'related_articles': [dict(row)]})
        else:
            match['keywords'] = list(dict.fromkeys((*match['keywords'], *row['keywords'])))
            match['discovered'] = min(match['discovered'], row['discovered'])
            match['related_count'] += 1
            match['sources'] = list(dict.fromkeys((*match['sources'], row['source'])))
            match['related_articles'].append(dict(row))
    return representatives
