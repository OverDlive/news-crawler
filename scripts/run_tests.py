"""Run the suite and expose full failure details as GitHub annotations."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
result = unittest.TextTestRunner(verbosity=2).run(suite)
for test, failure in result.failures + result.errors:
    message = failure.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    print(f'::error title={test.id()}::{message}', flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
