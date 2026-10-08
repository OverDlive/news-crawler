import json
from pathlib import Path
import tempfile
import unittest

from news_core import Article, Store, parse_feed, plain_text, valid_link


class FeedTests(unittest.TestCase):
    def test_bing_namespaced_source_and_publisher_link_are_parsed_and_stable(self):
        data = '''<rss xmlns:news="https://www.bing.com/news"><channel><item>
        <title>한전 개인정보 노출</title><news:Source>연합뉴스</news:Source>
        <link>https://www.bing.com/news/apiclick.aspx?tid=changing&amp;url=https%3A%2F%2Fwww.yna.co.kr%2Fview%2Farticle%3Fpage%3D1</link>
        <pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate><description>직원 정보 노출</description>
        </item></channel></rss>'''.encode()
        article = parse_feed(data)[0]
        self.assertEqual(article.source, '연합뉴스')
        self.assertEqual(article.url, 'https://www.yna.co.kr/view/article?page=1')
        self.assertEqual(article.description, '직원 정보 노출')
        self.assertEqual(parse_feed(data.replace(b'changing', b'new-id'))[0].url, article.url)

    def test_related_headlines_cannot_supply_primary_article_keywords(self):
        data = '''<rss><channel><item><title>한전 일반 계약 - 신문</title>
        <source>신문</source><link>https://example.com/main</link>
        <pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate>
        <description><![CDATA[<ol><li><a href="https://example.com/main">한전 일반 계약</a> 신문</li>
        <li><a href="https://example.com/other">다른 기업 개인정보 해킹</a> 다른신문</li></ol>]]></description>
        </item></channel></rss>'''.encode()
        from security_profiles import make_entries, COMPANIES, TOPICS
        from search_rules import SearchRule
        article = parse_feed(data)[0]
        self.assertNotIn('해킹', article.description)
        rule = SearchRule(make_entries('한국전력', COMPANIES['한국전력'], TOPICS)[0])
        self.assertFalse(rule.accepts(article))

    def test_parse_korean_feed_and_skip_invalid_entries(self):
        data = '''<rss><channel>
          <item><title>반도체 새 소식 - 한국신문</title>
            <link>https://news.google.com/rss/articles/abc?oc=5</link>
            <source>한국신문</source><pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate>
            <description>&lt;b&gt;수출 증가&lt;/b&gt; &amp; 성장</description></item>
          <item><title>잘못된 링크</title><link>javascript:alert(1)</link>
            <pubDate>Sun, 04 Oct 2026 03:00:00 GMT</pubDate></item>
          <item><title>날짜 없음</title><link>https://example.com/a</link></item>
        </channel></rss>'''.encode()
        articles = parse_feed(data)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "반도체 새 소식")
        self.assertEqual(articles[0].description, "수출 증가 & 성장")
        self.assertEqual(articles[0].url, "https://news.google.com/rss/articles/abc")

    def test_unsafe_links_and_html(self):
        self.assertFalse(valid_link("file:///C:/secret"))
        self.assertFalse(valid_link("https://["))
        self.assertEqual(plain_text("<script>secret</script><b>기사</b> &amp; 뉴스"), "기사 & 뉴스")
        with self.assertRaises(ValueError):
            parse_feed(b"<html><body>error</body></html>")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.now = 1_800_000_000

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def article(self, name="a", hours=1):
        return Article(f"https://example.com/{name}", "기사 " + name, "신문", self.now - hours * 3600, "요약")

    def test_dedup_across_keywords_and_preserve_discovery(self):
        article = self.article()
        self.assertEqual(self.store.ingest("반도체", [article, article], self.now), 1)
        self.assertEqual(self.store.ingest("인공지능", [article], self.now + 10), 0)
        rows = self.store.list_articles(["반도체", "인공지능"], now=self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["keywords"]), {"반도체", "인공지능"})
        self.assertEqual(rows[0]["discovered"], self.now)
        self.assertEqual(self.store.latest_success(["인공지능"]), self.now + 10)

    def test_time_window_and_disabled_keyword(self):
        self.store.ingest("반도체", [self.article(), self.article("old", 25)], self.now)
        self.store.ingest("경쟁사", [self.article("competitor")], self.now)
        self.assertEqual(len(self.store.list_articles(["반도체"], now=self.now)), 1)
        self.assertEqual(self.store.list_articles([], now=self.now), [])
        self.store.ingest("반도체", [self.article("future", -2)], self.now)
        self.assertEqual(len(self.store.list_articles(["반도체"], now=self.now)), 1)

    def test_delete_keeps_shared_articles(self):
        article = self.article()
        self.store.ingest("a", [article], self.now)
        self.store.ingest("b", [article], self.now)
        self.store.remove_keyword("a")
        self.assertEqual(len(self.store.list_articles(["b"], now=self.now)), 1)
        self.assertEqual(self.store.list_articles(["a"], now=self.now), [])
        self.store.remove_keyword("b")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 0)

    def test_config_restart_and_corruption_backup(self):
        config = self.store.load_config()
        config["keywords"] = [{"name": "반도체", "enabled": True}]
        self.store.save_config(config)
        self.assertEqual(self.store.load_config(), config)
        self.store.config_path.write_text('{bad json', encoding="utf-8")
        self.assertEqual(self.store.load_config()["keywords"], [])
        self.assertTrue(list(self.store.directory.glob("settings.invalid-*.json")))

    def test_invalid_config_values_do_not_crash_startup(self):
        self.store.config_path.write_text(json.dumps({"keywords": None}), encoding="utf-8")
        self.assertEqual(self.store.load_config()["interval_minutes"], 10)

    def test_short_collection_interval_is_migrated(self):
        self.store.config_path.write_text(json.dumps({"keywords": [], "interval_minutes": 1}), encoding="utf-8")
        self.assertEqual(self.store.load_config()["interval_minutes"], 10)

    def test_theme_migration_and_restart(self):
        self.store.config_path.write_text(json.dumps({"keywords": []}), encoding="utf-8")
        self.assertEqual(self.store.load_config()["theme"], "system")
        for mode in ("light", "dark", "system"):
            config = self.store.load_config()
            config["theme"] = mode
            self.store.save_config(config)
            self.assertEqual(self.store.load_config()["theme"], mode)
        self.store.config_path.write_text(json.dumps({"keywords": [], "theme": "unknown"}), encoding="utf-8")
        self.assertEqual(self.store.load_config()["theme"], "system")


if __name__ == "__main__":
    unittest.main()
