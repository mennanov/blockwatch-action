"""Core action behavior tests: diagnostics, exit codes, git diff topologies, and formats."""

from __future__ import annotations

from .harness import (
    SORTED_FRUIT,
    UNSORTED_FRUIT,
    UNSORTED_VEG,
    ActionTestCase,
    ensure_pinned_blockwatch,
)


def setUpModule() -> None:
    ensure_pinned_blockwatch()


class CoreTest(ActionTestCase):
    """Specification of core blockwatch-action diffing, formatting, and exit-code behavior."""

    def test_annotation_mirrors_the_diagnostic_it_came_from(self) -> None:
        """Error annotations mirror diagnostic path, inclusive endColumn, code, and address."""
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
        self.assertEqual(annotation.address, entry["address"])

    def test_exit_code_from_blockwatch_is_propagated(self) -> None:
        """The action exits 1 when violations exist and 0 once the diff fixes them."""
        self.break_fruit()
        self.assertEqual(self.run_action().exit_code, 1)

        self.write("docs/a.md", SORTED_FRUIT)
        self.write("docs/notes.md", "Prose, revised.\n")
        self.commit("restore the order")

        clean = self.run_action()
        self.assertEqual(clean.exit_code, 0)
        self.assertEqual(clean.annotations, [])

    def test_failed_diff_fails_the_step_without_running_blockwatch(self) -> None:
        """A failing git diff aborts the step before running blockwatch or scanning the tree."""
        self.break_fruit()

        result = self.run_action(
            event={"pull_request": {"number": 1}},
            event_name="pull_request",
            GITHUB_BASE_REF="no-such-branch",
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no diff to check", result.stderr)
        self.assertEqual(result.annotations, [])

    def test_empty_diff_counts_as_no_change(self) -> None:
        """An empty diff marks nothing as changed while keeping existing violations unscoped."""
        self.break_fruit()
        before = self.git("rev-parse", "HEAD")
        self.git("commit", "-q", "--allow-empty", "-m", "retrigger CI")

        result = self.run_action(event={"before": before})

        self.assertIn("diff for this event is empty", result.stdout)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.levels, ["error"])

        only_changed = self.run_action(event={"before": before}, INPUT_ONLY_CHANGED="true")
        self.assertEqual(only_changed.exit_code, 0)
        self.assertEqual(only_changed.annotations, [])

    def test_sarif_and_json_produce_identical_annotations(self) -> None:
        """SARIF and JSON formats produce identical GitHub annotations."""
        self.break_fruit()

        as_json = self.run_action()
        as_sarif = self.run_action(INPUT_FORMAT="sarif")

        self.assertTrue(as_json.annotations)
        self.assertEqual(as_sarif.annotations, as_json.annotations)

    def test_annotations_limit_caps_output_and_reports_the_remainder(self) -> None:
        """annotations_limit caps violation cards and emits a notice counting omitted violations."""
        self.break_both()

        result = self.run_action(INPUT_ANNOTATIONS_LIMIT="1")

        self.assertEqual(result.levels, ["error", "notice"])
        self.assertIn("1 further violation(s) were not annotated", result.annotations[1].message)

    def test_annotations_can_be_turned_off_without_affecting_the_verdict(self) -> None:
        """Disabling annotations and summary suppresses output without altering verdict or logs."""
        self.break_fruit()

        result = self.run_action(INPUT_ANNOTATIONS="false", INPUT_SUMMARY="false")

        self.assertEqual(result.annotations, [])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("keep-sorted", result.stderr)

    def test_a_run_that_cannot_finish_is_annotated_with_blockwatchs_error(self) -> None:
        """A runtime failure exits 2 and emits one fileless error annotation with raw stderr."""
        self.break_fruit()

        result = self.run_action(INPUT_SUPPRESS_FROM="no-such-file.txt")

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.levels, ["error"])
        (annotation,) = result.annotations
        self.assertNotIn("file", annotation.properties)
        message = annotation.message.replace("%0A", "\n").replace("%0D", "\r")
        self.assertEqual(message.replace("%25", "%"), result.stderr.strip())

    def test_push_to_a_new_branch_still_produces_a_diff(self) -> None:
        """An all-zero push base still computes a non-empty diff for the head commit."""
        self.break_fruit()

        result = self.run_action(event={"before": "0" * 40})

        self.assertNotIn("diff for this event is empty", result.stdout)
        self.assertEqual(result.levels, ["error"])

    def test_force_push_checks_the_head_commit_and_reads_its_message(self) -> None:
        """A force-push whose prior tip is missing from the clone falls back to the head commit."""
        self.break_fruit("break the order\n\nblockwatch-suppress: docs/a.md\n")

        result = self.run_action(event={"before": "f" * 40, "forced": True})

        self.assertNotIn("diff for this event is empty", result.stdout)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning", "warning"])

    def test_merge_at_a_new_branch_head_is_diffed_against_its_first_parent(self) -> None:
        """A merge commit at a new branch head diffs against its first parent only."""
        self.git("checkout", "-q", "-b", "side")
        self.write("docs/b.md", UNSORTED_VEG)
        self.commit("break the veg order")
        self.git("checkout", "-q", "-")
        self.break_fruit()
        self.git("merge", "-q", "--no-edit", "side")

        result = self.run_action(event={"before": "0" * 40}, INPUT_ONLY_CHANGED="true")

        self.assertEqual(list(result.diagnostics), ["docs/b.md"])

    def test_invalid_boolean_input_fails_the_step(self) -> None:
        """Invalid boolean inputs fail fast before running git or blockwatch."""
        self.break_fruit()

        result = self.run_action(INPUT_ANNOTATIONS="yes")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("annotations", result.stderr)
        self.assertEqual(result.annotations, [])

    def test_diverged_base_branch_on_pull_request_ignores_base_suppressions_and_changes(
        self,
    ) -> None:
        """A PR ignores commits and suppressions added to origin/<base> after the branch forked."""
        default_branch = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.git("checkout", "-q", "-b", "pr-branch")
        self.break_fruit("pr breaks fruit without suppressing")

        self.git("checkout", "-q", default_branch)
        self.write("docs/b.md", UNSORTED_VEG)
        base_tip = self.commit("main advances\n\nblockwatch-suppress: docs/a.md\n")
        self.git("update-ref", "refs/remotes/origin/main", base_tip)
        self.git("checkout", "-q", "pr-branch")

        result = self.run_pull_request("", sync_base=False, INPUT_ONLY_CHANGED="true")

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(list(result.diagnostics), ["docs/a.md"])
        self.assertEqual(result.levels, ["error"])
        self.assertEqual(result.json_output("commit_suppressions"), [])
        self.assertEqual(result.json_output("description_suppressions"), [])

    def test_diff_pathspec_excludes_files_from_diff_and_only_changed(self) -> None:
        """diff_pathspec forwards pathspec exclusions so omitted files skip changed-only runs."""
        self.break_both()

        result = self.run_action(
            INPUT_ONLY_CHANGED="true",
            INPUT_DIFF_PATHSPEC=":(exclude)docs/a.md",
        )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(list(result.diagnostics), ["docs/b.md"])
        self.assertEqual(result.levels, ["error"])

    def test_file_path_with_comma_is_properly_escaped_and_suppressed_by_commit(self) -> None:
        """Comma paths are properly escaped in annotations and suppressible by commit."""
        self.write("docs/a,b.md", UNSORTED_FRUIT)
        self.commit("break file containing comma")

        unsuppressed = self.run_action()
        self.assertEqual(unsuppressed.exit_code, 1)
        self.assertEqual(unsuppressed.annotations[0].properties["file"], "docs/a%2Cb.md")

        self.commit_notes(
            "suppress comma file\n\nblockwatch-suppress: docs/a,b.md\n", content="note\n"
        )
        suppressed = self.run_action(event={"before": self.base_sha})
        self.assertEqual(suppressed.exit_code, 0)
        self.assertEqual(suppressed.levels, ["warning", "warning"])
        self.assertIn("[matched 1]", suppressed.annotations[0].message)
        self.assertEqual(len(suppressed.json_output("commit_suppressions")), 1)
