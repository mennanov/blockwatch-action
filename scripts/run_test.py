#!/usr/bin/env python3
"""Black-box tests for run.py.

The script is run the way the action runs it — as a subprocess, configured
entirely through the environment — and only its observable output is asserted:
the workflow commands on stdout, the diagnostics replayed to stderr, the exit
code, and the argv it hands to git and blockwatch. Nothing is imported from
run.py, so these stay honest if its internals are rearranged.

`git` and `blockwatch` are replaced by stubs on PATH. That keeps every test
hermetic — no repository, no network, no blockwatch install — and lets a test
assert on the exact command that *would* have run, which is where two of the
nastier bugs in this action's history lived.

Tests are ordered by importance, most critical first, and unittest is told to
keep that order so the worst failure is the first one reported.

    python3 scripts/run_test.py          # or: python3 -m pytest scripts/run_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from typing import Any

RUN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")

# One violation, shaped exactly as blockwatch 0.5.x reports it: a 1-based range
# with an inclusive end column, and the address to suppress it by.
VIOLATION = {
    "docs/cli.md": [
        {
            "range": {
                "start": {"line": 12, "character": 1},
                "end": {"line": 12, "character": 40},
            },
            "code": "keep-sorted",
            "message": "Block docs/cli.md:cli-docs defined at line 9 has an out-of-order line 12 (asc)",
            "severity": 1,
            "address": "docs/cli.md:cli-docs:keep-sorted:35385fe8",
        }
    ]
}

SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "results": [
                {
                    "ruleId": "keep-sorted",
                    "level": "error",
                    "message": {"text": "Block docs/cli.md:cli-docs defined at line 9 has an out-of-order line 12 (asc)"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "docs/cli.md"},
                                "region": {
                                    "startLine": 12,
                                    "startColumn": 1,
                                    "endLine": 12,
                                    "endColumn": 40,
                                },
                            }
                        }
                    ],
                    "properties": {"address": "docs/cli.md:cli-docs:keep-sorted:35385fe8"},
                }
            ]
        }
    ],
}


class Annotation:
    """One `::level key=value,...::message` line, parsed for assertions."""

    def __init__(self, line: str) -> None:
        head, _, self.message = line[2:].partition("::")
        level, _, properties = head.partition(" ")
        self.level = level
        self.properties: dict[str, str] = {}
        for pair in properties.split(",") if properties else []:
            key, _, value = pair.partition("=")
            self.properties[key] = value

    def __repr__(self) -> str:
        return "Annotation(%s, %r, %r)" % (self.level, self.properties, self.message)


class Result:
    """What one run of the script produced."""

    def __init__(self, process: subprocess.CompletedProcess[str], calls_dir: str) -> None:
        self.exit_code = process.returncode
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._calls_dir = calls_dir

    @property
    def annotations(self) -> list[Annotation]:
        return [Annotation(line) for line in self.stdout.splitlines() if line.startswith("::")]

    def argv(self, program: str) -> list[list[str]]:
        """Every argv the named stub was called with, in order."""
        path = os.path.join(self._calls_dir, program + ".jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


class ActionTestCase(unittest.TestCase):
    """Shared setup: stubbed binaries, a fake event, and a way to run the script."""

    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="blockwatch-action-test-")
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.bin_dir = os.path.join(self.workspace, "bin")
        self.calls_dir = os.path.join(self.workspace, "calls")
        os.makedirs(self.bin_dir)
        os.makedirs(self.calls_dir)
        self.summary_path = os.path.join(self.workspace, "summary.md")

        # A diff that is never inspected: the stub blockwatch decides the
        # outcome, so the patch only has to exist.
        self.write_stub("git", stdout="diff --git a/docs/cli.md b/docs/cli.md\n")
        self.write_stub("blockwatch", stderr=json.dumps(VIOLATION), exit_code=1)

    def write_stub(self, name: str, stdout: str = "", stderr: str = "", exit_code: int = 0) -> None:
        """Put an executable on PATH that records its argv and emits fixed output."""
        script = textwrap.dedent(
            '''\
            #!/usr/bin/env python3
            import json, os, sys
            record = os.path.join(%(calls)r, %(name)r + ".jsonl")
            with open(record, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(sys.argv[1:]) + "\\n")
            sys.stdout.write(%(stdout)r)
            sys.stderr.write(%(stderr)r)
            sys.exit(%(exit_code)d)
            '''
        ) % {
            "calls": self.calls_dir,
            "name": name,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        }
        path = os.path.join(self.bin_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(path, 0o755)

    def write_event(self, **payload: Any) -> str:
        path = os.path.join(self.workspace, "event.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def run_action(self, event: str | None = None, **inputs: str) -> Result:
        """Run the script with a clean environment, as the step would."""
        environment = {
            "PATH": self.bin_dir + os.pathsep + os.environ.get("PATH", ""),
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_SHA": "f" * 40,
            "GITHUB_STEP_SUMMARY": self.summary_path,
            "GITHUB_EVENT_PATH": event or self.write_event(before="a" * 40),
            # Left empty on purpose: with no token the script never reaches the
            # network, so every test here is hermetic.
            "GH_TOKEN": "",
        }
        environment.update(inputs)
        process = subprocess.run(
            [sys.executable, RUN_PY],
            env=environment,
            capture_output=True,
            text=True,
            cwd=self.workspace,
        )
        return Result(process, self.calls_dir)


class RunTest(ActionTestCase):
    # ----------------------------------------------------------------- 1
    def test_violation_becomes_an_error_annotation_carrying_its_address(self) -> None:
        """The feature itself: a violation lands on its own line, ready to act on."""
        result = self.run_action()

        annotations = result.annotations
        self.assertEqual(len(annotations), 1, result.stdout)
        annotation = annotations[0]
        self.assertEqual(annotation.level, "error")
        self.assertEqual(annotation.properties["file"], "docs/cli.md")
        self.assertEqual(annotation.properties["line"], "12")
        self.assertEqual(annotation.properties["endLine"], "12")
        # A single-line violation keeps its columns; GitHub ignores them on a
        # multi-line annotation.
        self.assertEqual(annotation.properties["col"], "1")
        self.assertEqual(annotation.properties["endColumn"], "40")
        self.assertEqual(annotation.properties["title"], "blockwatch%3A keep-sorted")
        self.assertIn("out-of-order line 12", annotation.message)
        # The address appears nowhere else on the line, and is what a reader
        # needs in order to suppress it.
        self.assertIn(
            "Suppress with: Blockwatch-suppress: docs/cli.md:cli-docs:keep-sorted:35385fe8",
            annotation.message,
        )

    # ----------------------------------------------------------------- 2
    def test_exit_code_is_blockwatch_s_own(self) -> None:
        """Reporting must never decide the verdict — in either direction."""
        self.assertEqual(self.run_action().exit_code, 1)

        self.write_stub("blockwatch", stderr="", exit_code=0)
        clean = self.run_action()
        self.assertEqual(clean.exit_code, 0)
        self.assertEqual(clean.annotations, [])

    # ----------------------------------------------------------------- 3
    def test_suppressed_violation_becomes_a_notice_annotation(self) -> None:
        """A suppressed violation is still reported, but must not block."""
        suppressed = json.loads(json.dumps(VIOLATION))
        suppressed["docs/cli.md"][0]["suppressed"] = True
        self.write_stub("blockwatch", stderr=json.dumps(suppressed), exit_code=0)

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual([a.level for a in result.annotations], ["notice"])

    # ----------------------------------------------------------------- 4
    def test_unparseable_diagnostics_are_replayed_but_not_annotated(self) -> None:
        """A usage error arrives on the same stream and must survive untouched."""
        self.write_stub("blockwatch", stderr="Error: diff in stdin is empty.\n", exit_code=2)

        result = self.run_action()

        self.assertEqual(result.exit_code, 2)
        self.assertIn("diff in stdin is empty.", result.stderr)
        self.assertEqual(result.annotations, [])

    # ----------------------------------------------------------------- 5
    def test_annotations_limit_caps_output_and_reports_the_remainder(self) -> None:
        """GitHub drops annotations past its own cap silently; this one says so."""
        many = {"docs/cli.md": VIOLATION["docs/cli.md"] * 3}
        self.write_stub("blockwatch", stderr=json.dumps(many), exit_code=1)

        result = self.run_action(INPUT_ANNOTATIONS_LIMIT="1")

        levels = [a.level for a in result.annotations]
        self.assertEqual(levels, ["error", "notice"])
        self.assertIn("2 further violation(s) were not annotated", result.annotations[1].message)

    # ----------------------------------------------------------------- 6
    def test_sarif_diagnostics_produce_the_same_annotation(self) -> None:
        """format and annotations are orthogonal: both parsers agree."""
        self.write_stub("blockwatch", stderr=json.dumps(SARIF), exit_code=1)

        result = self.run_action(INPUT_FORMAT="sarif")

        self.assertEqual(len(result.annotations), 1, result.stdout)
        annotation = result.annotations[0]
        self.assertEqual(annotation.level, "error")
        self.assertEqual(annotation.properties["file"], "docs/cli.md")
        self.assertEqual(annotation.properties["line"], "12")
        self.assertIn("docs/cli.md:cli-docs:keep-sorted:35385fe8", annotation.message)

    # ----------------------------------------------------------------- 7
    def test_annotations_can_be_turned_off_without_affecting_the_verdict(self) -> None:
        result = self.run_action(INPUT_ANNOTATIONS="false", INPUT_SUMMARY="false")

        self.assertEqual(result.annotations, [])
        self.assertEqual(result.exit_code, 1)
        # The diagnostics still reach the log; only the reporting is off.
        self.assertIn("keep-sorted", result.stderr)

    # ----------------------------------------------------------------- 8
    def test_globs_are_passed_last_and_list_inputs_are_split(self) -> None:
        """Globs are positional: anything appended after them changes their meaning."""
        result = self.run_action(
            INPUT_GLOBS="**/*.md, **/*.yml",
            INPUT_IGNORE="target/**\ndist/**",
            INPUT_ENABLE="keep-sorted",
        )

        argv = result.argv("blockwatch")[0]
        self.assertEqual(argv[-2:], ["**/*.md", "**/*.yml"])
        self.assertEqual(argv.count("--ignore"), 2)
        self.assertIn("dist/**", argv)
        self.assertEqual(argv[argv.index("-e") + 1], "keep-sorted")
        # The suppression file the step builds is one more --suppress-from, and
        # it has to land before the positional globs.
        self.assertLess(argv.index("--suppress-from"), argv.index("**/*.md"))

    # ----------------------------------------------------------------- 9
    def test_push_to_a_new_branch_diffs_with_diff_tree(self) -> None:
        """`git diff --root` silently compares the working tree — an empty patch."""
        event = self.write_event(before="0" * 40)

        result = self.run_action(event=event)

        diff = [call for call in result.argv("git") if call[0] in ("diff", "diff-tree")][0]
        self.assertEqual(diff[0], "diff-tree")
        for flag in ("--root", "-m", "--first-parent", "--no-commit-id"):
            self.assertIn(flag, diff)

    # ---------------------------------------------------------------- 10
    def test_invalid_boolean_input_fails_the_step(self) -> None:
        """Reading `yes` as false would turn reporting off without a word."""
        result = self.run_action(INPUT_ANNOTATIONS="yes")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("annotations", result.stderr)
        self.assertEqual(result.argv("blockwatch"), [])


def _by_definition_order(first: str, second: str) -> int:
    """Order test methods as they are written, which here is by importance.

    Setting `sortTestMethodsUsing = None` does not do this: unittest builds its
    list from `dir()`, which is alphabetical already, so the names have to be
    compared by the line they are defined on.
    """
    lines: tuple[int, int] = (
        getattr(RunTest, first).__code__.co_firstlineno,
        getattr(RunTest, second).__code__.co_firstlineno,
    )
    return (lines[0] > lines[1]) - (lines[0] < lines[1])


if __name__ == "__main__":
    # The most critical failure should be the first one reported. pytest runs in
    # definition order already; this is for `python3 scripts/run_test.py`.
    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = _by_definition_order
    unittest.main(testLoader=loader, verbosity=2)
