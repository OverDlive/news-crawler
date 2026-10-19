"""Select one important representative per institution's pending news."""
from news_dedup import _COMPANY, normalize
from news_importance import share_priority
from security_profiles import INSTITUTIONS


_NAMES = {name.casefold(): name for name in INSTITUTIONS}
_AUTHORITIES = frozenset(normalize(name) for name in
                         ('금융감독원', '금융위원회', '개인정보보호위원회', '경찰청', '검찰청'))


def _subjects(text):
    names = set(_COMPANY.findall(normalize(text)))
    return names - _AUTHORITIES or names


def institution(article):
    """Prefer the headline's subject; ambiguous multi-institution news stays alone."""
    names = _subjects(article.get('title', ''))
    if not names:
        names = set().union(*(_subjects(member.get('title', ''))
                             for member in article.get('related_articles', [])))
    if not names:
        names = set().union(*(_subjects(keyword) for keyword in article.get('keywords', [])))
    names = names - _AUTHORITIES or names
    return _NAMES.get(next(iter(names)), '') if len(names) == 1 else ''


def clipped(value, units):
    return str(value).encode('utf-16-le')[:units * 2].decode('utf-16-le', errors='ignore')


def telegram_text(article):
    # Plain text avoids markup injection and keeps each original article link.
    return (clipped(article.get('title', ''), 500) + '\n' +
            clipped(article.get('source', ''), 100) + '\n\n' +
            clipped(article.get('description', ''), 600) + '\n\n' +
            clipped(article.get('content_url') or article['url'], 1500))


def select_representative(pending):
    """Claim all pending peers so later delivery cycles cannot send the runners-up.

    Higher importance wins; the latest publication breaks ties. Unknown or
    ambiguous institutions stay individual. Future arrivals remain eligible.
    """
    ranked = sorted(pending, key=lambda item: (share_priority(item[1])[0],
                    item[1].get('published', 0)), reverse=True)
    first_key, first = ranked[0]
    name = institution(first)
    if not name:
        return [(first_key, first)], first
    selected = [(key, article) for key, article in ranked if institution(article) == name]
    return selected, {**first, 'institution': name, 'representative_count': len(selected)}


def delivery_detail(article, detail):
    if article.get('representative_count'):
        return f"{article['institution']} 대표 기사 1건 (후보 {article['representative_count']}건) · {detail}"
    return detail


def delivery_results(batch, article, state, detail, owner):
    """Only the representative was sent; retain retry/uncertainty for all claims."""
    for key, _ in batch:
        excluded = state == 'sent' and len(batch) > 1 and key != article['url']
        yield ('skipped' if excluded else state,
               '기관 대표 기사 발송으로 공유 제외 · ' + detail if excluded else detail,
               owner, key)
