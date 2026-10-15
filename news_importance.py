"""Explainable automatic sharing gate; collected articles remain on screen."""
import re

from incident_filter import incident_relevant


MIN_SHARE_SCORE = 60
_PROMOTION = re.compile(
    r'채용|모집|인사\s*발령|업무\s*협약|공모전|해커톤|봉사|기부|'
    r'세미나|컨퍼런스|콘퍼런스|박람회|포럼|캠페인|출시|신제품|'
    r'(?:방어|해킹|보안|공격)\s*대회|보안\s*(?:교육|훈련)|모의\s*(?:해킹|공격)')
_COMMENTARY = re.compile(r'사설|칼럼|기고|출근길|어디까지|전망|가능성|우려')
_CONFIRMED = re.compile(r'부과|처분|명령|확정|의결|적발|착수|발표|시행|통과')
_REGULATORY = re.compile(r'과징금|과태료|시정명령|업무정지|행정제재|행정처분|법안|법률|개정안')
_OUTAGE = re.compile(r'(?:서비스|전산|결제|접속|거래|발전소|전력).{0,12}(?:장애|중단|마비|정지)|정전')
_PREVENTIVE = re.compile(r'예방|대비|훈련|모의|가정|우려|가능성')


def importance(article):
    """Return score and reason using headline evidence, never publisher count alone."""
    title = article.get('title', '')
    if _PROMOTION.search(title):
        return 10, '행사·홍보·채용성 기사'
    if _COMMENTARY.search(title):
        return 25, '해설·전망 위주 기사'
    if _REGULATORY.search(title) and _CONFIRMED.search(title):
        return 85, '제재·조사 또는 법령의 확정 조치'
    if _OUTAGE.search(title) and not _PREVENTIVE.search(title):
        return 90, '서비스·기반시설 장애'
    # RSS snippets often repeat an old breach underneath an unrelated headline.
    if incident_relevant(title):
        return 80, '보안 사고·피해 관련 보도'
    return 30, '중요 사건·확정 조치 근거 부족'


def share_priority(article):
    # A substantive member can supply the evidence missing from a vague headline.
    return max((importance(member) for member in
                [article, *article.get('related_articles', [])]), key=lambda result: result[0])
