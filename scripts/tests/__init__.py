"""Black-box test suite for scripts/run.py.

Run via `python3 -m unittest discover -s scripts -v` with the pinned
`blockwatch` binary on PATH. Discovery returns a thread-pooled suite because
the tests spend almost all of their wall-clock time waiting on isolated `git`
and `blockwatch` subprocesses; set `UNITTEST_WORKERS=1` to run sequentially.
"""

from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import ModuleType, TracebackType
from typing import Any, Iterator

_OptExcInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]
_RecordedEvent = tuple[str, tuple[Any, ...]]


def _fixture_error(description: str) -> unittest.TestCase:
    """Create a synthetic test case without exposing a TestCase class at module scope."""

    class FixtureError(unittest.TestCase):
        def runTest(self) -> None:
            return None

        def id(self) -> str:
            return description

        def __str__(self) -> str:
            return description

    return FixtureError("runTest")


class _RecordingResult(unittest.TestResult):
    """Records one test case's unittest callbacks for thread-safe main-thread replay."""

    def __init__(self, failfast: bool) -> None:
        super().__init__()
        self.failfast = failfast
        self.events: list[_RecordedEvent] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.testsRun += 1

    def stopTest(self, test: unittest.TestCase) -> None:
        return None

    def addSuccess(self, test: unittest.TestCase) -> None:
        self.events.append(("addSuccess", (test,)))

    def addError(
        self,
        test: unittest.TestCase,
        err: _OptExcInfo,
    ) -> None:
        self.events.append(("addError", (test, err)))
        if self.failfast:
            self.stop()

    def addFailure(
        self,
        test: unittest.TestCase,
        err: _OptExcInfo,
    ) -> None:
        self.events.append(("addFailure", (test, err)))
        if self.failfast:
            self.stop()

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self.events.append(("addSkip", (test, reason)))

    def addExpectedFailure(
        self,
        test: unittest.TestCase,
        err: _OptExcInfo,
    ) -> None:
        self.events.append(("addExpectedFailure", (test, err)))

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        self.events.append(("addUnexpectedSuccess", (test,)))
        if self.failfast:
            self.stop()

    def addSubTest(
        self,
        test: unittest.TestCase,
        subtest: unittest.TestCase,
        err: _OptExcInfo | None,
    ) -> None:
        self.events.append(("addSubTest", (test, subtest, err)))
        if err is not None and self.failfast:
            self.stop()

    def addDuration(self, test: unittest.TestCase, elapsed: float) -> None:
        self.events.append(("addDuration", (test, elapsed)))


def _iter_cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def _worker_count(case_count: int) -> int:
    raw = os.environ.get("UNITTEST_WORKERS")
    if raw is not None:
        try:
            configured = int(raw)
        except ValueError as exc:
            raise ValueError("UNITTEST_WORKERS must be an integer, got %r" % raw) from exc
        if configured < 1:
            raise ValueError("UNITTEST_WORKERS must be >= 1, got %d" % configured)
        return min(case_count, configured)
    return min(case_count, os.cpu_count() or 4)


def _run_case(case: unittest.TestCase, failfast: bool) -> list[_RecordedEvent]:
    local_result = _RecordingResult(failfast=failfast)
    case(local_result)
    return local_result.events


def _replay_events(
    result: unittest.TestResult,
    case: unittest.TestCase,
    events: list[_RecordedEvent],
) -> None:
    result.startTest(case)
    try:
        for method_name, args in events:
            if method_name == "addDuration" and not hasattr(result, "addDuration"):
                continue
            getattr(result, method_name)(*args)
    finally:
        result.stopTest(case)


class ParallelTestSuite(unittest.TestSuite):
    """TestSuite executing independent TestCases concurrently across worker threads."""

    def _prepare_modules(
        self,
        cases: list[unittest.TestCase],
        result: unittest.TestResult,
    ) -> tuple[list[unittest.TestCase], list[ModuleType]]:
        runnable: list[unittest.TestCase] = []
        active_modules: list[ModuleType] = []
        module_outcomes: dict[str, tuple[str, str]] = {}

        for case in cases:
            if result.shouldStop:
                break
            mod_name = case.__class__.__module__
            if mod_name not in module_outcomes:
                module = sys.modules.get(mod_name)
                setup = getattr(module, "setUpModule", None) if module is not None else None
                if callable(setup) and module is not None:
                    try:
                        setup()
                    except unittest.SkipTest as exc:
                        module_outcomes[mod_name] = ("skip", str(exc))
                    except Exception:
                        module_outcomes[mod_name] = ("error", "")
                        result.addError(
                            _fixture_error("setUpModule (%s)" % mod_name), sys.exc_info()
                        )
                    else:
                        module_outcomes[mod_name] = ("ok", "")
                        active_modules.append(module)
                else:
                    module_outcomes[mod_name] = ("ok", "")

            status, detail = module_outcomes[mod_name]
            if status == "ok":
                runnable.append(case)
            elif status == "skip":
                result.startTest(case)
                result.addSkip(case, detail)
                result.stopTest(case)
        return runnable, active_modules

    def _teardown_modules(
        self,
        modules: list[ModuleType],
        result: unittest.TestResult,
    ) -> None:
        for module in reversed(modules):
            teardown = getattr(module, "tearDownModule", None)
            if callable(teardown):
                try:
                    teardown()
                except Exception:
                    result.addError(
                        _fixture_error("tearDownModule (%s)" % module.__name__),
                        sys.exc_info(),
                    )

    def run(self, result: unittest.TestResult, debug: bool = False) -> unittest.TestResult:
        cases = list(_iter_cases(self))
        workers = _worker_count(len(cases)) if cases else 1
        if debug or getattr(result, "buffer", False) or len(cases) <= 1 or workers <= 1:
            return super().run(result, debug=debug)

        runnable, active_modules = self._prepare_modules(cases, result)
        try:
            if not runnable or result.shouldStop:
                return result
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_case = {
                    pool.submit(_run_case, case, result.failfast): case for case in runnable
                }
                for future in as_completed(future_to_case):
                    case = future_to_case[future]
                    _replay_events(result, case, future.result())
                    if result.shouldStop:
                        for pending in future_to_case:
                            pending.cancel()
                        break
        finally:
            self._teardown_modules(active_modules, result)
        return result


def load_tests(
    loader: unittest.TestLoader,
    standard_tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    """Discover tests in this package and wrap them in a ParallelTestSuite."""
    package_dir = os.path.dirname(__file__)
    top_level_dir = os.path.dirname(package_dir)
    discovered = loader.discover(
        start_dir=package_dir,
        pattern=pattern or "test*.py",
        top_level_dir=top_level_dir,
    )
    return ParallelTestSuite([standard_tests, discovered])
