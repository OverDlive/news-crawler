"""Publisher-body extraction and local, extractive summaries (no API key)."""
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
import time
import urllib.parse
import urllib.request

from news_core import RSS_HEADERS, plain_text, valid_link


@dataclass(frozen=True)
class Summary:
    text: str
    basis: str
    note: str
    content_url: str = ''
    body: str = ''


class ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.blocks = []
        self.structured = []
        self.script = None
        self.links = []
        self.targets = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        marker = ' '.join((attrs.get('id', ''), attrs.get('class', ''), attrs.get('itemprop', ''))).lower()
        hidden = tag in ('style', 'script', 'nav', 'footer', 'header', 'aside', 'button', 'noscript')
        hidden = hidden or bool(re.search(r'comment|related|recommend|advert|copyright', marker))
        body = tag == 'article' or bool(re.search(r'article.?body|article.?content|news.?body|news.?content|story.?body|articleview|newsct_article|dic_area|article_txt|view_con', marker))
        if tag not in ('meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'wbr', 'area', 'base', 'embed', 'param', 'track', 'col'):
            self.stack.append((tag, hidden, body))
        if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
            self.script = []
        if tag == 'meta' and attrs.get('property', '').lower() == 'og:url':
            self.targets.append(attrs.get('content', ''))
        if tag == 'link' and attrs.get('rel', '').lower() == 'canonical':
            self.targets.append(attrs.get('href', ''))
        if tag == 'a':
            self.anchor = [attrs.get('href', ''), []]
        if tag in ('p', 'div', 'br') and any(b for _, _, b in self.stack):
            self.blocks.append('\n')

    def handle_data(self, value):
        if self.script is not None:
            self.script.append(value)
        if self.anchor is not None:
            self.anchor[1].append(value)
        if any(h for _, h, _ in self.stack):
            return
        if any(b for _, _, b in self.stack):
            self.blocks.append(value)

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            try:
                self.structured.append(json.loads(''.join(self.script)))
            except (ValueError, RecursionError):
                pass
            self.script = None
        if tag == 'a' and self.anchor is not None:
            self.links.append((self.anchor[0], ' '.join(''.join(self.anchor[1]).split())))
            self.anchor = None
        if tag in ('p', 'div', 'article'):
            self.blocks.append('\n')
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def body(self):
        def bodies(value):
            if isinstance(value, dict):
                if isinstance(value.get('articleBody'), str):
                    yield plain_text(value['articleBody'])
                for child in value.values():
                    yield from bodies(child)
            elif isinstance(value, list):
                for child in value:
                    yield from bodies(child)
        candidates = [body for data in self.structured for body in bodies(data)]
        candidates.append('\n'.join(line for part in ''.join(self.blocks).splitlines()
                                    if (line := ' '.join(part.split()))))
        return max(candidates, key=len, default='')[:60000]


def summarize(text, title='', limit=360):
    """Select original sentences, keeping their order and never inventing facts."""
    sentences = re.split(r'(?<=[.!?。])\s+|\n+', text.strip())
    sentences = list(dict.fromkeys(' '.join(s.split()) for s in sentences if len(s.strip()) >= 15))
    sentences = [s for s in sentences if not re.search(r'무단\s*(전재|전재 및)|재배포\s*금지|구독|로그인|ⓒ|저작권', s)]
    if not sentences:
        return ''
    terms = set(re.findall(r'[가-힣A-Za-z0-9]{2,}', title))
    ranked = sorted(range(len(sentences)), key=lambda i: (
        (3 if i == 0 else 1 / (i + 1)) + sum(term in sentences[i] for term in terms)), reverse=True)
    chosen = sorted(ranked[:3])
    result = ''
    for index in chosen:
        sentence = sentences[index]
        available = limit - len(result) - (1 if result else 0)
        if available < 25:
            break
        if len(sentence) > available:
            if result:
                continue
            sentence = sentence[:available - 1].rstrip() + '…'
        result += (' ' if result else '') + sentence
    return result


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not valid_link(newurl):
            raise ValueError('지원하지 않는 기사 링크')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ArticleSummarizer:
    def __init__(self, timeout=10):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(SafeRedirect())

    def read(self, url):
        if not valid_link(url):
            raise ValueError('지원하지 않는 기사 링크')
        headers = {**RSS_HEADERS, 'Accept': 'text/html,application/xhtml+xml'}
        with self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('기사 응답 크기 초과')
            if response.headers.get_content_type() not in ('text/html', 'application/xhtml+xml'):
                raise ValueError('HTML 기사 응답이 아닙니다')
            charset = response.headers.get_content_charset()
            if not charset:
                match = re.search(br'charset\s*=\s*[\"\x27]?([\w-]+)', data[:8192], re.I)
                charset = match[1].decode('ascii') if match else 'utf-8'
            parser = ArticleHTML()
            parser.feed(data.decode(charset, errors='replace'))
            return parser, response.geturl()

    def create(self, row):
        note = '원문에서 기사 본문을 추출하지 못했습니다.'
        try:
            try:
                parser, url = self.read(row['url'])
            except Exception:
                if urllib.parse.urlsplit(row['url']).hostname != 'news.google.com':
                    raise
                target = self.publisher_link(row)
                if not target:
                    raise
                parser, url = self.read(target)
            if urllib.parse.urlsplit(url).hostname == 'news.google.com':
                # Follow only explicit publisher links; never summarize Google's UI.
                targets = parser.targets + [href for href, label in parser.links
                                             if label == row['title']]
                target = next((urllib.parse.urljoin(url, t) for t in targets
                               if valid_link(urllib.parse.urljoin(url, t)) and
                               urllib.parse.urlsplit(urllib.parse.urljoin(url, t)).hostname != 'news.google.com'), '')
                if not target:
                    target = self.publisher_link(row)
                if not target:
                    raise ValueError('원문 연결 주소 없음')
                parser, url = self.read(target)
            body = parser.body()
            if len(body) >= 100:
                text = summarize(body, row['title'])
                if text:
                    return Summary(text, 'body', '기사 본문 핵심 문장 자동 발췌', url, body)
        except Exception:
            note = '원문 접근 실패 또는 본문 확인 불가'
        description = row.get('description', '').strip()
        if description and description not in (row['title'], row['title'] + ' ' + row.get('source', '')):
            text = summarize(description, row['title'])
            if text:
                return Summary(text, 'rss', note + ' · RSS 설명 기준 요약')
        return Summary('', 'unavailable', note + ' · 요약할 내용이 없습니다. 기사 열기로 확인하세요.')

    def publisher_link(self, row):
        """Google opaque links: accept a Bing publisher result only for the same story."""
        from news_collection import NewsCollector
        articles = NewsCollector(timeout=self.timeout).fetch_bing('"' + row['title'] + '"', 168)
        normalize = lambda value: re.sub(r'\s+', '', value).casefold()
        published = row.get('published', time.time())
        for article in articles:
            if (normalize(article.title) == normalize(row['title'])
                    and normalize(article.source) == normalize(row.get('source', ''))
                    and abs(article.published - published) <= 48 * 3600
                    and urllib.parse.urlsplit(article.url).hostname != 'news.google.com'):
                return article.url
        return ''
