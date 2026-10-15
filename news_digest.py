"""Institution roundups for sharing; individual story records remain separate."""
from news_dedup import _COMPANY, normalize
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
    items = article.get('digest_articles')
    if items:
        header = f"[{clipped(article['institution'], 100)}] 주요 뉴스 {len(items)}건"
        return header + '\n\n' + '\n\n'.join(
            f"{index}. {clipped(item['title'], 220)}\n"
            f"{clipped(item.get('source', ''), 80)}\n{item['url']}"
            for index, item in enumerate(items, 1))
    return (clipped(article.get('title', ''), 500) + '\n' +
            clipped(article.get('source', ''), 100) + '\n\n' +
            clipped(article.get('description', ''), 600) + '\n\n' +
            clipped(article['url'], 1500))


def select_digest(pending, *, max_articles, fits=lambda article: True):
    """Select one priority-ordered institution batch, returning exact claimed rows."""
    first_key, first = pending[0]
    name = institution(first)
    selected = [(first_key, first)]
    if not name:
        return selected, first
    outbound = first
    for key, article in pending[1:]:
        if len(selected) >= max_articles:
            break
        if institution(article) != name:
            continue
        proposed = [*selected, (key, article)]
        digest = {**first, 'institution': name,
                  'digest_articles': [item for _, item in proposed]}
        if fits(digest):
            selected, outbound = proposed, digest
    return selected, outbound


def delivery_detail(article, detail):
    if article.get('digest_articles'):
        return f"{article['institution']} 기관 묶음 {len(article['digest_articles'])}건 · {detail}"
    return detail
