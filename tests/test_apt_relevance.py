import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from incident_filter import incident_relevant
from news_collection import NewsCollector
from news_core import Article, Store
from news_importance import MIN_SHARE_SCORE, importance
from search_rules import SearchRule
from security_profiles import TOPICS, make_entries


# Actual RSS headline/snippet that previously matched the housing authority.
APARTMENT_TITLE = "[보령소식]명천2차APT '도 그린홈 으뜸아파트' 현판 제막식 등"
APARTMENT_DESCRIPTION = (
    "충남 보령시는 4일 주택관리공단 보령명천2 주거행복지원센터에서 "
    "'2026년 충남도 그린홈 으뜸아파트'에 선정된 명천2차 아파트 현판 제막식을 개최했다."
)


class AptRelevanceTests(unittest.TestCase):
    def entry(self, **overrides):
        entry = make_entries('주택관리공단', ['주택관리공단'], TOPICS)[0]
        entry.update(overrides)
        return entry

    def test_apartment_and_bare_acronym_are_not_attack_evidence(self):
        cases = [
            (APARTMENT_TITLE, APARTMENT_DESCRIPTION),
            ('주택관리공단 APT', ''),
            ('주택관리공단 apt 그린홈 선정', ''),
            ('주택관리공단 ＡＰＴ 현판식', ''),
            ('주택관리공단 APT 보안 강화', ''),
            ('주택관리공단 APT 101동 침수 피해', ''),
            ('주택관리공단 APT 주차장 침입 사건', ''),
            ('주택관리공단 CAPTIVE 사업 설명', ''),
            ('주택관리공단 CAPTIVE 공격 조사', ''),
            ('주택관리공단 APT37X 공격 조사', ''),
            ('주택관리공단 APT 소식. 축구팀 공격 전술 소개', ''),
            ('주택관리공단 APT 공격 예방 교육', ''),
            ('주택관리공단 APT37 공격 대비 훈련', ''),
            ('주택관리공단 APT 공격 가능성 우려', ''),
            ('주택관리공단 APT 공격과는 무관', 'APT 공격으로 개인정보 유출'),
            ('주택관리공단 APT 공격은 없다', ''),
            ('주택관리공단 APT37과는 무관', ''),
        ]
        rule = SearchRule(self.entry())
        for title, description in cases:
            with self.subTest(title=title):
                self.assertFalse(incident_relevant(title, description))
                self.assertFalse(rule.matches(title, description))
                self.assertLess(importance({'title': title})[0], MIN_SHARE_SCORE)

    def test_actual_apt_attacks_and_apartment_data_leaks_are_retained(self):
        titles = [
            '주택관리공단 APT 공격 조사',
            '주택관리공단 apt공격 피해 확인',
            '주택관리공단 ＡＰＴ 기반 공격 피해',
            '주택관리공단 APT(지능형 지속 위협) 공격 확인',
            '주택관리공단 공격 배후는 APT 조직',
            '주택관리공단 APT 조직이 내부망에 침투',
            '주택관리공단 APT 침투 확인',
            '주택관리공단 APT37 표적 피해 확인',
            '주택관리공단 APT-37 소행 확인',
            '주택관리공단 APT 37 공격으로 전산망 침해',
            '주택관리공단 APT 공격 피해 확인…재발 예방 추진',
            '주택관리공단 아파트 입주민 개인정보 유출',
            '주택관리공단 그린홈 으뜸아파트 입주민 개인정보 유출',
            '주택관리공단 APT 공격과는 무관…입주민 개인정보 유출 확인',
            '주택관리공단 랜섬웨어 감염…APT37 공격과는 무관',
        ]
        rule = SearchRule(self.entry())
        for title in titles:
            with self.subTest(title=title):
                self.assertTrue(incident_relevant(title))
                self.assertTrue(rule.matches(title))
                self.assertGreaterEqual(importance({'title': title})[0], MIN_SHARE_SCORE)

    def test_confirmed_leak_survives_speculation_about_apt_attribution(self):
        title = '주택관리공단 개인정보 유출 확인…APT 공격 가능성 조사'
        self.assertTrue(incident_relevant(title))
        self.assertTrue(SearchRule(self.entry()).matches(title))

    def test_rss_description_and_title_scope(self):
        title, description = '주택관리공단 피해 조사', 'APT37 공격으로 내부망 침해가 확인됐다.'
        self.assertTrue(SearchRule(self.entry()).matches(title, description))
        self.assertFalse(SearchRule(self.entry(scope='title')).matches(title, description))
        government = SearchRule(make_entries('주택관리공단', ['주택관리공단'], TOPICS)[1])
        self.assertTrue(government.matches('주택관리공단 조사 착수', description))
        self.assertFalse(government.matches('주택관리공단 조사 착수', 'APT 현판식 관련 조사'))
        self.assertTrue(government.matches('주택관리공단 조사 착수',
                                          '개인정보 유출 확인…APT 공격과는 무관'))

    def test_numbered_groups_respect_disabled_terms_exclusions_and_evidence(self):
        title = '주택관리공단 APT37 공격 피해 확인'
        rule = SearchRule(self.entry())
        self.assertIn('검색어: APT', rule.evidence(title))
        self.assertFalse(SearchRule(self.entry(disabled_terms=['APT'])).matches(title))
        self.assertFalse(SearchRule(self.entry(exclude='APT37')).matches(title))

    def test_plain_search_and_custom_categories_keep_literal_semantics(self):
        for entry in ({'name': 'APT', 'condition': 'APT'},
                      make_entries('주택관리공단', ['주택관리공단'], {'주거': ['APT']})[0]):
            rule = SearchRule(entry)
            self.assertTrue(rule.matches('주택관리공단 APT 현판식'))
            self.assertFalse(rule.matches('주택관리공단 APT37 공격'))

    def test_google_and_bing_apply_same_relevance_gate(self):
        now = time.time()
        articles = [
            Article('https://example.com/housing', APARTMENT_TITLE, '신문', now, APARTMENT_DESCRIPTION),
            Article('https://example.com/attack', '주택관리공단 APT37 공격 피해 확인', '신문', now, ''),
            Article('https://example.com/leak', '주택관리공단 아파트 개인정보 유출', '신문', now, ''),
        ]
        entry = self.entry()
        for provider in ('google', 'bing'):
            with self.subTest(provider=provider):
                client = NewsCollector(fallback=True, google_request_interval=0)
                with patch.object(client, 'fetch', return_value=articles,
                                  side_effect=TimeoutError() if provider == 'bing' else None), \
                        patch.object(client, 'fetch_bing', return_value=articles):
                    report = client.collect([entry])
                self.assertFalse(report.errors)
                self.assertEqual(report.provider, provider)
                self.assertEqual({article.url for article in dict(report.results)[entry['name']]},
                                 {'https://example.com/attack', 'https://example.com/leak'})

    def test_saved_apartment_article_is_hidden_without_deleting_original(self):
        now = time.time()
        entry = self.entry()
        articles = [
            Article('https://example.com/housing', APARTMENT_TITLE, '신문', now, APARTMENT_DESCRIPTION),
            Article('https://example.com/attack', '주택관리공단 APT 공격 피해 확인', '신문', now, ''),
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                store.ingest(entry['name'], articles, now)
                for grouped in (False, True):
                    rows = store.list_articles([entry['name']], now=now, grouped=grouped, rules=[entry])
                    self.assertEqual([row['url'] for row in rows], ['https://example.com/attack'])
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 2)
            finally:
                store.close()
