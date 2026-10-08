"""Small live provider probe; saves original RSS for reproducible parsing checks."""
from pathlib import Path
import argparse
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


queries = {
    'google-simple': 'https://news.google.com/rss/search?' + urllib.parse.urlencode({
        'q': '한전 개인정보 when:24h', 'hl': 'ko', 'gl': 'KR', 'ceid': 'KR:ko'}),
    'bing-simple': 'https://www.bing.com/news/search?' + urllib.parse.urlencode({
        'q': '한전 개인정보', 'format': 'rss', 'setlang': 'ko-KR', 'cc': 'KR'}),
}
parser = argparse.ArgumentParser()
parser.add_argument('--bing', action='store_true')
parser.add_argument('--bing-conditions', action='store_true')
args = parser.parse_args()
if args.bing_conditions:
    from search_rules import SearchRule
    config = json.loads(Path('data/settings.json').read_text(encoding='utf-8'))
    entry = next(e for e in config['keywords'] if e.get('company') == '한국전력' and e.get('category') == '공격 유형')
    variants = [('simple-sorted', '한전 개인정보', True),
                ('quoted', '"한전" "개인정보"', False),
                ('aliases', '("한국전력" OR "한국전력공사" OR "한전") 개인정보', False),
                ('topics', '한전 (개인정보 OR 노출 OR 해킹)', False),
                ('condition', SearchRule(entry).queries[0], False)]
    queries = {}
    for name, query, sorted_results in variants:
        params = {'q': query, 'format': 'rss', 'setlang': 'ko-KR', 'cc': 'KR'}
        if sorted_results:
            params.update(sortby='date', count=50)
        queries['bing-' + name] = 'https://www.bing.com/news/search?' + urllib.parse.urlencode(params)
directory = Path('audit')
directory.mkdir(exist_ok=True)
for name, url in queries.items():
    if args.bing and not args.bing_conditions and name != 'bing-simple':
        continue
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'NewsMonitor/1.0'})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(4 * 1024 * 1024)
        (directory / (name + '.xml')).write_bytes(data)
        items = ET.fromstring(data).findall('./channel/item')
        print(name, 'items', len(items))
        if items:
            print(ET.tostring(items[0], encoding='unicode')[:2200])
    except Exception as exc:
        print(name, type(exc).__name__, str(exc))
