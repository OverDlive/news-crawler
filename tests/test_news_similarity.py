from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from news_core import Article, Store
from news_dedup import deduplicate
from news_similarity import body_score, clean_body, compare_bodies, fingerprint, CONFLICT
from news_summary import Summary


BODY = ('한국전력이 직원 개인정보 유출 사고를 확인하고 조사에 착수했다고 밝혔다. '
        '외부 접속 기록을 분석한 결과 직원 24000명의 정보가 노출된 것으로 파악됐다. '
        '회사는 관련 시스템의 접속을 제한하고 피해 대상 직원들에게 사고 내용을 안내했다. '
        '보안 담당 부서는 외부 전문 기관과 함께 유출 경로를 추적하고 추가 피해 여부를 점검하고 있다. '
        '현재까지 고객 정보 피해는 확인되지 않았으며 조사 결과에 따라 재발 방지 대책을 마련할 예정이다.')


def article(url, title, body=BODY, published=1800000000):
    return dict(url=url, title=title, body=body, description='', source=url,
                published=published, discovered=published, keywords=['뉴스'])


class BodyMatchingTests(unittest.TestCase):
    def test_different_headlines_same_body_merge_and_members_survive(self):
        rows = [article('a', '한국전력 직원 정보 유출 사고 조사'),
                article('b', '한전 보안 담당 부서 외부 접속 경로 추적',
                        BODY + ' 담당자는 후속 조사 결과를 공개하겠다고 설명했다.')]
        results = compare_bodies(rows, set(), threading.Event())
        self.assertGreaterEqual(results[0][-1], .82)
        grouped = deduplicate(rows, {('a', 'b'): results[0][-1]})
        self.assertEqual(len(grouped), 1)
        self.assertEqual([r['url'] for r in grouped[0]['related_articles']], ['a', 'b'])
        self.assertNotIn('related_articles', rows[0])
        self.assertNotIn('body', grouped[0])
        self.assertNotIn('body', grouped[0]['related_articles'][0])

    def test_changed_company_size_date_and_denial_are_separate(self):
        a = article('a', '한국전력 직원 24000명 개인정보 유출')
        others = [article('b', '한전KDN 직원 24000명 개인정보 유출'),
                  article('b', '한국전력 직원 24000명 임금 협상 타결'),
                  article('b', '한전 직원 3000명 개인정보 유출'),
                  article('b', '한국전력 직원 24000명 개인정보 유출 없어'),
                  article('b', a['title'], published=a['published'] - 49 * 3600),
                  article('b', '한전 직원 정보 유출', BODY.replace('24000명', '3000명'))]
        for b in others:
            with self.subTest(title=b['title']):
                result = compare_bodies([a, b], set(), threading.Event())
                self.assertTrue(not result or result[0][-1] == CONFLICT)

    def test_cache_negative_decisions_body_changes_and_cancel(self):
        rows = [article('a', '한국전력 개인정보 유출'), article('b', '한전 개인정보 유출')]
        result = compare_bodies(rows, set(), threading.Event())
        with patch('news_similarity.body_score', side_effect=AssertionError('recomputed')):
            self.assertEqual(compare_bodies(rows, {result[0][:4]}, threading.Event()), [])
        rows[1]['body'] += ' 추가 조사 결과를 발표했다.'
        self.assertEqual(len(compare_bodies(rows, {result[0][:4]}, threading.Event())), 1)
        stopped = threading.Event()
        stopped.set()
        self.assertEqual(compare_bodies(rows, set(), stopped), [])
        self.assertEqual(body_score('짧은 본문', '짧은 본문'), 0)
        self.assertNotIn('저작권', clean_body(BODY + '\n저작권 안내 및 재배포 금지'))

    def test_similarity_chains_do_not_merge(self):
        rows = [article('a', '한국전력 직원 개인정보 유출 조사'),
                article('b', '보안 담당 부서 외부 접속 경로 추적'),
                article('c', '당국 시스템 차단 이후 고객 안내 현황')]
        groups = deduplicate(rows, {('a', 'b'): .95, ('b', 'c'): .95, ('a', 'c'): .2})
        self.assertEqual([r['related_count'] for r in groups], [2, 1])

    def test_almost_identical_bodies_override_different_headline_numbers_and_angle(self):
        body = BODY.replace('한국전력', 'OK저축은행').replace('24000', '250')
        a = article('a', 'OK저축은행 고객 250명 개인정보 유출', body)
        for title in ('OK저축은행 고객 정보 유출 확인…2차 피해 차단 총력',
                      'OK저축은행 개인정보 보호 대책 마련',
                      '금감원, OK저축은행 사고 후속 대응 확인'):
            with self.subTest(title=title):
                b = article('b', title, body + ' 고객 상담창구를 별도로 운영한다.')
                result = compare_bodies([a, b], set(), threading.Event())
                self.assertGreaterEqual(result[0][-1], .82)
                self.assertEqual(len(deduplicate([a, b], {('a', 'b'): result[0][-1]})), 1)

    def test_publication_metadata_related_articles_and_dates_do_not_veto_body_match(self):
        a = article('a', 'OK저축은행 고객정보 유출 확인',
                    '입력 2026.10.15 11:35\n' + BODY + '\n관련기사\n다른 은행 3000명 피해 14일 발표')
        b = article('b', 'OK저축은행 피해 고객 안내',
                    '수정 2026.10.16 12:00\n' + BODY + '\n관련기사\n새 은행 7000명 피해 15일 발표')
        result = compare_bodies([a, b], set(), threading.Event())
        self.assertGreaterEqual(result[0][-1], .82)
        self.assertNotIn('3000', clean_body(a['body']))
        self.assertNotIn('7000', clean_body(b['body']))

    def test_longer_verbatim_version_matches_without_identical_lengths(self):
        addition = ('담당 부서는 현재 운영 중인 모든 서비스의 접근 권한을 다시 점검한다. '
                    '외부 전문가들과 함께 수집한 기록을 분석하고 확인된 내용을 안내한다. '
                    '향후 피해 상담과 관련한 연락은 별도로 마련한 창구를 통해 처리한다. ')
        left, right = clean_body(BODY), clean_body(BODY + addition)
        self.assertGreaterEqual(body_score(left, right), .82)

    def test_low_body_score_does_not_split_same_confirmed_headline(self):
        rows = [article('a', 'OK저축은행 고객 250명 개인정보 유출'),
                article('b', 'OK저축은행 대출 고객 250명 정보 유출')]
        self.assertEqual(len(deduplicate(rows, {('a', 'b'): .25})), 1)
        self.assertEqual(len(deduplicate(rows, {('a', 'b'): CONFLICT})), 2)

    def test_updated_victim_total_remains_separate_from_earlier_report(self):
        body = BODY.replace('한국전력', 'OK저축은행').replace('24000', '250')
        updated = article('b', 'OK저축은행 유출 피해 183명→250명으로 확대', body)
        earlier = article('a', 'OK저축은행 고객 183명 개인정보 유출', body.replace('250', '183'))
        current = article('c', 'OK저축은행 고객 250명 개인정보 유출', body)
        results = compare_bodies([earlier, updated, current], set(), threading.Event())
        scores = {(a, b): score for a, b, _, _, score in results}
        self.assertEqual(scores[('a', 'b')], CONFLICT)
        self.assertGreaterEqual(scores[('b', 'c')], .82)

    def test_algorithm_upgrade_invalidates_old_negative_cache(self):
        import hashlib
        from news_similarity import VERSION
        old = hashlib.sha256(('tfidf-char-v1' + clean_body(BODY)).encode()).hexdigest()
        self.assertNotEqual(fingerprint(BODY), old)
        self.assertNotEqual(VERSION, 'tfidf-char-v1')

    def test_persistence_filters_changed_hash_and_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            rows = [article('a', '한국전력 직원 정보 유출 사고 조사'),
                    article('b', '한전 외부 접속 경로 추적')]
            for index, r in enumerate(rows):
                store.ingest(('첫째', '둘째')[index], [Article(r['url'], r['title'], r['source'], r['published'], '')])
                store.save_summary(r['url'], Summary('요약', 'body', '', '', r['body']))
            store.save_comparisons(compare_bodies(rows, set(), threading.Event()))
            store.close()
            store = Store(Path(directory))
            self.assertEqual(len(store.list_articles(['첫째', '둘째'], now=1800000000)), 1)
            self.assertEqual(len(store.list_articles(['첫째'], now=1800000000)[0]['related_articles']), 1)
            self.assertEqual(len(store.comparison_cache()), 1)
            store.save_summary('b', Summary('수정', 'body', '', '', BODY.replace('유출', '임금')))
            self.assertEqual(len(store.list_articles(['첫째', '둘째'], now=1800000000)), 2)
            store.remove_keyword('둘째')
            self.assertEqual(store.comparison_cache(), set())
            store.save_summary('b', Summary('늦은 결과', 'body', '', '', BODY))
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM article_bodies').fetchone()[0], 1)
            store.close()
