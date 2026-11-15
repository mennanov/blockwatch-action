#!/usr/bin/env python3
"""Black-box tests for run.py: what the action does with blockwatch's output.

Real repository, real blockwatch, but the assertions are only about run.py —
annotations, exit code, input handling. blockwatch's own contract (columns,
address format, message wording) is read back from the diagnostics it emitted,
never hardcoded, so a change there is not a failure here. The one exception is
the half-open range convention, which run.py exists to translate: the column
test asserts the conversion, so a blockwatch that stopped reporting an
exclusive end would rightly fail it. `blockwatch` must be on PATH, at the
version action.yml pins.

python3 scripts/run_test.py          # or: python3 -m pytest scripts/run_test.py
"""

from __future__ import annotations

import unittest

from tests.harness import ensure_pinned_blockwatch
from tests.test_core import CoreTest as CoreTest
from tests.test_pr_api import PullRequestApiTest as PullRequestApiTest
from tests.test_suppressions import (
    SuppressionDiscoveryTest as SuppressionDiscoveryTest,
    SuppressionPolicyTest as SuppressionPolicyTest,
    SuppressionReportingTest as SuppressionReportingTest,
)


def setUpModule() -> None:
    ensure_pinned_blockwatch()


if __name__ == "__main__":
    loader = unittest.TestLoader()
    unittest.main(testLoader=loader, verbosity=2)
