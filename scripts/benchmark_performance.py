"""Deterministic offline benchmark; no user data, network or notifications.

Optionally pass a pre-change news_similarity.py with --reference-file. That
reference may require scikit-learn, but the application and default benchmark do not.
"""
import argparse
import importlib.util
import itertools
import json
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def corpus(count=32):
    rng = random.Random(741)
    words = ['시스템', '담당자', '점검', '조사', '피해', '고객', '서비스', '분석',
             '대응', '계획', '기록', '접속', '확인', '보호', '안내', '보안']
    base = ''.join(rng.choices(words, k=240))
    return [base[:320] + ''.join(rng.choices(words, k=100 + i)) + base[320:]
            for i in range(count)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reference-file', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    from news_similarity import body_score
    pairs = list(itertools.combinations(corpus(), 2))
    result = {'documents': 32, 'pairs': len(pairs), 'runtime': sys.version.split()[0]}
    began = time.perf_counter()
    current = [body_score(a, b) for a, b in pairs]
    result['optimized_seconds'] = time.perf_counter() - began
    if args.reference_file:
        spec = importlib.util.spec_from_file_location('reference_similarity', args.reference_file)
        reference = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reference)
        began = time.perf_counter()
        expected = [reference.body_score(a, b) for a, b in pairs]
        result['reference_seconds_including_first_import'] = time.perf_counter() - began
        began = time.perf_counter()
        expected = [reference.body_score(a, b) for a, b in pairs]
        result['reference_seconds_warm'] = time.perf_counter() - began
        result['max_score_difference'] = max(abs(a - b) for a, b in zip(current, expected))
        result['threshold_decisions_equal'] = all((a >= .82) == (b >= .82) for a, b in zip(current, expected))
        assert result['max_score_difference'] < 2e-6, result
        assert result['threshold_decisions_equal'], result
    result['heavy_runtime_modules_loaded'] = any(name in sys.modules for name in ('numpy', 'sklearn', 'scipy')) if not args.reference_file else None
    output = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(output + '\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
