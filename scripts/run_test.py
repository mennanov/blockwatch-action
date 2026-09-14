#!/usr/bin/env python3
"""Black-box tests for run.py, against a real git repository and a real blockwatch.

Nothing is stubbed, so these fail if blockwatch changes the shape of what it
reports — line and column base, address format, the severity of a suppressed
violation — instead of passing against a frozen copy of output. `blockwatch`
must be on PATH.

python3 scripts/run_test.py          # or: python3 -m pytest scripts/run_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict

RUN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
JsonObject = Dict[str, Any]

# A sorted block, and the same block with its entries swapped. The violating
# line is line 3 in both files; `- apple` is 7 characters, `- carrot` is 8.
SORTED_FRUIT = '<!-- <block name="fruit" keep-sorted> -->\n- apple\n- banana\n<!-- </block> -->\n'
UNSORTED_FRUIT = '<!-- <block name="fruit" keep-sorted> -->\n- banana\n- apple\n<!-- </block> -->\n'
SORTED_VEG = '<!-- <block name="veg" keep-sorted> -->\n- carrot\n- potato\n<!-- </block> -->\n'
UNSORTED_VEG = '<!-- <block name="veg" keep-sorted> -->\n- potato\n- carrot\n<!-- </block> -->\n'

VIOLATING_LINE = "3"
FRUIT_ADDRESS_PREFIX = "docs/a.md:fruit:keep-sorted:"


def setUpModule() -> None:
    if shutil.which("blockwatch") is None:
        raise RuntimeError(
            "blockwatch is not on PATH. These tests run the real binary, so that a "
            "change to its output format fails here rather than in production. "
            "Install it with: cargo binstall blockwatch"
        )


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

    @property
    def address(self) -> str:
        """The suppression address the annotation offers, if it carries one."""
        _, _, address = self.message.partition("Suppress with: Blockwatch-suppress: ")
        return address

    def __repr__(self) -> str:
        return "Annotation(%s, %r, %r)" % (self.level, self.properties, self.message)


class Result:
    def __init__(self, process: subprocess.CompletedProcess[str]) -> None:
        self.exit_code = process.returncode
        self.stdout = process.stdout
        self.stderr = process.stderr

    @property
    def annotations(self) -> list[Annotation]:
        return [Annotation(line) for line in self.stdout.splitlines() if line.startswith("::")]

    @property
    def levels(self) -> list[str]:
        return [a.level for a in self.annotations]

    @property
    def annotated_files(self) -> set[str]:
        return {a.properties["file"] for a in self.annotations if "file" in a.properties}


class ActionTestCase(unittest.TestCase):
    """A throwaway repository, plus a way to run the script against it."""

    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="blockwatch-action-test-")
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.repo = os.path.join(self.workspace, "repo")
        os.makedirs(os.path.join(self.repo, "docs"))
        self.summary_path = os.path.join(self.workspace, "summary.md")

        # HOME points into the workspace and the system config is ignored, so no
        # global git setting can change what these tests see.
        self.git_env = dict(os.environ)
        self.git_env.update(HOME=self.workspace, GIT_CONFIG_NOSYSTEM="1")

        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        self.write("docs/a.md", SORTED_FRUIT)
        self.write("docs/b.md", SORTED_VEG)
        self.write("docs/notes.md", "Prose with no blocks in it.\n")
        self.base_sha = self.commit("initial")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=self.repo,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def write(self, relative_path: str, content: str) -> None:
        with open(os.path.join(self.repo, relative_path), "w", encoding="utf-8") as handle:
            handle.write(content)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def break_fruit(self, message: str = "break the fruit order") -> str:
        self.write("docs/a.md", UNSORTED_FRUIT)
        return self.commit(message)

    def run_action(self, event: JsonObject | None = None, **inputs: str) -> Result:
        event_path = os.path.join(self.workspace, "event.json")
        with open(event_path, "w", encoding="utf-8") as handle:
            json.dump(event if event is not None else {"before": self.base_sha}, handle)

        environment = dict(self.git_env)
        environment.update(
            GITHUB_EVENT_NAME="push",
            GITHUB_SHA=self.git("rev-parse", "HEAD"),
            GITHUB_STEP_SUMMARY=self.summary_path,
            GITHUB_EVENT_PATH=event_path,
            # Empty on purpose: with no token the script never reaches the network.
            GH_TOKEN="",
        )
        # Inherited INPUT_* would leak a developer's shell into the run.
        for name in [n for n in environment if n.startswith("INPUT_")]:
            del environment[name]
        environment.update(inputs)

        process = subprocess.run(
            [sys.executable, RUN_PY],
            env=environment,
            capture_output=True,
            text=True,
            cwd=self.repo,
        )
        return Result(process)


class RunTest(ActionTestCase):
    def test_violation_becomes_an_error_annotation_carrying_its_address(self) -> None:
        self.break_fruit()

        result = self.run_action()

        self.assertEqual(len(result.annotations), 1, result.stdout)
        annotation = result.annotations[0]
        self.assertEqual(annotation.level, "error")
        self.assertEqual(annotation.properties["file"], "docs/a.md")
        self.assertEqual(annotation.properties["line"], VIOLATING_LINE)
        self.assertEqual(annotation.properties["endLine"], VIOLATING_LINE)
        # 1-based columns with an inclusive end, which is what GitHub wants.
        self.assertEqual(annotation.properties["col"], "1")
        self.assertEqual(annotation.properties["endColumn"], "7")
        self.assertEqual(annotation.properties["title"], "blockwatch%3A keep-sorted")
        self.assertIn("out-of-order line 3", annotation.message)
        self.assertTrue(
            annotation.address.startswith(FRUIT_ADDRESS_PREFIX), annotation.message
        )
        self.assertTrue(annotation.address[len(FRUIT_ADDRESS_PREFIX):], "address has no hash")

    def test_exit_code_is_blockwatch_s_own(self) -> None:
        self.break_fruit()
        self.assertEqual(self.run_action().exit_code, 1)

        # A commit that changes something without breaking a block: the diff is
        # real, so blockwatch runs and finds nothing.
        self.write("docs/a.md", SORTED_FRUIT)
        self.write("docs/notes.md", "Prose, revised.\n")
        self.commit("restore the order")

        clean = self.run_action()
        self.assertEqual(clean.exit_code, 0)
        self.assertEqual(clean.annotations, [])

    def test_suppressed_violation_becomes_a_notice_annotation(self) -> None:
        self.break_fruit()

        result = self.run_action(INPUT_SUPPRESS="docs/a.md")

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["notice"])

    def test_commit_message_can_suppress_a_violation_it_introduces(self) -> None:
        self.break_fruit("break the order\n\nBlockwatch-suppress: docs/a.md\n")

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["notice"])
        # This changes the outcome while appearing nowhere in the workflow file,
        # so the log has to say it happened.
        self.assertIn("suppression addresses found", result.stdout)

    def test_empty_diff_fails_the_step_without_annotating(self) -> None:
        self.break_fruit()

        # The pathspec excludes everything the commit touched, so git produces no
        # patch and blockwatch refuses to guess.
        result = self.run_action(INPUT_DIFF_PATHSPEC=":(exclude)docs/**")

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("diff in stdin is empty", result.stderr)
        self.assertEqual(result.annotations, [])

    def test_annotations_limit_caps_output_and_reports_the_remainder(self) -> None:
        self.write("docs/a.md", UNSORTED_FRUIT)
        self.write("docs/b.md", UNSORTED_VEG)
        self.commit("break both")

        result = self.run_action(INPUT_ANNOTATIONS_LIMIT="1")

        self.assertEqual(result.levels, ["error", "notice"])
        self.assertIn("1 further violation(s) were not annotated", result.annotations[1].message)

    def test_sarif_diagnostics_produce_the_same_annotation(self) -> None:
        self.break_fruit()

        result = self.run_action(INPUT_FORMAT="sarif")

        self.assertEqual(len(result.annotations), 1, result.stdout)
        annotation = result.annotations[0]
        self.assertEqual(annotation.level, "error")
        self.assertEqual(annotation.properties["file"], "docs/a.md")
        self.assertEqual(annotation.properties["line"], VIOLATING_LINE)
        self.assertEqual(annotation.properties["col"], "1")
        self.assertEqual(annotation.properties["endColumn"], "7")
        self.assertTrue(annotation.address.startswith(FRUIT_ADDRESS_PREFIX), annotation.message)

    def test_annotations_can_be_turned_off_without_affecting_the_verdict(self) -> None:
        self.break_fruit()

        result = self.run_action(INPUT_ANNOTATIONS="false", INPUT_SUMMARY="false")

        self.assertEqual(result.annotations, [])
        self.assertEqual(result.exit_code, 1)
        # The diagnostics still reach the log; only the reporting is off.
        self.assertIn("keep-sorted", result.stderr)

    def test_globs_narrow_the_run(self) -> None:
        self.write("docs/a.md", UNSORTED_FRUIT)
        self.write("docs/b.md", UNSORTED_VEG)
        self.commit("break both")

        # Globs are positional. verbosity and the suppression file are appended
        # before them; if that order inverts, blockwatch reads a flag's value as
        # a path and this narrowing stops working.
        result = self.run_action(INPUT_GLOBS="docs/a.md", INPUT_VERBOSITY="summary")

        self.assertEqual(result.annotated_files, {"docs/a.md"})

    def test_push_to_a_new_branch_still_produces_a_diff(self) -> None:
        self.break_fruit()

        result = self.run_action(event={"before": "0" * 40})

        # `git diff --root` would silently compare the working tree and hand
        # blockwatch an empty patch; diff-tree is what makes this reach a check.
        self.assertEqual(result.levels, ["error"])
        self.assertNotIn("diff in stdin is empty", result.stderr)

    def test_invalid_boolean_input_fails_the_step(self) -> None:
        self.break_fruit()

        result = self.run_action(INPUT_ANNOTATIONS="yes")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("annotations", result.stderr)
        self.assertEqual(result.annotations, [])


if __name__ == "__main__":
    loader = unittest.TestLoader()
    unittest.main(testLoader=loader, verbosity=2)
