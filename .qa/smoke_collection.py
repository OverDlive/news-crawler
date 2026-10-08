from news_collection import NewsCollector
from security_profiles import COMPANIES, TOPICS, make_entries
import json
import urllib.error

client = NewsCollector()
try:
    articles = client.fetch('"KEPCO"', 24)
except Exception as exc:
    print(json.dumps({'simple_request': type(exc).__name__, 'reason': str(exc)}, ensure_ascii=True))
else:
    company = next(iter(COMPANIES))
    report = client.collect(make_entries(company, COMPANIES[company], TOPICS))
    print(json.dumps({'simple_request_articles': len(articles), 'requests': report.requests,
                      'seconds': round(report.seconds, 2), 'successes': len(report.results),
                      'errors': [(name, str(error)) for name, error in report.errors]}, ensure_ascii=True))
