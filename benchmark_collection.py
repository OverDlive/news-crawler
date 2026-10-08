"""Reproducible offline comparison; no external service or stored news required."""
import argparse
from email.utils import formatdate
import json
import time
from unittest.mock import patch

from news_collection import NewsCollector
from news_core import Article, fetch_news
from security_profiles import COMPANIES, TOPICS, make_entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--latency', type=float, default=.04, help='simulated seconds per request')
    args = parser.parse_args()
    entries = [entry for company, aliases in COMPANIES.items() for entry in make_entries(company, aliases, TOPICS)]
    article = Article('https://example.com/benchmark', '한국남동발전 랜섬웨어 과징금', '테스트', time.time(), '')
    feed = f'<rss><channel><item><title>{article.title}</title><link>{article.url}</link><pubDate>{formatdate(article.published, usegmt=True)}</pubDate></item></channel></rss>'.encode()

    class Response:
        def __enter__(self):
            time.sleep(args.latency)
            return self
        def __exit__(self, *args):
            return False
        def read(self, maximum):
            return feed
        headers = {}

    # Offline throughput comparison only; production Google pacing stays enabled.
    with patch('urllib.request.urlopen', side_effect=lambda *a, **k: Response()) as network, \
            patch('news_core._legacy_collector', NewsCollector(google_request_interval=0)):
        started = time.perf_counter()
        old = [(entry['name'], fetch_news(entry)) for entry in entries]
        sequential = time.perf_counter() - started
        old_count = network.call_count
        network.reset_mock()
        new = NewsCollector(google_request_interval=0).collect(entries)
        new_count = network.call_count
    assert {n: [a.url for a in items] for n, items in old} == {n: [a.url for a in items] for n, items in new.results}
    print(json.dumps({'profiles': len(entries), 'requests_before': old_count, 'requests_after': new_count,
                      'seconds_before': round(sequential, 3), 'seconds_after': round(new.seconds, 3),
                      'speedup': round(sequential / new.seconds, 2), 'same_results': True}, indent=2))


if __name__ == '__main__':
    main()
