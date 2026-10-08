from pathlib import Path
import tempfile
import unittest

from news_core import Article, Store
from news_dedup import deduplicate, same_story


def row(title, description='', published=1_800_000_000, source='신문 A'):
    return dict(url='https://example.com/' + source, title=title, description=description,
                published=published, discovered=published, source=source, keywords=['뉴스'])


class StoryTests(unittest.TestCase):
    def test_publisher_punctuation_aliases_and_amount_formats(self):
        a = row('한전 직원 2만4000명 개인정보 노출…고객정보는 피해 없어')
        b = row('[속보] 한국전력, 2만 4천여 명 전체 직원 개인정보 외부 노출', source='신문 B')
        self.assertTrue(same_story(a, b))
        self.assertEqual(len(deduplicate([a, b])), 1)

    def test_description_supports_differently_worded_headlines(self):
        summary = '미래에너지 연구원은 발전소 제어 시스템 보안을 강화하기 위해 새 탐지 시스템을 개발하고 현장에서 검증했다.'
        a = row('미래에너지, 발전소 보안 탐지 시스템 개발', summary)
        b = row('미래에너지 발전소 제어 시스템 보안 강화 기술 공개', summary)
        self.assertTrue(same_story(a, b))

    def test_different_companies_amounts_and_events_remain_separate(self):
        a = row('한국전력 직원 2만4000명 개인정보 노출')
        for title in ('한전KDN 직원 2만4000명 개인정보 노출',
                      '한국전력 직원 3000명 개인정보 노출',
                      '한국전력 직원 2만4000명 임금 협상 타결',
                      '한국전력 직원 2만4000명 개인정보 노출 없어'):
            with self.subTest(title=title):
                self.assertFalse(same_story(a, row(title)))

    def test_boilerplate_and_short_titles_do_not_merge_unrelated_articles(self):
        boilerplate = '이 뉴스는 실시간으로 제공되는 기사입니다. 더 많은 소식은 홈페이지에서 확인하세요.'
        self.assertFalse(same_story(row('한국전력 신재생에너지 발전소 건설', boilerplate),
                                    row('한국전력 고객 개인정보 보호 시스템 구축', boilerplate)))
        self.assertFalse(same_story(row('기사 a', boilerplate), row('기사 b', boilerplate)))
        self.assertFalse(same_story(row('[삼성전자] 신제품 출시 기자회견 개최', boilerplate),
                                    row('[LG전자] 신제품 출시 기자회견 개최', boilerplate)))

    def test_old_repeat_is_separate_and_identical_short_titles_merge(self):
        a = row('반도체 수출 증가')
        self.assertTrue(same_story(a, row('반도체 수출 증가', source='신문 B')))
        self.assertFalse(same_story(a, row(a['title'], published=a['published'] - 49 * 3600)))

    def test_existing_store_filter_tags_discovery_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            now = 1_800_000_000
            a = Article('https://example.com/a', '한전 직원 2만4000명 개인정보 노출', 'A', now - 100, '')
            b = Article('https://example.com/b', '한국전력 직원 2만4천여명 개인정보 노출', 'B', now - 50, '')
            try:
                store.ingest('공격 유형', [a], now - 80)
                store.ingest('정부 대응', [b], now - 10)
                store.close()
                store = Store(Path(directory))
                rows = store.list_articles(['공격 유형', '정부 대응'], now=now)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['url'], b.url)
                self.assertEqual(rows[0]['discovered'], now - 80)
                self.assertEqual(set(rows[0]['keywords']), {'공격 유형', '정부 대응'})
                self.assertEqual(rows[0]['related_count'], 2)
                self.assertEqual(rows[0]['sources'], ['B', 'A'])
                self.assertEqual(store.list_articles(['공격 유형'], now=now)[0]['url'], a.url)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 2)
                store.remove_keyword('정부 대응')
                self.assertEqual(store.list_articles(['공격 유형'], now=now)[0]['url'], a.url)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
