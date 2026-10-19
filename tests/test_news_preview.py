import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from email.message import Message

from kakao_notifications import memo_template, send_memo
from news_preview import article_preview, _metadata, telegram_preview_url
from news_summary import ArticleHTML, ArticleSummarizer


class PreviewTests(unittest.TestCase):
    def setUp(self):
        _metadata.cache_clear()
        self.article = dict(url='https://publisher.example/news/1', title='기사 제목',
                            source='신문', description='기사 설명')

    def page(self, html):
        parser = ArticleHTML()
        parser.feed(html)
        return parser

    def test_og_image_priority_relative_urls_and_cache(self):
        page = self.page('<meta name="twitter:image" content="https://cdn.example/twitter.jpg">'
                         '<meta property="og:image" content="javascript:invalid">'
                         '<meta property="og:image" content="/images/news.jpg?a=1&amp;b=2">')
        with patch.object(ArticleSummarizer, 'read_article', return_value=(page, self.article['url'])) as read:
            result = article_preview(self.article)
            self.assertEqual(result['image_url'], 'https://publisher.example/images/news.jpg?a=1&b=2')
            self.assertEqual(article_preview(self.article), result)
            self.assertEqual(read.call_count, 1)
        self.assertNotIn('image_url', self.article)

    def test_twitter_fallback_and_missing_image_or_timeout_preserve_link(self):
        for html, expected in [('<meta name="twitter:image" content="//cdn.example/a.jpg">',
                                'https://cdn.example/a.jpg'), ('<article>이미지 없음</article>', '')]:
            _metadata.cache_clear()
            with patch.object(ArticleSummarizer, 'read_article', return_value=(self.page(html), self.article['url'])):
                result = article_preview(self.article)
                self.assertEqual(result['image_url'], expected)
                self.assertEqual(result['content_url'], self.article['url'])
        _metadata.cache_clear()
        with patch.object(ArticleSummarizer, 'read_article', side_effect=TimeoutError):
            result = article_preview(self.article)
        self.assertEqual(memo_template(result)['object_type'], 'text')
        self.assertEqual(memo_template(result)['link']['web_url'], self.article['url'])

    def test_google_preview_uses_publisher_image_and_click_destination(self):
        google = 'https://news.google.com/rss/articles/opaque'
        portal = self.page('<link rel="canonical" href="' + self.article['url'] + '">')
        publisher = self.page('<meta property="og:image" content="/news.jpg">')
        with patch.object(ArticleSummarizer, 'read', side_effect=[(portal, google), (publisher, self.article['url'])]):
            result = article_preview({**self.article, 'url': google})
        template = memo_template(result)
        self.assertEqual(template['content']['image_url'], 'https://publisher.example/news.jpg')
        self.assertEqual(template['content']['link']['web_url'], self.article['url'])
        self.assertEqual(template['buttons'][0]['link']['mobile_web_url'], self.article['url'])
        self.assertEqual(telegram_preview_url({**self.article, 'url': google}), self.article['url'])

    def test_kakao_actual_request_contains_one_clickable_feed(self):
        article = {**self.article, 'image_url': 'https://cdn.example/article.jpg'}
        with patch('kakao_notifications.urllib.request.urlopen', return_value=io.BytesIO(b'{"result_code":0}')) as request:
            self.assertEqual(send_memo('test-token', article)[0], 'sent')
        fields = parse_qs(request.call_args.args[0].data.decode())
        template = json.loads(fields['template_object'][0])
        self.assertEqual(template['object_type'], 'feed')
        self.assertEqual(template['content']['image_url'], article['image_url'])
        self.assertEqual(template['content']['link']['web_url'], article['url'])

    def test_telegram_uses_resolved_article_for_text(self):
        from news_digest import telegram_text
        result = telegram_text({**self.article, 'content_url': 'https://publisher.example/resolved'})
        self.assertIn('https://publisher.example/resolved', result)
        self.assertNotIn(self.article['url'], result)

    def test_real_google_signed_redirect_shape_reaches_both_message_templates(self):
        google = 'https://news.google.com/rss/articles/signed-article-id'
        class Response(io.BytesIO):
            def __init__(self, body, url):
                super().__init__(body.encode())
                self.url = url
                self.headers = Message()
                self.headers['Content-Type'] = 'text/html;charset=utf-8'
            def geturl(self):
                return self.url
        # Actual Google RSS landing pages contain no publisher <a> at all.
        portal = ('<meta property="og:image" content="https://google.example/generic-logo.jpg">'
                  '<link rel="canonical" href="' + google + '">'
                  '<div data-n-a-id="signed-article-id" data-n-a-ts="1792375196" '
                  'data-n-a-sg="signed-value"></div>')
        rpc = ")]}'\n\n123\n" + json.dumps([['wrb.fr', 'Fbv4je',
                                  json.dumps(['garturlres', self.article['url']])]]) + '\n'
        responses = [Response(portal, google), Response(rpc, google),
                     Response('<meta property="og:image" content="/real-news.jpg">', self.article['url'])]
        with patch('urllib.request.OpenerDirector.open', side_effect=responses) as read:
            result = article_preview({**self.article, 'url': google})
            self.assertEqual(telegram_preview_url({**self.article, 'url': google}), self.article['url'])
        request = read.call_args_list[1].args[0]
        envelope = json.loads(parse_qs(request.data.decode())['f.req'][0])
        query = json.loads(envelope[0][0][1])
        self.assertEqual(query[2:], ['signed-article-id', 1792375196, 'signed-value'])
        template = memo_template(result)
        self.assertEqual(template['object_type'], 'feed')
        self.assertEqual(template['content']['image_url'], 'https://publisher.example/real-news.jpg')
        self.assertEqual(template['content']['link']['web_url'], self.article['url'])

    def test_temporary_failure_does_not_cache_missing_preview_forever(self):
        publisher = self.page('<meta property="og:image" content="/recovered.jpg">')
        with patch.object(ArticleSummarizer, 'read_article',
                          side_effect=[TimeoutError(), (publisher, self.article['url'])]) as read:
            self.assertEqual(article_preview(self.article)['image_url'], '')
            self.assertEqual(article_preview(self.article)['image_url'],
                             'https://publisher.example/recovered.jpg')
            self.assertEqual(read.call_count, 2)

    def test_changed_google_rpc_falls_back_to_verified_publisher_search(self):
        google = 'https://news.google.com/rss/articles/opaque'
        portal = self.page('<div data-n-a-ts="123" data-n-a-sg="sig"></div>')
        publisher = self.page('<meta property="og:image" content="/news.jpg">')
        with patch.object(ArticleSummarizer, 'read', side_effect=[(portal, google), (publisher, self.article['url'])]), \
             patch.object(ArticleSummarizer, 'google_publisher_link', side_effect=ValueError), \
             patch.object(ArticleSummarizer, 'publisher_link', return_value=self.article['url']):
            self.assertEqual(article_preview({**self.article, 'url': google})['content_url'], self.article['url'])
