"""Company aliases AND any topic phrase, without Boolean expansion."""
import re
from search_rules import RuleError, quote, normalize
from institution_catalog import (GOVERNMENT_BODIES, PUBLIC_INSTITUTIONS, CATALOG_VERSION,
                                 FINANCIAL_INSTITUTIONS, FINANCIAL_CATALOG_VERSION)

COMPANIES = {
    "한국전력": ["한국전력", "한국전력공사", "한전"],
    "한국수력원자력": ["한국수력원자력", "한수원"],
    "한국남동발전": ["한국남동발전", "남동발전"],
    "한국중부발전": ["한국중부발전", "중부발전"],
    "한국서부발전": ["한국서부발전", "서부발전"],
    "한국남부발전": ["한국남부발전", "남부발전"],
    "한국동서발전": ["한국동서발전", "동서발전"],
    "한국전력기술": ["한국전력기술", "한전기술"],
    "한전KPS": ["한전KPS", "한전 KPS", "KEPCO KPS", "KPS"],
    "한전KDN": ["한전KDN", "한전 KDN", "KEPCO KDN", "KDN"],
    "한전원자력연료": ["한전원자력연료", "한전원자력 연료"],
}
INSTITUTIONS = {**GOVERNMENT_BODIES, **PUBLIC_INSTITUTIONS, **FINANCIAL_INSTITUTIONS}
# Keep existing dashboard identities and article matches for the power companies.
for _name, _aliases in COMPANIES.items():
    _official = next((key for key in INSTITUTIONS if key in _aliases
                     or key == _name + '공사'), _name)
    _names = INSTITUTIONS.pop(_official, [])
    INSTITUTIONS[_name] = list(dict.fromkeys([*_aliases, *_names]))

TOPICS = {
    "공격 유형": "개인정보 | 유출 | 노출 | 정보유출 | 정보 유출 | 해킹 | 침해사고 | 사이버공격 | 사이버 공격 | 악성코드 | 랜섬웨어 | APT | DDoS | 사이버테러 | 피싱 | 스미싱 | 악성메일 | 보안사고 | 해킹 피해 | 정보탈취 | 계정탈취 | 데이터 유출 | 개인정보 침해 | 개인정보 유출사고".split(" | "),
    "개인정보위·정부 대응": "개인정보보호위원회 | 개인정보위 | 과징금 | 과태료 | 시정명령 | 시정권고 | 개선권고 | 유출신고 | 유출통지 | 개인정보보호법 | 개인정보보호법 위반 | 행정처분 | 행정제재 | 조사 착수 | 현장조사 | 수사의뢰 | 형사고발 | 시정조치 | 개선조치 | 안전조치".split(" | "),
}

FINANCIAL_TOPICS = {
    '공격 유형': list(dict.fromkeys([*TOPICS['공격 유형'], '신용정보', '카드번호',
                                  'CVC', '고객정보', '무단 접근', '디도스'])),
    '개인정보위·정부 대응': list(dict.fromkeys([*TOPICS['개인정보위·정부 대응'],
        '금융감독원', '금감원', '금융보안원', '금융위원회', '금융위',
        '긴급 점검', '긴급점검', '현장검사', '업무정지', '재발급', '피해 보상'])),
}

FINANCIAL_FILTER = '금융권 전체'


def financial_keyword_names(entries):
    return [entry['name'] for entry in entries if entry.get('enabled')
            and entry.get('profile') == 'company_topic'
            and entry.get('company') in FINANCIAL_INSTITUTIONS]


def register_financial_institutions(config):
    """Migrate finance independently, preserving deleted government entries."""
    if config.get('financial_catalog_version', 0) >= FINANCIAL_CATALOG_VERSION:
        return False
    existing = {(entry.get('company'), entry.get('category'))
                for entry in config['keywords'] if entry.get('profile') == 'company_topic'}
    for name, aliases in FINANCIAL_INSTITUTIONS.items():
        for entry in make_entries(name, aliases, FINANCIAL_TOPICS):
            if (name, entry['category']) not in existing:
                config['keywords'].append(entry)
    config['financial_catalog_version'] = FINANCIAL_CATALOG_VERSION
    return True

def phrases(text):
    # Spaces inside a phrase are retained, including '정보 유출' and 'KEPCO KPS'.
    return list(dict.fromkeys(x.strip().strip('"').strip() for x in re.split(r'\s+OR\s+|[|,\n\r]', text, flags=re.I) if x.strip().strip('"').strip()))

def validate_terms(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        raise RuleError("검색어는 1~200개까지 등록할 수 있습니다.")
    if any(not isinstance(x, str) or not x.strip() or len(x) > 80 or '"' in x or any(ord(c) < 32 for c in x) for x in values):
        raise RuleError("각 검색어는 큰따옴표·줄바꿈 없이 1~80자로 입력해 주세요.")
    return list(dict.fromkeys(x.strip() for x in values))

def make_entries(company, aliases, topics, exclude="", scope="title_description"):
    if not company.strip() or len(company) > 50 or any(ord(c) < 32 for c in company):
        raise RuleError("기관 이름을 1~50자로 입력해 주세요.")
    if not topics:
        raise RuleError("모니터링 분류를 하나 이상 선택해 주세요.")
    for category in topics:
        if (not isinstance(category, str) or not category.strip() or len(category) > 50
                or len(f"{company.strip()} · {category}") > 80
                or any(ord(c) < 32 for c in category)):
            raise RuleError("분류 이름은 1~50자, 기관과 합친 표시 이름은 80자 이내로 입력해 주세요.")
    return [{"name": f"{company.strip()} · {category}", "enabled": True,
             "profile": "company_topic", "company": company.strip(),
             "aliases": validate_terms(aliases), "category": category,
             "terms": validate_terms(terms), "exclude": exclude, "scope": scope}
            for category, terms in topics.items()]

def parse_template(text):
    sections = {}
    current = None
    for line in text.lstrip('\ufeff').splitlines():
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1]
            sections[current] = []
        elif current and line and not set(line) <= {'='}:
            sections[current].append(line)
    section = next((key for key in ('기관 키워드', '발전그룹사 키워드') if key in sections), None)
    if section is None or any(key not in sections for key in TOPICS):
        raise RuleError("[기관 키워드], [공격 유형], [개인정보위·정부 대응] 항목이 필요합니다.")
    # The catalog is larger than the per-institution alias limit.
    aliases = phrases('\n'.join(sections[section]))
    for alias in aliases:
        validate_terms([alias])
    companies = {}
    for alias in aliases:
        company = next((name for name, names in INSTITUTIONS.items() if normalize(alias) in [normalize(x) for x in names]), alias)
        companies.setdefault(company, []).append(alias)
    topics = {key: validate_terms(phrases('\n'.join(sections[key]))) for key in TOPICS}
    return companies, topics


def register_initial_institutions(config):
    """Add missing catalog institutions once; keep user edits and later deletions."""
    if config.get('institution_catalog_version', 0) >= CATALOG_VERSION:
        return False
    existing = {entry['company'] for entry in config['keywords']
                if entry.get('profile') == 'company_topic'}
    entries = list(config['keywords'])
    for name, aliases in INSTITUTIONS.items():
        if name not in existing:
            entries.extend(make_entries(name, aliases,
                                       FINANCIAL_TOPICS if name in FINANCIAL_INSTITUTIONS else TOPICS))
    config['keywords'] = entries
    config['institution_catalog_version'] = CATALOG_VERSION
    return True
