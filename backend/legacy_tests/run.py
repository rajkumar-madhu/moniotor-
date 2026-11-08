"""Run the legacy security tests:  python legacy_tests/run.py [labels...]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from legacy_tests import harness

harness.setup()

from django.test.utils import get_runner  # noqa: E402
from django.conf import settings  # noqa: E402

if __name__ == '__main__':
    labels = sys.argv[1:] or ['legacy_tests']
    Runner = get_runner(settings)
    failures = Runner(verbosity=2).run_tests(labels)
    sys.exit(bool(failures))
