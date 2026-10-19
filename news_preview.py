"""Best-effort publisher metadata for clickable news cards, outside the GUI thread."""
from functools import lru_cache
from urllib.parse import urljoin, urlsplit

from news_core import valid_link
from news_summary import ArticleSummarizer


@lru_cache(maxsize=128)
def _metadata(url, title, source, published):
    row = dict(url=url, title=title, source=source)
    if published:
        row['published'] = published
    parser, resolved = ArticleSummarizer(timeout=5).read_article(row)
    image = next((target for value in [*parser.images, *parser.twitter_images]
                  if value.strip() and valid_link(target := urljoin(resolved, value.strip()))), '')
    return resolved, image


def article_preview(article):
    url = article.get('content_url') or article['url']
    image = article.get('image_url', '')
    if valid_link(image):
        return {**article, 'content_url': url}
    try:
        resolved, image = _metadata(url, article['title'], article.get('source', ''),
                                    article.get('published', 0))
    except Exception:
        # Exceptions aren't cached: a temporary failure can recover next time.
        resolved, image = url, ''
    return {**article, 'content_url': resolved, 'image_url': image}


def telegram_preview_url(article):
    url = article.get('content_url') or article['url']
    # Native previews handle publisher pages; resolve Google's portal first.
    if urlsplit(url).hostname == 'news.google.com':
        return article_preview(article)['content_url']
    return url
