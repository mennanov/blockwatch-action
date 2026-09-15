#!/usr/bin/env python3
"""Black-box tests for run.py: what the action does with blockwatch's output.

Real repository, real blockwatch, but the assertions are only about run.py —
annotations, exit code, input handling. blockwatch's own contract (columns,
address format, message wording) is read back from the diagnostics it emitted,
never hardcoded, so a change there is not a failure here. The one exception is
the half-open range convention, which run.py exists to translate: the column
test asserts the conversion, so a blockwatch that stopped reporting an
exclusive end would rightly fail it. `blockwatch` must be on PATH.

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

# A sorted block, and the same block with its entries swapped.
SORTED_FRUIT = '<!-- <block name="fruit" keep-sorted> -->\n- apple\n- banana\n<!-- </block> -->\n'
UNSORTED_FRUIT = '<!-- <block name="fruit" keep-sorted> -->\n- banana\n- apple\n<!-- </block> -->\n'
SORTED_VEG = '<!-- <block name="veg" keep-sorted> -->\n- carrot\n- potato\n<!-- </block> -->\n'
UNSORTED_VEG = '<!-- <block name="veg" keep-sorted> -->\n- potato\n- carrot\n<!-- </block> -->\n'


def setUpModule() -> None:
    if shutil.which("blockwatch") is None:
        raise RuntimeError(
            "blockwatch is not on PATH. These tests feed the action real diagnostics "
            "rather than a frozen copy of them. Install it with: cargo binstall blockwatch"
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
    def diagnostics(self) -> JsonObject:
        """What blockwatch reported, as the script replayed it to the log.

        Comparing the annotations against this is what keeps these tests about
        the translation rather than about blockwatch's numbers.
        """
        document: JsonObject = json.loads(self.stderr[self.stderr.index("{") :])
        return document


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
    def test_annotation_mirrors_the_diagnostic_it_came_from(self) -> None:
        self.break_fruit()

        result = self.run_action()

        ((path, entries),) = result.diagnostics.items()
        entry = entries[0]
        self.assertEqual(len(result.annotations), 1, result.stdout)
        annotation = result.annotations[0]
        self.assertEqual(annotation.level, "error")
        self.assertEqual(annotation.properties["file"], path)
        self.assertEqual(annotation.properties["line"], str(entry["range"]["start"]["line"]))
        self.assertEqual(annotation.properties["endLine"], str(entry["range"]["end"]["line"]))
        self.assertEqual(annotation.properties["col"], str(entry["range"]["start"]["character"]))
        # blockwatch reports a half-open range but GitHub's endColumn is inclusive.
        self.assertEqual(
            annotation.properties["endColumn"], str(entry["range"]["end"]["character"] - 1)
        )
        self.assertEqual(annotation.properties["title"], "blockwatch%3A " + entry["code"])
        self.assertIn(entry["message"], annotation.message)
        # The address is the only part a reader can act on, and it appears
        # nowhere else on the line.
        self.assertEqual(annotation.address, entry["address"])

    def test_exit_code_from_blockwatch_is_propagated(self) -> None:
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

    def test_commit_message_can_suppress_a_violation_it_introduces(self) -> None:
        self.break_fruit("break the order\n\nBlockwatch-suppress: docs/a.md\n")

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["notice"])
        # This changes the outcome while appearing nowhere in the workflow file,
        # so the log has to say it happened.
        self.assertIn("suppression addresses found", result.stdout)

    def test_sarif_and_json_produce_identical_annotations(self) -> None:
        self.break_fruit()

        as_json = self.run_action()
        as_sarif = self.run_action(INPUT_FORMAT="sarif")

        def rendered(result: Result) -> list[tuple[str, dict[str, str], str]]:
            return [(a.level, a.properties, a.message) for a in result.annotations]

        self.assertTrue(rendered(as_json))
        self.assertEqual(rendered(as_json), rendered(as_sarif))

    def test_annotations_limit_caps_output_and_reports_the_remainder(self) -> None:
        self.write("docs/a.md", UNSORTED_FRUIT)
        self.write("docs/b.md", UNSORTED_VEG)
        self.commit("break both")

        result = self.run_action(INPUT_ANNOTATIONS_LIMIT="1")

        self.assertEqual(result.levels, ["error", "notice"])
        self.assertIn("1 further violation(s) were not annotated", result.annotations[1].message)

    def test_annotations_can_be_turned_off_without_affecting_the_verdict(self) -> None:
        self.break_fruit()

        result = self.run_action(INPUT_ANNOTATIONS="false", INPUT_SUMMARY="false")

        self.assertEqual(result.annotations, [])
        self.assertEqual(result.exit_code, 1)
        # The diagnostics still reach the log; only the reporting is off.
        self.assertIn("keep-sorted", result.stderr)

    def test_output_that_is_not_diagnostics_is_replayed_but_not_annotated(self) -> None:
        self.break_fruit()

        # The pathspec excludes everything the commit touched, so git produces no
        # patch and blockwatch answers with a plain-text usage error instead of
        # a document. Whatever it says has to reach the log unparsed.
        result = self.run_action(INPUT_DIFF_PATHSPEC=":(exclude)docs/**")

        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(result.annotations, [])

    def test_push_to_a_new_branch_still_produces_a_diff(self) -> None:
        self.break_fruit()

        result = self.run_action(event={"before": "0" * 40})

        # `git diff --root` would silently compare the working tree and hand
        # blockwatch an empty patch; diff-tree is what makes this reach a check.
        self.assertEqual(result.levels, ["error"])

    def test_invalid_boolean_input_fails_the_step(self) -> None:
        self.break_fruit()

        result = self.run_action(INPUT_ANNOTATIONS="yes")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("annotations", result.stderr)
        self.assertEqual(result.annotations, [])


if __name__ == "__main__":
    loader = unittest.TestLoader()
    unittest.main(testLoader=loader, verbosity=2)
