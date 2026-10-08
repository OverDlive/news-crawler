"""News collection and persistence, independent of the desktop UI."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from search_rules import SearchRule
from news_dedup import deduplicate

DEFAULTS = {
    "keywords": [], "interval_minutes": 10, "hours": 24,
    "page_seconds": 20, "page_size": 5, "new_minutes": 10,
    "auto_rotate": True, "theme": "system",
}

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
_legacy_collector = None


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "li", "div"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = PlainText()
    parser.feed(value or "")
    return " ".join("".join(parser.parts).split())


class FeedDescription(PlainText):
    """Keep a primary item's description, excluding bundled related headlines."""
    def __init__(self, link):
        super().__init__()
        self.link = urllib.parse.urlsplit(link)._replace(query='', fragment='').geturl()
        self.entries = []
        self.stack = []
        self.has_list = False

    def handle_starttag(self, tag, attrs):
        if tag == 'li':
            self.has_list = True
            self.stack.append([len(self.parts), False])
        if tag == 'a' and self.stack:
            target = dict(attrs).get('href', '')
            try:
                normalized = urllib.parse.urlsplit(target)._replace(query='', fragment='').geturl()
                if normalized == self.link:
                    self.stack[-1][1] = True
            except ValueError:
                pass
        super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == 'li' and self.stack:
            start, primary = self.stack.pop()
            if primary:
                self.entries.append(' '.join(''.join(self.parts[start:]).split()))
        super().handle_endtag(tag)

    def description(self, value):
        self.feed(value or '')
        if self.has_list:
            return self.entries[0] if self.entries else ''
        return ' '.join(''.join(self.parts).split())


