"""Black-box test suite for scripts/run.py.

Run via `python3 -m unittest discover -s scripts -v` with the pinned
`blockwatch` binary on PATH. Discovery returns a thread-pooled suite because
these tests spend nearly all of their time waiting on isolated `git` and
`blockwatch` subprocesses; set `UNITTEST_WORKERS=1` to run sequentially.
"""

from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import TracebackType
from typing import Any, Iterator

from .harness import ensure_pinned_blockwatch

_ExcInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]


class _RecordingResult(unittest.TestResult):
    """Buffers one test's callbacks so main-thread replay never interleaves output."""

    def __init__(self, failfast: bool) -> None:
        super().__init__()
        self.failfast = failfast
        self.events: list[tuple[str, tuple[Any, ...]]] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.testsRun += 1

    def stopTest(self, test: unittest.TestCase) -> None:
        return None

    def _record(self, name: str, *args: Any, stop: bool = False) -> None:
        self.events.append((name, args))
        if stop and self.failfast:
            self.stop()

    def addSuccess(self, test: unittest.TestCase) -> None:
        self._record("addSuccess", test)

    def addError(self, test: unittest.TestCase, err: _ExcInfo) -> None:
        self._record("addError", test, err, stop=True)

    def addFailure(self, test: unittest.TestCase, err: _ExcInfo) -> None:
        self._record("addFailure", test, err, stop=True)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self._record("addSkip", test, reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: _ExcInfo) -> None:
        self._record("addExpectedFailure", test, err)

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        self._record("addUnexpectedSuccess", test, stop=True)

    def addSubTest(
        self,
        test: unittest.TestCase,
        subtest: unittest.TestCase,
        err: _ExcInfo | None,
    ) -> None:
        self._record("addSubTest", test, subtest, err, stop=err is not None)

    def addDuration(self, test: unittest.TestCase, elapsed: float) -> None:
        self._record("addDuration", test, elapsed)


def _iter_cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def _run_case(case: unittest.TestCase, failfast: bool) -> list[tuple[str, tuple[Any, ...]]]:
    local_result = _RecordingResult(failfast=failfast)
    case(local_result)
    return local_result.events


class ParallelTestSuite(unittest.TestSuite):
    """TestSuite executing independent TestCases concurrently across worker threads."""

    def run(self, result: unittest.TestResult, debug: bool = False) -> unittest.TestResult:
        cases = list(_iter_cases(self))
        workers = min(len(cases), int(os.environ.get("UNITTEST_WORKERS") or os.cpu_count() or 4))
        if debug or getattr(result, "buffer", False) or len(cases) <= 1 or workers <= 1:
            return super().run(result, debug=debug)

        ensure_pinned_blockwatch()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(_run_case, case, result.failfast): case for case in cases}
            for future in as_completed(pending):
                case = pending[future]
                result.startTest(case)
                try:
                    for method_name, args in future.result():
                        if method_name == "addDuration" and not hasattr(result, "addDuration"):
                            continue
                        getattr(result, method_name)(*args)
                finally:
                    result.stopTest(case)
                if result.shouldStop:
                    for remaining in pending:
                        remaining.cancel()
                    break
        return result


def load_tests(
    loader: unittest.TestLoader,
    standard_tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    """Discover tests in this package and wrap them in a ParallelTestSuite."""
    package_dir = os.path.dirname(__file__)
    discovered = loader.discover(
        start_dir=package_dir,
        pattern=pattern or "test*.py",
        top_level_dir=os.path.dirname(package_dir),
    )
    return ParallelTestSuite([standard_tests, discovered])
