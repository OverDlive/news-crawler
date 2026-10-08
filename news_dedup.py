"""Conservative RSS story matching; original articles stay in the database."""
from functools import lru_cache
import re
import unicodedata

from security_profiles import COMPANIES


WINDOW = 48 * 3600
_ALIASES = {re.sub(r'\s+', '', alias).casefold(): company.casefold()
            for company, aliases in COMPANIES.items() for alias in aliases}
_COMPANY = re.compile('|'.join(re.escape(alias) for alias in
                             sorted(_ALIASES, key=len, reverse=True)))
_LABEL = re.compile(r'\[(?:속보|단독|종합\d*|영상|포토|뉴스)[^\]]*\]|\((?:종합\d*|속보|단독|영상|포토)\)')


def _amount(match):
    return str(int(match[1]) * 10000 + int(match[2] or 0) * 1000 + int(match[3] or 0))


def normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    value = _LABEL.sub('', value)
    value = re.sub(r'(?<=\d),(?=\d)', '', value)
    value = re.sub(r'\s+', '', value)
    value = re.sub(r'(\d+)만(?:(\d+)천)?(\d+)?', _amount, value)
    value = re.sub(r'(\d+)천', lambda m: str(int(m[1]) * 1000), value)
    value = re.sub(r'(?<=\d)여(?=명|건)', '', value)
    return _COMPANY.sub(lambda m: _ALIASES[m[0]], value)


def _grams(value):
    return frozenset(value[i:i + 2] for i in range(len(value) - 1))


def _dice(left, right):
    return 2 * len(left & right) / (len(left) + len(right)) if left and right else 0


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
    a = _features(left['title'], left['description'])
    b = _features(right['title'], right['description'])
    # Related group companies and differently sized incidents are separate stories.
    if a[5] and b[5] and a[5] != b[5]:
        return False
    if a[6] and b[6] and not a[6] & b[6]:
        return False
    if a[7] != b[7]:
        return False
    if not a[5] and not b[5] and a[8] != b[8]:
        return False
    if a[0] == b[0]:
        return True
    if min(len(a[2]), len(b[2])) < 12:
        return False
    title_score = _dice(a[1], b[1])
    core_score = _dice(a[3], b[3])
    description_score = _dice(a[4], b[4]) if min(len(a[4]), len(b[4])) >= 30 else 0
    return (title_score >= .82 or core_score >= .78
            or (a[5] and a[5] == b[5] and a[6] & b[6] and core_score >= .64)
            or (a[5] and a[5] == b[5] and description_score >= .28
                and (core_score >= .62 or (a[6] & b[6] and title_score >= .48)))
            or (max(title_score, core_score) >= .48
                and description_score >= .68))


def deduplicate(rows):
    """Rows arrive newest first. Match representatives, avoiding similarity chains."""
    representatives = []
    for row in rows:
        match = next((item for item in representatives if same_story(item, row)), None)
        if match is None:
            representatives.append({**row, 'keywords': list(row['keywords']),
                                    'related_count': 1, 'sources': [row['source']]})
        else:
            match['keywords'] = list(dict.fromkeys((*match['keywords'], *row['keywords'])))
            match['discovered'] = min(match['discovered'], row['discovered'])
            match['related_count'] += 1
            match['sources'] = list(dict.fromkeys((*match['sources'], row['source'])))
    return representatives
