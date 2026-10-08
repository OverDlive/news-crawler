import tempfile
import unittest
from pathlib import Path

from news_core import Article, Store
from search_rules import SearchRule
from security_profiles import TOPICS, make_entries


class IncidentFilterTests(unittest.TestCase):
    def rule(self, category='공격 유형', scope='title_description'):
        return SearchRule(make_entries('법무부', ['법무부', '검찰'], TOPICS, scope=scope)[
            0 if category == '공격 유형' else 1])

    def test_unrelated_leaks_penalties_and_recruitment(self):
        cases = [
            ('한동훈 검찰 문서 유출 의혹', ''),
            ('법무부 수사 문건 유출 수사 착수', ''),
            ('법무부 과징금 취소 소송', '개인정보 유출 및 해킹 사고에 따른 처분'),
            ('법무부 과징금 불복 행정소송', ''),
            ('법무부 개인정보보호법 교육 프로그램 채용', '해킹 대응 전문가 모집'),
            ('법무부 해킹 대응 인턴 채용', ''),
            ('법무부 보안 인재 공개 모집', '랜섬웨어 사고 대응 업무'),
            ('법무부 개인정보 정책 발표', ''),
            ('법무부 개인정보 보호 강화', ''),
            ('법무부 랜섬웨어 예방 교육', ''),
            ('법무부 모의 해킹 훈련', ''),
            ('법무부 해킹 예방 솔루션 출시', ''),
            ('법무부 개인정보 유출 방지 캠페인', ''),
            ('법무부 과징금 처분', '근로기준법 위반'),
            ('법무부 개인정보 유출 사실은 없다', ''),
            ('법무부 해킹과는 무관', ''),
            ('법무부 접속 장애…금융권 해킹 공격과 무관', '금융권 해킹으로 개인정보 유출'),
            ('법무부 AI 해킹 방어 대회 개최', ''),
            ('법무부 개인정보 유출 우려', ''),
            ('법무부 해킹 대응 회의에서 제외', ''),
            ('법무부 AI 감사 통제체계 구축…데이터 유출 대응', ''),
            ('법무부 개인정보보호위원회 업무 협약', '개인정보 유출 사고 예방'),
        ]
        for category in TOPICS:
            rule = self.rule(category)
            for title, description in cases:
                with self.subTest(category=category, title=title):
                    self.assertFalse(rule.matches(title, description))

    def test_actual_incidents_and_incident_linked_response(self):
        for title in [
            '법무부 랜섬웨어 피해 확인',
            '법무부 DDoS 공격으로 서비스 중단',
            '법무부 APT 공격 조사',
            '법무부 해킹으로 검찰 문서 유출',
            '법무부 직원 개인정보 노출 사고',
            '법무부 직원 개인정보 노출…AI 해킹과는 무관',
            '법무부 직원 개인정보 노출…고객 정보 유출은 없다',
            '법무부 10만 명 정보 유출…2차 피해 예방 노력',
        ]:
            self.assertTrue(self.rule().matches(title), title)
        government = self.rule('개인정보위·정부 대응')
        self.assertTrue(government.matches('법무부 해킹 피해로 과징금 부과'))
        self.assertTrue(government.matches('법무부 개인정보 유출 조사 착수'))
        self.assertTrue(government.matches('법무부 CI 유출 현장조사'))

    def test_description_evidence_and_scope(self):
        title, description = '법무부 조사 착수', '해커가 시스템에 침입해 개인정보를 유출했다'
        self.assertTrue(self.rule('개인정보위·정부 대응').matches(title, description))
        self.assertFalse(self.rule('개인정보위·정부 대응', 'title').matches(title, description))
        self.assertFalse(self.rule().matches('법무부 일반 정책 발표', '지난해 해킹 피해 이후 정책 마련'))

    def test_custom_categories_and_plain_keywords_are_preserved(self):
        rule = SearchRule(make_entries('법무부', ['법무부'], {'인사': ['채용']})[0])
        self.assertTrue(rule.matches('법무부 채용'))
        self.assertTrue(SearchRule({'name': '채용'}).matches('법무부 채용'))

    def test_saved_results_are_filtered_before_grouping_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            self.addCleanup(store.close)
            now = 1800000000
            entries = make_entries('법무부', ['법무부', '검찰'], TOPICS)
            bad = Article('https://example.com/bad', '한동훈 검찰 문서 유출', '신문', now, '')
            good = Article('https://example.com/good', '법무부 랜섬웨어 피해', '신문', now, '')
            for entry in entries:
                store.ingest(entry['name'], [bad, good], now)
            names = [entry['name'] for entry in entries]
            for grouped in (True, False):
                rows = store.list_articles(names, now=now, grouped=grouped, rules=entries)
                self.assertEqual([row['url'] for row in rows], [good.url])
                self.assertEqual(rows[0]['keywords'], [entries[0]['name']])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 2)
            store.close()


if __name__ == '__main__':
    unittest.main()
