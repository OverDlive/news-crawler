"""Compare configured queries with a known news search (read-only audit)."""
import argparse
import json
from pathlib import Path
import time

from news_collection import NewsCollector
from search_rules import SearchRule


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--output', default='audit/latest.json')
    args = parser.parse_args()
    config = json.loads(Path('data/settings.json').read_text(encoding='utf-8'))
    entries = [entry for entry in config['keywords'] if entry['enabled']]
    collector = NewsCollector(fallback=True)
    if args.all:
        report = collector.collect(entries, config['hours'])
        rows = [{'name': name, 'count': len(articles), 'articles': [article.__dict__ for article in articles]}
                for name, articles in report.results]
        result = {'checked_at': time.time(), 'hours': config['hours'], 'requests': report.requests,
                  'seconds': report.seconds, 'results': rows,
                  'errors': [(name, str(error)) for name, error in report.errors],
                  'provider': report.provider, 'fallback_reason': report.fallback_reason}
        result['live_collection_verified'] = not report.errors and not report.cancelled
        result['total_unique_articles'] = len({article.url for _, articles in report.results for article in articles})
        for row in rows:
            print(row['name'], row['count'])
        print('requests', report.requests, 'errors', len(report.errors))
    else:
        entry = next(e for e in entries if e.get('company') == '한국전력' and e.get('category') == '공격 유형')
        rule = SearchRule(entry)
        queries = rule.queries + ['한전 개인정보', '"한전" "개인정보"',
                                  '(한전 OR 한국전력) (개인정보 OR 노출)', '"한국전력" "노출"']
        rows = []
        for query in queries:
            try:
                articles = collector.fetch(query, config['hours'])
            except Exception as exc:
                rows.append({'query': query, 'error': str(exc), 'raw': None, 'kept': None})
                print('ERROR', str(exc), 'QUERY', query)
                if collector.cooldown_remaining():
                    break
                continue
            kept = [article for article in articles if rule.accepts(article)]
            rows.append({'query': query, 'raw': len(articles), 'kept': len(kept),
                         'articles': [article.__dict__ for article in articles]})
            print('RAW', len(articles), 'MATCHES', len(kept), 'QUERY', query)
            print('FIRST', articles[0].title if articles else '-')
        result = {'checked_at': time.time(), 'hours': config['hours'], 'results': rows}
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
