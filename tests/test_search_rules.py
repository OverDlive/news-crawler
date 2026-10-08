from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from news_core import Article, Store, fetch_news
from search_rules import RuleError, SearchRule


class SearchRuleTests(unittest.TestCase):
    def rule(self, condition, **kwargs):
        return SearchRule({"name": "한전 주요 소식", "condition": condition, **kwargs})

    def test_and_or_not_parentheses(self):
        rule = self.rule("한전 AND (전기 OR 요금) NOT 주가")
        self.assertTrue(rule.matches("한전 전기 공급 확대"))
        self.assertTrue(rule.matches("한전 요금 인상"))
        self.assertFalse(rule.matches("한전 주가와 요금 전망"))
        self.assertFalse(rule.matches("다른 회사 전기 요금"))
        self.assertFalse(rule.matches("한전 채용"))
        self.assertIn('("한전" "전기" -"주가")', rule.query)
        self.assertIn(' OR ', rule.query)

    def test_precedence_and_implicit_and(self):
        rule = self.rule("한전 OR 발전 AND 계약")
        self.assertTrue(rule.matches("한전 소식"))
        self.assertFalse(rule.matches("발전 소식"))
        self.assertTrue(rule.matches("발전 계약"))
        self.assertFalse(self.rule("한전 요금").matches("한전 소식"))
        self.assertTrue(self.rule("한전 요금").matches("한전 요금 인상"))

    def test_exact_phrase_case_and_exclude_field(self):
        rule = self.rule('("한국 전력" OR KEPCO) AND 계약', exclude="주가, 채용 공고")
        self.assertTrue(rule.matches("kepco 계약 체결"))
        self.assertTrue(rule.matches("한국 전력 계약"))
        self.assertFalse(rule.matches("한국의 전력 계약"))
        self.assertFalse(rule.matches("한국 전력 계약 관련 주가"))
        self.assertFalse(rule.matches("한국 전력 계약직 채용 공고"))

    def test_grouped_not_and_negative_phrase(self):
        rule = self.rule('한전 AND NOT (주가 OR 채용)')
        self.assertTrue(rule.matches("한전 전기"))
        self.assertFalse(rule.matches("한전 채용"))
        self.assertFalse(rule.matches("한전 주가"))
        self.assertFalse(self.rule('한전 -"채용 공고"').matches("한전 채용 공고"))
        self.assertTrue(self.rule("한전 AND NOT NOT 요금").matches("한전 요금"))

    def test_scope(self):
        rule = self.rule("한전 AND 요금", scope="title")
        self.assertFalse(rule.matches("한전 소식", "요금 인상"))
        self.assertTrue(self.rule("한전 AND 요금").matches("한전 소식", "요금 인상"))
        self.assertFalse(self.rule("한전 NOT 주가").matches("한전 요금", "주가 상승"))

    def test_invalid_input_and_complexity_limits(self):
        for expression in ('한전 AND', 'OR 한전', '(한전 OR 요금', '한전)', '"한전', '한전 AND ()',
                           'NOT 한전', '한전 OR NOT 요금', '한전 && 요금', '""',
                           '(' * 14 + '한전' + ')' * 14):
            with self.subTest(expression=expression), self.assertRaises(RuleError):
                self.rule(expression)
        with self.assertRaises(RuleError):
            self.rule(" AND ".join(f"(a{i} OR b{i})" for i in range(6)))
        with self.assertRaises(RuleError):
            self.rule("한전", exclude='""')

    def test_legacy_name_is_still_a_literal_phrase(self):
        rule = SearchRule({"name": "한국 전력", "enabled": True})
        self.assertEqual(rule.query, '"한국 전력"')
        self.assertFalse(rule.filter_locally)

    def test_request_uses_expression_and_filters_response(self):
        feed = '''<rss><channel>
        <item><title>한전 전기 공급</title><link>https://example.com/a</link>
        <pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate></item>
        <item><title>한전 전기 주가</title><link>https://example.com/b</link>
        <pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate></item>
        </channel></rss>'''.encode()
        with patch("news_core.urllib.request.urlopen") as connection:
            connection.return_value.__enter__.return_value.read.return_value = feed
            results = fetch_news({"name": "표시 이름", "condition": "한전 AND 전기 NOT 주가"})
            query = parse_qs(urlsplit(connection.call_args.args[0].full_url).query)["q"][0]
        self.assertNotIn("표시 이름", query)
        self.assertIn('-"주가"', query)
        self.assertIn("when:24h", query)
        self.assertEqual([article.url for article in results], ["https://example.com/a"])

    def test_advanced_settings_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                config = store.load_config()
                config["keywords"] = [{"name": "한전", "enabled": True, "condition": "한전 AND 요금",
                                       "exclude": "주가, 채용", "scope": "title"}]
                store.save_config(config)
                self.assertEqual(store.load_config(), config)
            finally:
                store.close()
