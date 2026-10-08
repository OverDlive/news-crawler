"""Conservative relevance gate for the built-in security monitoring categories."""
import re

from search_rules import contains_term, normalize


SECURITY_CATEGORIES = frozenset(('공격 유형', '개인정보위·정부 대응'))

# These headlines describe hiring, promotion or legal proceedings, even when
# their RSS snippets mention a historical breach or the security industry.
OFF_TOPIC = re.compile(
    r'채용|공개\s*모집|인재\s*모집|신입|경력직|인턴|취업|'
    r'교육생|수강생|교육\s*프로그램|교육\s*과정|양성\s*과정|'
    r'모집|공모전|해커톤|업무\s*협약|'
    r'과징금.{0,30}(?:소송|취소|불복)|(?:소송|취소|불복).{0,30}과징금|'
    r'행정\s*소송|손해\s*배상\s*(?:소송|청구)|소송|항소|상고|판결'
)
PROMOTION = re.compile(
    r'세미나|컨퍼런스|콘퍼런스|박람회|포럼|캠페인|출시|신제품|'
    r'(?:해킹|해커|보안).{0,20}대회|통제\s*체계\s*구축|프레임워크\s*구축|'
    r'암호.{0,20}구축|'
    r'솔루션\s*(?:공개|소개|발표)|보안\s*(?:교육|훈련)|모의\s*(?:해킹|공격)|'
    r'(?:해킹|사이버\s*공격|랜섬웨어)\s*(?:예방|대비|방지|교육|훈련)|'
    r'보안\s*(?:강화|인증)|프로그램.{0,15}(?:모집|운영|지원|개최)'
)
CYBER_TERMS = (
    '해킹', '랜섬웨어', '악성코드', '사이버공격', '사이버 공격',
    '사이버테러', '침해사고', '침해 사고', '정보탈취', '정보 탈취',
    '계정탈취', '계정 탈취', '악성메일', '악성 메일', 'DDoS', '디도스',
    'APT', '피싱', '스미싱', '무단 침입', '불법 접속', '무단 접근',
)
LEAK = re.compile(r'유출|노출|탈취|유출신고|유출통지|털렸|털린')
PERSONAL_DATA = re.compile(
    r'개인\s*정보|고객\s*정보|회원\s*정보|직원\s*정보|'
    r'주민\s*등록\s*번호|전화\s*번호|이메일\s*주소|계정\s*정보|'
    r'비밀\s*번호|신용\s*정보|카드\s*번호|계좌\s*정보|결제\s*정보|'
    r'(?<![a-z])cvc(?![a-z])|데이터\s*베이스|데이터|db(?![a-z])'
)
NEGATED_CYBER = re.compile(
    r'(?:해킹(?:\s*공격)?|사이버\s*공격|랜섬웨어|침해\s*사고|디도스|ddos)'
    r'(?:과는|과|와는|와|은|는|이|가)?\s*(?:무관|아니|없|아닌)'
)
DENIED_LEAK = re.compile(
    r'(?:유출|노출)(?:된\s*정보|\s*사실|\s*정황)?(?:은|는|이|가)?\s*'
    r'(?:없|아니|않|안\s*돼|안\s*되)|(?:유출|노출)\s*(?:방지|예방)'
)


def incident_relevant(title, description=''):
    title = normalize(title)
    description = normalize(description)
    if OFF_TOPIC.search(title) or PROMOTION.search(title):
        return False

    def leak_evidence(text):
        affirmative = DENIED_LEAK.sub('', text)
        # CI is the identity-linkage identifier; numeric victim counts also
        # establish the meaning of abbreviated "information leak" headlines.
        target = PERSONAL_DATA.search(affirmative) or re.search(
            r'(?<![a-z])ci(?![a-z])|(?:\d+[\d,.]*\s*(?:만|천)?\s*명|직원).{0,15}정보', affirmative)
        return bool(LEAK.search(affirmative) and target)

    if NEGATED_CYBER.search(title) and not leak_evidence(title):
        return False
    if re.search(r'(?:유출|노출)\s*(?:우려|위험|가능성)|해킹\s*예상', title):
        return False
    if re.search(r'(?:대응.{0,12}회의|회의.{0,12}(?:배제|제외|빠져)|대응.{0,12}강화)', title):
        return False

    def evidence(text):
        # Ignore explicit denials as attack evidence. Another confirmed leak
        # in the same headline can still be an actual security incident.
        affirmative = NEGATED_CYBER.sub('', text)
        cyber = any(contains_term(term, affirmative) for term in CYBER_TERMS)
        leak = leak_evidence(text)
        return cyber or leak

    if evidence(title):
        return True
    # A vague headline needs evidence in its own RSS description. Do not
    # promote political document leaks or general policy/industry stories
    # based on incidental background mentions of attacks.
    if re.search(r'문서|문건|수사\s*자료|검찰|공약|채용|프로그램|정책|예산', title):
        return False
    if OFF_TOPIC.search(description) or PROMOTION.search(description):
        return False
    return evidence(description)