def valid_link(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def article_link(value):
    """Extract Bing's publisher URL without making another network request."""
    if not valid_link(value):
        return ''
    parsed = urllib.parse.urlsplit(value)
    if parsed.hostname in ('www.bing.com', 'bing.com') and parsed.path == '/news/apiclick.aspx':
        target = urllib.parse.parse_qs(parsed.query).get('url', [''])[0]
        if valid_link(target):
            return target
    if parsed.hostname == 'news.google.com':
        return urllib.parse.urlunsplit(parsed._replace(query='', fragment=''))
    return value


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    source: str
    published: float
    description: str


def parse_feed(data: bytes) -> list[Article]:
    root = ET.fromstring(data)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("뉴스 RSS 형식이 아닙니다.")
    articles = []
    for item in root.findall("./channel/item"):
        title = plain_text(item.findtext("title", ""))
        link = item.findtext("link", "").strip()
        if not title or not valid_link(link):
            continue
        try:
            date = parsedate_to_datetime(item.findtext("pubDate", ""))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            published = date.timestamp()
        except (ValueError, TypeError, OverflowError):
            # Unknown dates cannot be presented as freshly published news.
            continue
        source = plain_text(item.findtext("source", ""))
        if not source:
            source = next((plain_text(child.text) for child in item
                           if child.tag.rsplit('}', 1)[-1].casefold() == 'source' and child.text), '')
        source = source or "언론사 미상"
        suffix = " - " + source
        if title.endswith(suffix):
            title = title[:-len(suffix)]
        description = FeedDescription(link).description(item.findtext("description", ""))
        if description in (title, title + " " + source, title + suffix):
            description = ""
        link = article_link(link)
        articles.append(Article(link, title, source, published, description[:1500]))
    return articles


def fetch_news(keyword: str | dict, hours: int = 24) -> list[Article]:
    # Reuse pacing and cooldown even for callers outside the desktop collector.
    global _legacy_collector
    if _legacy_collector is None:
        from news_collection import NewsCollector
        _legacy_collector = NewsCollector(timeout=20)
    rule = SearchRule({"name": keyword} if isinstance(keyword, str) else keyword)
    results = {}
    queries = rule.bing_queries if hasattr(rule, 'company') else rule.queries
    for query in queries:
        for article in _legacy_collector.fetch(query, hours):
            if rule.accepts(article):
                results[article.url] = article
    return sorted(results.values(), key=lambda article: article.published, reverse=True)


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.config_path = directory / "settings.json"
        self.warning = ""
        self.db = sqlite3.connect(directory / "news.sqlite3")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                url TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL,
                published REAL NOT NULL, description TEXT NOT NULL, discovered REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS articles_published ON articles(published);
            CREATE TABLE IF NOT EXISTS matches (
                url TEXT REFERENCES articles(url) ON DELETE CASCADE,
                keyword TEXT NOT NULL, PRIMARY KEY(url, keyword)
            );
            CREATE TABLE IF NOT EXISTS checks (keyword TEXT PRIMARY KEY, succeeded REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS summaries (
                url TEXT PRIMARY KEY REFERENCES articles(url) ON DELETE CASCADE,
                summary TEXT NOT NULL, basis TEXT NOT NULL, note TEXT NOT NULL,
                content_url TEXT NOT NULL, checked REAL NOT NULL
            );
        """)

    def load_config(self):
        config = {**DEFAULTS, "keywords": []}
        if not self.config_path.exists():
            return config
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("설정 형식 오류")
            for key, low, high in (("interval_minutes", 1, 60), ("hours", 1, 168),
                                   ("page_seconds", 5, 300), ("page_size", 3, 8),
                                   ("new_minutes", 1, 60)):
                number = value.get(key, DEFAULTS[key])
                if type(number) is not int or not low <= number <= high:
                    raise ValueError("설정 범위 오류")
                config[key] = max(10, number) if key == "interval_minutes" else number
            if type(value.get("auto_rotate", True)) is not bool:
                raise ValueError("자동 전환 설정 오류")
            config["auto_rotate"] = value.get("auto_rotate", True)
            theme = value.get("theme", "system")
            config["theme"] = theme if theme in ("system", "light", "dark") else "system"
            seen = set()
            for entry in value.get("keywords", []):
                name = entry["name"].strip()
                if not name or len(name) > 80 or name.casefold() in seen:
                    raise ValueError("키워드 형식 오류")
                if type(entry["enabled"]) is not bool:
                    raise ValueError("키워드 상태 오류")
                seen.add(name.casefold())
                stored = {"name": name, "enabled": entry["enabled"]}
                for key in ("condition", "exclude", "scope", "profile", "company", "aliases", "category", "terms", "disabled_terms"):
                    if key in entry:
                        stored[key] = entry[key]
                SearchRule(stored)
                config["keywords"].append(stored)
            return config
        except (ValueError, TypeError, KeyError, AttributeError):
            backup = self.directory / f"settings.invalid-{time.time_ns()}.json"
            self.config_path.replace(backup)
            self.warning = f"설정을 읽지 못해 초기화했습니다. 원본 보관: {backup.name}"
            return {**DEFAULTS, "keywords": []}

    def save_config(self, config):
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.config_path)

    def ingest(self, keyword: str, articles: list[Article], now=None) -> int:
        now = time.time() if now is None else now
        added = 0
        with self.db:
            for article in articles:
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?)",
                    (article.url, article.title, article.source, article.published, article.description, now),
                )
                added += cursor.rowcount
                self.db.execute("INSERT OR IGNORE INTO matches VALUES (?,?)", (article.url, keyword))
            self.db.execute("INSERT OR REPLACE INTO checks VALUES (?,?)", (keyword, now))
        return added

    def remove_keyword(self, keyword):
        with self.db:
            self.db.execute("DELETE FROM matches WHERE keyword=?", (keyword,))
            self.db.execute("DELETE FROM checks WHERE keyword=?", (keyword,))
            self.db.execute("DELETE FROM articles WHERE url NOT IN (SELECT url FROM matches)")

    def list_articles(self, keywords, hours=24, now=None):
        if not keywords:
            return []
        now = time.time() if now is None else now
        slots = ",".join("?" for _ in keywords)
        rows = self.db.execute(f"""
            SELECT DISTINCT a.* FROM articles a JOIN matches m ON a.url=m.url
            WHERE m.keyword IN ({slots}) AND a.published BETWEEN ? AND ?
            ORDER BY a.published DESC, a.url
        """, (*keywords, now - hours * 3600, now + 300)).fetchall()
        tag_rows = self.db.execute(
            f"SELECT url, keyword FROM matches WHERE keyword IN ({slots})", keywords
        ).fetchall()
        tags = {}
        for url, keyword in tag_rows:
            tags.setdefault(url, []).append(keyword)
        names = ("url", "title", "source", "published", "description", "discovered")
        summaries = {row[0]: dict(zip(('summary', 'summary_basis', 'summary_note', 'content_url'), row[1:]))
                     for row in self.db.execute('SELECT url, summary, basis, note, content_url FROM summaries')}
        return deduplicate([{**dict(zip(names, row)), **summaries.get(row[0], {}),
                             "keywords": tags.get(row[0], [])} for row in rows])

    def summary_candidates(self, keywords, hours=24, now=None, limit=5):
        if not keywords:
            return []
        now = time.time() if now is None else now
        slots = ','.join('?' for _ in keywords)
        rows = self.db.execute(f'''
            SELECT DISTINCT a.url, a.title, a.source, a.description, a.published FROM articles a
            JOIN matches m ON m.url=a.url LEFT JOIN summaries s ON s.url=a.url
            WHERE m.keyword IN ({slots}) AND a.published BETWEEN ? AND ?
            AND (s.url IS NULL OR (s.basis != 'body' AND s.checked < ?))
            ORDER BY a.published DESC LIMIT ?
        ''', (*keywords, now - hours * 3600, now + 300, now - 86400, limit)).fetchall()
        return [dict(zip(('url', 'title', 'source', 'description', 'published'), row)) for row in rows]

    def save_summary(self, url, summary, now=None):
        with self.db:
            # Removed keywords/articles must not be resurrected by late workers.
            self.db.execute('''INSERT OR REPLACE INTO summaries
                SELECT url, ?, ?, ?, ?, ? FROM articles WHERE url=?''',
                (summary.text, summary.basis, summary.note, summary.content_url,
                 time.time() if now is None else now, url))

    def get_summary(self, url):
        row = self.db.execute('SELECT summary, basis, note, content_url FROM summaries WHERE url=?', (url,)).fetchone()
        return dict(zip(('summary', 'summary_basis', 'summary_note', 'content_url'), row)) if row else {}

    def latest_success(self, keywords):
        if not keywords:
            return None
        slots = ",".join("?" for _ in keywords)
        row = self.db.execute(f"SELECT MAX(succeeded) FROM checks WHERE keyword IN ({slots})", keywords).fetchone()
        return row[0]

    def prune(self, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("DELETE FROM articles WHERE published < ?", (now - 30 * 86400,))

    def close(self):
        self.db.close()
