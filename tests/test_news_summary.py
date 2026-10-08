from email.message import Message
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from news_core import Article, Store
from news_summary import ArticleHTML, ArticleSummarizer, Summary, summarize


BODY = ('한국전력이 개인정보 보호 체계를 강화한다고 밝혔다. '
        '회사는 직원 대상 보안 교육을 실시하고 외부 접속 기록을 점검할 예정이다. '
        '점검 결과에 따라 추가적인 보호 대책을 마련할 계획이라고 설명했다. '
        '이번 조치는 다음 달부터 전국 사업장에 적용된다.')


def page(html):
    parser = ArticleHTML()
    parser.feed(html)
    return parser


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(url='https://publisher.example/news/1', title='한국전력 개인정보 보호 강화',
                        source='신문', description='한국전력이 개인정보 보호를 위한 직원 보안 교육을 실시한다.')
        self.summarizer = ArticleSummarizer()

    def test_structured_body_and_article_container_exclude_noise(self):
        structured = page('<script type="application/ld+json">{"@graph":[{"articleBody":"' + BODY + '"}]}</script>')
        self.assertEqual(structured.body(), BODY)
        html = '<nav>다른 뉴스</nav><article><p>' + BODY + '</p><aside>관련 기사</aside><div class="related">광고</div><script>숨김</script></article>'
        extracted = page(html).body()
        self.assertEqual(extracted, BODY)
        self.assertEqual(page('<html><p>로그인 페이지 및 다른 기사 목록</p></html>').body(), '')
        self.assertEqual(page('<div id="newsct_article">' + BODY + '</div>').body(), BODY)

    def test_summary_is_bounded_original_sentences_and_keeps_order(self):
        result = summarize(BODY, self.row['title'])
        self.assertLessEqual(len(result), 360)
        self.assertIn('한국전력이', result)
        self.assertNotIn('무단 전재', summarize(BODY + '\n무단 전재 및 재배포 금지입니다.'))
        self.assertEqual(summarize(''), '')
        self.assertLessEqual(len(summarize('긴문장' * 300)), 360)

    def test_body_success_and_rss_fallback_are_distinct(self):
        with patch.object(self.summarizer, 'read', return_value=(page('<article>' + BODY + '</article>'), self.row['url'])):
            result = self.summarizer.create(self.row)
        self.assertEqual(result.basis, 'body')
        self.assertEqual(result.content_url, self.row['url'])
        with patch.object(self.summarizer, 'read', side_effect=TimeoutError):
            result = self.summarizer.create(self.row)
            self.assertEqual(result.basis, 'rss')
            self.assertIn('RSS', result.note)
            result = self.summarizer.create({**self.row, 'description': self.row['title']})
            self.assertEqual(result.basis, 'unavailable')
            self.assertEqual(result.text, '')

    def test_google_follows_explicit_publisher_link_and_does_not_summarize_portal(self):
        google = 'https://news.google.com/rss/articles/opaque'
        portal = page('<a href="' + self.row['url'] + '">' + self.row['title'] + '</a>')
        with patch.object(self.summarizer, 'read', side_effect=[(portal, google), (page('<article>' + BODY + '</article>'), self.row['url'])]) as read:
            result = self.summarizer.create({**self.row, 'url': google})
            self.assertEqual(result.basis, 'body')
            self.assertEqual(read.call_count, 2)
        with patch.object(self.summarizer, 'read', return_value=(page('<article>' + BODY + '</article>'), google)), patch.object(self.summarizer, 'publisher_link', return_value=''):
            self.assertEqual(self.summarizer.create({**self.row, 'url': google}).basis, 'rss')

    def test_google_alternate_link_requires_same_title_source_and_date(self):
        import time
        now = time.time()
        correct = Article(self.row['url'], self.row['title'], self.row['source'], now, '')
        wrong = Article('https://other.example/a', self.row['title'], '다른 언론사', now, '')
        old = Article('https://publisher.example/old', self.row['title'], self.row['source'], now - 72 * 3600, '')
        with patch('news_collection.NewsCollector.fetch_bing', return_value=[wrong, old, correct]):
            self.assertEqual(self.summarizer.publisher_link({**self.row, 'published': now}), correct.url)
        with patch('news_collection.NewsCollector.fetch_bing', return_value=[wrong, old]):
            self.assertEqual(self.summarizer.publisher_link({**self.row, 'published': now}), '')

    def test_read_supports_korean_charset_and_caps_size(self):
        headers = Message()
        headers['Content-Type'] = 'text/html'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return 'https://publisher.example/news/1'
            def read(self, size): return self.data
        response = Response()
        response.headers = headers
        response.data = ('<meta charset="euc-kr"><article>' + BODY + '</article>').encode('euc-kr')
        with patch.object(self.summarizer.opener, 'open', return_value=response):
            self.assertEqual(self.summarizer.read(self.row['url'])[0].body(), BODY)
            response.data = b'x' * (2 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError): self.summarizer.read(self.row['url'])


class SummaryStoreTests(unittest.TestCase):
    def test_existing_database_cache_retry_and_deleted_article(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            # Existing installations have the six-column article table.
            db = sqlite3.connect(path / 'news.sqlite3')
            db.execute('CREATE TABLE articles (url TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL, published REAL NOT NULL, description TEXT NOT NULL, discovered REAL NOT NULL)')
            db.close()
            store = Store(path)
            now = 1800000000
            article = Article('https://example.com/a', '기사 제목', '신문', now, BODY)
            store.ingest('키워드', [article], now)
            self.assertEqual(len(store.summary_candidates(['키워드'], now=now)), 1)
            store.save_summary(article.url, Summary('요약', 'rss', 'RSS 설명 기준'), now)
            self.assertEqual(store.summary_candidates(['키워드'], now=now), [])
            self.assertEqual(len(store.summary_candidates(['키워드'], hours=48, now=now + 86401)), 1)
            store.save_summary(article.url, Summary('본문 요약', 'body', '본문 발췌', article.url), now)
            self.assertEqual(store.list_articles(['키워드'], now=now)[0]['summary'], '본문 요약')
            store.close()
            store = Store(path)
            self.assertEqual(store.get_summary(article.url)['summary_basis'], 'body')
            store.remove_keyword('키워드')
            store.save_summary(article.url, Summary('늦은 결과', 'body', '본문 발췌'), now)
            self.assertEqual(store.get_summary(article.url), {})
            store.close()


if __name__ == '__main__':
    unittest.main()
