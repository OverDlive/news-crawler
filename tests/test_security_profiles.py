import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from news_core import Store, fetch_news
from search_rules import SearchRule, RuleError
from security_profiles import COMPANIES, TOPICS, make_entries, parse_template, phrases


class SecurityProfileTests(unittest.TestCase):
    def test_parent_company_does_not_match_subsidiary_name(self):
        rule = SearchRule(make_entries('한국전력', COMPANIES['한국전력'], TOPICS)[0])
        for title in ('한전KDN 개인정보 유출', '한전KPS 해킹', '한국전력기술 랜섬웨어', '대한전기협회 개인정보'):
            self.assertFalse(rule.matches(title), title)
        self.assertTrue(rule.matches('한전은 개인정보 유출을 확인했다'))
        self.assertTrue(rule.matches('한국전력공사 직원 개인정보 노출'))
        self.assertTrue(rule.matches('한전KDN과 한전 직원의 개인정보 유출'))

    def test_latin_security_acronyms_do_not_match_unrelated_words(self):
        rule = SearchRule(make_entries('한국전력', COMPANIES['한국전력'], {'공격 유형': ['APT', 'DDoS']})[0])
        self.assertFalse(rule.matches('한전 CAPTIVE 사업 설명'))
        self.assertTrue(rule.matches('한전 APT 공격'))
        self.assertTrue(rule.matches('한전 DDoS공격'))

    def test_web_verified_current_headlines_match_correct_company(self):
        titles = [
            '한전 직원 2만4천여명 개인정보 노출…"AI 해킹과는 무관"(종합)',
            '[단독] 한국전력 전 직원 개인정보 노출...공조조사 진행 중',
            '한전 직원 2만4000명 개인정보 노출 사고…"고객 정보는 유출 안 돼"',
        ]
        entries = [entry for company, aliases in COMPANIES.items() for entry in make_entries(company, aliases, TOPICS)]
        for title in titles:
            hits = [entry['name'] for entry in entries if SearchRule(entry).matches(title)]
            self.assertEqual(hits, ['한국전력 · 공격 유형'])

    def test_all_presets_match_each_alias_and_each_topic(self):
        entries = [entry for company, aliases in COMPANIES.items() for entry in make_entries(company, aliases, TOPICS)]
        self.assertEqual(len(entries), 22)
        for entry in entries:
            rule = SearchRule(entry)
            for alias in entry['aliases']:
                for term in entry['terms']:
                    self.assertTrue(rule.matches(alias + ' ' + term))
            self.assertFalse(rule.matches(entry['aliases'][0] + ' 일반 계약 체결'))
            self.assertFalse(rule.matches('다른 기업 랜섬웨어 과징금'))
            self.assertEqual(len(rule.queries), 3)

    def test_template_roundtrip_and_multilingual_phrases(self):
        text = '[발전그룹사 키워드]\n' + '\nOR '.join('"' + alias + '"' for names in COMPANIES.values() for alias in names)
        for key, terms in TOPICS.items():
            text += '\n====\n[' + key + ']\n' + ' | '.join(terms)
        companies, topics = parse_template(text)
        self.assertEqual(companies, COMPANIES)
        self.assertEqual(topics, TOPICS)
        self.assertEqual(phrases('KEPCO KPS | 정보 유출, 개인정보 침해'), ['KEPCO KPS', '정보 유출', '개인정보 침해'])
        with self.assertRaises(RuleError):
            parse_template('[발전그룹사 키워드]\n한전')

    def test_scope_exclusion_and_independent_topics(self):
        attack, government = make_entries('한국전력', COMPANIES['한국전력'], TOPICS, '채용, 주가', 'title')
        self.assertTrue(SearchRule(attack).matches('한전 해킹'))
        self.assertFalse(SearchRule(attack).matches('한전 과징금'))
        self.assertTrue(SearchRule(government).matches('한국전력 과징금'))
        self.assertFalse(SearchRule(attack).matches('한전', '해킹'))
        self.assertFalse(SearchRule(attack).matches('한전 해킹 주가'))
        with self.assertRaises(RuleError):
            make_entries('한전', ['한전'], {})

    def test_company_candidate_requests_filter_and_deduplicate(self):
        rule = make_entries('한국전력', COMPANIES['한국전력'], TOPICS)[0]
        feed = '''<rss><channel>
        <item><title>한전 랜섬웨어 피해</title><link>https://example.com/a</link><pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate></item>
        <item><title>다른 기업 랜섬웨어 피해</title><link>https://example.com/b</link><pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate></item>
        <item><title>한전 일반 계약</title><link>https://example.com/c</link><pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate></item>
        </channel></rss>'''.encode()
        with patch('news_core.urllib.request.urlopen') as connection:
            connection.return_value.__enter__.return_value.read.return_value = feed
            results = fetch_news(rule)
            queries = [parse_qs(urlsplit(call.args[0].full_url).query)['q'][0] for call in connection.call_args_list]
        self.assertEqual(len(queries), 3)
        self.assertEqual(set(queries), {f'("{alias}") when:24h' for alias in COMPANIES['한국전력']})
        self.assertEqual([x.url for x in results], ['https://example.com/a'])

    def test_profiles_restore_all_metadata_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                config = store.load_config()
                config['keywords'] = [entry for company, aliases in COMPANIES.items() for entry in make_entries(company, aliases, TOPICS)]
                store.save_config(config)
                self.assertEqual(store.load_config(), config)
            finally:
                store.close()

    def test_custom_category_and_disabled_terms_survive_restart(self):
        entry = make_entries('한국전력', ['한전'], {'안전 사고': ['화재', '산업재해']})[0]
        entry['disabled_terms'] = ['화재']
        rule = SearchRule(entry)
        self.assertFalse(rule.matches('한전 화재'))
        self.assertTrue(rule.matches('한전 산업재해'))
        self.assertTrue(all('화재' not in query for query in rule.queries))
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                config = store.load_config()
                config['keywords'] = [entry]
                store.save_config(config)
                self.assertEqual(store.load_config(), config)
            finally:
                store.close()
        entry['disabled_terms'] = entry['terms'][:]
        with self.assertRaises(RuleError):
            SearchRule(entry)
        with self.assertRaises(RuleError):
            make_entries('한전', ['한전'], {'': ['화재']})
