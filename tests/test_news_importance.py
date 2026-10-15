import unittest

from news_importance import MIN_SHARE_SCORE, importance, share_priority


class ImportanceTests(unittest.TestCase):
    def test_incidents_sanctions_and_outages_do_not_need_multiple_publishers(self):
        for title in ('식약처 개인정보 1195명 유출…사과',
                      '금융위 금융사 해킹 피해 조사 착수',
                      '개인정보위 과징금 100억원 부과',
                      '은행 결제 서비스 장애 발생', '대규모 정전 발생'):
            with self.subTest(title=title):
                self.assertGreaterEqual(importance({'title': title})[0], MIN_SHARE_SCORE)

    def test_promotion_and_speculation_do_not_qualify_even_with_many_sources(self):
        for title in ('남부발전 사이버 공격 방어 대회 개최', '해킹 예방 보안 교육',
                      '지역 문화 행사 개최', '은행 개인정보 유출 가능성 우려',
                      '금융권 개인정보 유출, 어디까지 조사하나', '서비스 장애 대비 훈련',
                      '한국전력 개인정보 유출 없어', '보안 솔루션 출시'):
            with self.subTest(title=title):
                self.assertLess(share_priority({'title': title, 'related_count': 100})[0], MIN_SHARE_SCORE)

    def test_substantive_group_member_supplies_evidence(self):
        score, reason = share_priority({'title': '은행 후속 대응', 'related_articles': [
            {'title': '은행 고객정보 1만명 유출 확인'}]})
        self.assertGreaterEqual(score, MIN_SHARE_SCORE)
        self.assertIn('사고', reason)
