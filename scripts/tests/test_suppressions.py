"""Behavioral specifications for blockwatch-suppress directives, policies, and reporting."""

from __future__ import annotations

import os
import re

from .harness import UNSORTED_FRUIT, UNSORTED_VEG, ActionTestCase, ensure_pinned_blockwatch


def setUpModule() -> None:
    ensure_pinned_blockwatch()


class SuppressionDiscoveryTest(ActionTestCase):
    """How suppression directives are discovered by git/PR scope and matched against violations."""

    def test_commit_message_suppresses_violation_and_warns(self) -> None:
        """A commit-message directive suppresses its violation and warns reviewers."""
        self.break_fruit("break the order\n\nblockwatch-suppress: docs/a.md\n")

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning", "warning"])
        self.assertIn("blockwatch-suppress: docs/a.md", result.annotations[0].message)
        self.assertIn("Suppressed by: commit message", result.annotations[1].message)
        self.assertIn("suppression addresses found", result.stdout)

    def test_pull_request_description_suppresses_violation_and_warns(self) -> None:
        """A blockwatch-suppress line in a PR description suppresses the violation and warns."""
        self.break_fruit()

        result = self.run_pull_request("blockwatch-suppress: docs/a.md")

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning", "warning"])
        self.assertIn("Suppressed by: pull request description", result.annotations[1].message)

    def test_same_violation_suppressed_by_both_commit_and_description(self) -> None:
        """When both commit and PR directives match one violation, both sources are credited."""
        self.break_fruit("suppress in commit\n\nblockwatch-suppress: docs/a.md:fruit\n")

        result = self.run_pull_request("Also in PR description\n\nblockwatch-suppress: docs/a.md")

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning", "warning", "warning"])
        self.assertIn("[matched 1]", result.annotations[0].message)
        self.assertIn("[matched 1]", result.annotations[1].message)
        self.assertIn(
            "Suppressed by: commit message, pull request description", result.annotations[2].message
        )
        self.assertIn("(suppressed via commits, description)", result.summary)
        self.assertEqual(len(result.json_output("commit_suppressions")), 1)
        self.assertEqual(len(result.json_output("description_suppressions")), 1)

    def test_multi_commit_range_collects_multiline_and_case_insensitive_directives(self) -> None:
        """All commits in range are scanned across multi-line and case-insensitive directives."""
        self.write("docs/a.md", UNSORTED_FRUIT)
        sha1 = self.commit(
            "first change\n\nParagraph before directive.\n"
            "  BLOCKWATCH-SUPPRESS: docs/a.md:fruit\n\nTrailing notes."
        )
        self.write("docs/b.md", UNSORTED_VEG)
        sha2 = self.commit("second change\n\nRationale\nblockwatch-suppress: docs/b.md:veg\n")

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning", "warning", "warning"])
        directive_msg = result.annotations[0].message
        self.assertIn(
            "commit %s (first change):   BLOCKWATCH-SUPPRESS: docs/a.md:fruit [matched 1]"
            % sha1[:7],
            directive_msg,
        )
        self.assertIn(
            "commit %s (second change): blockwatch-suppress: docs/b.md:veg [matched 1]" % sha2[:7],
            directive_msg,
        )
        self.assertEqual(
            [
                (item["commit"], item["subject"], item["address"])
                for item in result.json_output("commit_suppressions")
            ],
            [
                (sha2[:7], "second change", "docs/b.md:veg"),
                (sha1[:7], "first change", "docs/a.md:fruit"),
            ],
        )

    def test_validator_segment_matching_targets_only_matching_blocks_and_validators(self) -> None:
        """Directives match on block and validator segments without crediting unrelated items."""
        self.break_both(
            "partial suppressions\n\n"
            "blockwatch-suppress: docs/a.md:fruit:keep-sorted\n"
            "blockwatch-suppress: docs/a.md:veg\n"
            "blockwatch-suppress: docs/a.md:fruit:line-count\n"
        )

        result = self.run_action()

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.levels, ["warning", "warning", "error"])
        directive_msg = result.annotations[0].message
        self.assertIn("blockwatch-suppress: docs/a.md:fruit:keep-sorted [matched 1]", directive_msg)
        self.assertIn("blockwatch-suppress: docs/a.md:veg [matched 0]", directive_msg)
        self.assertIn("blockwatch-suppress: docs/a.md:fruit:line-count [matched 0]", directive_msg)
        self.assertIn("Suppressed by: commit message", result.annotations[1].message)
        self.assertEqual(result.annotations[1].properties["file"], "docs/a.md")
        self.assertEqual(result.annotations[2].properties["file"], "docs/b.md")

    def test_unnamed_block_suppresses_by_normalized_path_without_address_suggestion(self) -> None:
        """File-level directives match normalized paths on unnamed blocks and omit address hints."""
        self.write("docs/unnamed.md", "<!-- <block keep-sorted> -->\n- z\n- a\n<!-- </block> -->\n")
        self.commit("break unnamed\n\nblockwatch-suppress: ./docs/unnamed.md\n")

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning", "warning"])
        self.assertIn("[matched 1]", result.annotations[0].message)
        self.assertIn("Suppressed by: commit message", result.annotations[1].message)
        self.assertNotIn("Suppress with:", result.annotations[1].message)
        self.assertTrue(result.json_output("commit_suppressions")[0]["applied"])

    def test_workflow_inputs_stay_notices_without_suppression_attempts(self) -> None:
        """Workflow `suppress` and `suppress_from` suppressions stay notices with no attempts."""
        self.break_both("break both without commit suppressions")
        suppress_file = self.write_workspace_file(
            "suppressions.txt", "blockwatch-suppress: docs/b.md\n"
        )

        result = self.run_action(INPUT_SUPPRESS="docs/a.md", INPUT_SUPPRESS_FROM=suppress_file)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["notice", "notice"])
        for annotation in result.annotations:
            self.assertNotIn("Suppressed by:", annotation.message)
        self.assertNotIn("suppression directive", result.summary)
        self.assertIn("(suppressed)", result.summary)
        self.assertNotIn("(suppressed via", result.summary)
        self.assertEqual(
            result.outputs,
            {"commit_suppressions": "[]", "description_suppressions": "[]"},
        )

    def test_mixed_commit_and_workflow_suppressions_report_separate_sources(self) -> None:
        """Commit-suppressed violations warn while sibling workflow ones stay notices."""
        self.break_both("break both\n\nblockwatch-suppress: docs/a.md:fruit\n")

        result = self.run_action(INPUT_SUPPRESS="docs/b.md")

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning", "warning", "notice"])
        self.assertEqual(result.annotations[1].properties["file"], "docs/a.md")
        self.assertIn("Suppressed by: commit message", result.annotations[1].message)
        self.assertEqual(result.annotations[2].properties["file"], "docs/b.md")
        self.assertNotIn("Suppressed by:", result.annotations[2].message)
        self.assertIn("(suppressed via commits)", result.summary)
        self.assertIn(" (suppressed) |", result.summary)
        self.assertEqual(len(result.json_output("commit_suppressions")), 1)
        self.assertEqual(result.json_output("description_suppressions"), [])

    def test_unused_directive_still_emits_warning_and_summary(self) -> None:
        """An enabled directive matching zero violations still warns and records an attempt."""
        self.commit_notes("touch notes\n\nblockwatch-suppress: docs/b.md\n")

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning"])
        self.assertIn("[matched 0]", result.annotations[0].message)
        self.assertIn("applied (matched 0 violations)", result.summary)
        self.assertEqual(len(result.json_output("commit_suppressions")), 1)
        self.assertEqual(result.json_output("description_suppressions"), [])

    def test_prose_and_blockquotes_are_not_treated_as_directives(self) -> None:
        """Blockquotes and inline prose mentions of blockwatch-suppress: do not trigger attempts."""
        self.break_fruit(
            "explain suppression syntax\n\n"
            "> blockwatch-suppress: docs/a.md\n"
            "Do not use blockwatch-suppress: docs/a.md here.\n"
        )
        body = (
            "Review note:\n\n"
            "> blockwatch-suppress: docs/a.md\n"
            "Mentioning `blockwatch-suppress: docs/a.md` in prose does not count.\n"
        )

        result = self.run_pull_request(body)

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.levels, ["error"])
        self.assertNotIn("suppression directive", result.summary)
        self.assertEqual(result.json_output("commit_suppressions"), [])
        self.assertEqual(result.json_output("description_suppressions"), [])

    def test_commits_outside_push_range_cannot_suppress(self) -> None:
        """A suppression commit before `before` does not affect the current push."""
        prior = self.commit_notes("earlier commit\n\nblockwatch-suppress: docs/a.md\n")
        self.break_fruit("later push without suppression")

        result = self.run_action(event={"before": prior})

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.levels, ["error"])
        self.assertEqual(result.json_output("commit_suppressions"), [])

    def test_workflow_dispatch_reads_head_commit_suppression_when_enabled(self) -> None:
        """Events without a diff range (`workflow_dispatch`) inspect HEAD for directives."""
        self.break_fruit("head suppression\n\nblockwatch-suppress: docs/a.md\n")

        with self.subTest("commits enabled"):
            enabled = self.run_action(event={}, event_name="workflow_dispatch")
            self.assertEqual(enabled.exit_code, 0)
            self.assertEqual(enabled.levels, ["warning", "warning"])
            self.assertIs(enabled.json_output("commit_suppressions")[0]["applied"], True)

        with self.subTest("commits disabled"):
            disabled = self.run_action(
                event={}, event_name="workflow_dispatch", INPUT_SUPPRESS_FROM_COMMITS="false"
            )
            self.assertEqual(disabled.exit_code, 1)
            self.assertEqual(disabled.levels, ["notice", "error"])
            self.assertIs(disabled.json_output("commit_suppressions")[0]["applied"], False)

    def test_temporary_suppression_file_is_cleaned_up_after_normal_and_failed_runs(self) -> None:
        """Temporary suppression files created for blockwatch are deleted after exit or failure."""
        self.break_fruit("break with suppress\n\nblockwatch-suppress: docs/a.md\n")

        normal = self.run_action()
        self.assertEqual(normal.exit_code, 0)
        self.assertEqual(os.listdir(self.temp_dir), [])

        failed_diff = self.run_pull_request(
            "blockwatch-suppress: docs/a.md",
            base_ref="no-such-branch",
            sync_base=False,
        )
        self.assertEqual(failed_diff.exit_code, 1)
        self.assertEqual(os.listdir(self.temp_dir), [])


class SuppressionPolicyTest(ActionTestCase):
    """How suppress_from_commits and suppress_from_description control enforcement and hints."""

    def test_disabling_commit_suppressions_on_push_rejects_directive_and_drops_commit_hint(
        self,
    ) -> None:
        """On push with commits disabled, directives emit notices and hints point to `suppress`."""
        self.break_fruit("break the order\n\nblockwatch-suppress: docs/a.md\n")

        result = self.run_action(INPUT_SUPPRESS_FROM_COMMITS="false")

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.levels, ["notice", "error"])
        self.assertIn("ignored", result.annotations[0].message)
        self.assertIn("were ignored", result.stdout)
        self.assertIn("blockwatch-suppress: docs/a.md", result.stdout)
        ((_, entries),) = result.diagnostics.items()
        violation_annotation = result.annotations[1]
        self.assertNotIn("blockwatch-suppress:", violation_annotation.message)
        self.assertIn(entries[0]["address"], violation_annotation.message)

    def test_disabling_description_suppressions_ignores_pr_body_while_keeping_commit_hint(
        self,
    ) -> None:
        """With PR description suppressions off, body directives are ignored while commits apply."""
        self.break_fruit()
        body = "blockwatch-suppress: docs/a.md"

        ignored = self.run_pull_request(body, INPUT_SUPPRESS_FROM_DESCRIPTION="false")

        self.assertEqual(ignored.exit_code, 1)
        self.assertEqual(ignored.levels, ["warning", "error"])
        self.assertIn("ignored", ignored.annotations[0].message)
        self.assertIn("description not read", ignored.stdout)
        ((_, entries),) = ignored.diagnostics.items()
        self.assertEqual(ignored.annotations[1].address, entries[0]["address"])

    def test_pull_request_with_mixed_flags_enforces_only_enabled_source(self) -> None:
        """A PR applies only enabled sources, warning while any source remains active."""
        self.break_both("attempt commit suppress\n\nblockwatch-suppress: docs/a.md\n")
        body = "PR suppression\n\nblockwatch-suppress: docs/b.md"

        with self.subTest("commits disabled, description enabled"):
            desc_only = self.run_pull_request(
                body, INPUT_SUPPRESS_FROM_COMMITS="false", INPUT_SUPPRESS_FROM_DESCRIPTION="true"
            )
            self.assertEqual(desc_only.exit_code, 1)
            self.assertEqual(desc_only.levels, ["warning", "warning", "error", "warning"])
            self.assertIn("suppress_from_commits is false", desc_only.annotations[0].message)
            self.assertIn("[matched 1]", desc_only.annotations[1].message)
            self.assertIn(
                "Suppress with: blockwatch-suppress: docs/a.md", desc_only.annotations[2].message
            )
            self.assertIn(
                "Suppressed by: pull request description", desc_only.annotations[3].message
            )
            self.assertIn("> [!WARNING]", desc_only.summary)
            self.assertIn("ignored", desc_only.summary)
            self.assertIn(
                "suppress_from_commits is false", re.sub(r"<[^>]+>", "", desc_only.summary)
            )
            self.assertIn("applied (matched 1 violation)", desc_only.summary)
            self.assertIs(desc_only.json_output("commit_suppressions")[0]["applied"], False)
            self.assertIs(desc_only.json_output("description_suppressions")[0]["applied"], True)
            self.assertEqual(desc_only.annotations[3].properties["file"], "docs/b.md")

        with self.subTest("both sources disabled"):
            both_off = self.run_pull_request(
                body, INPUT_SUPPRESS_FROM_COMMITS="false", INPUT_SUPPRESS_FROM_DESCRIPTION="false"
            )
            self.assertEqual(both_off.exit_code, 1)
            self.assertEqual(both_off.levels, ["notice", "notice", "error", "error"])
            self.assertNotIn("blockwatch-suppress:", both_off.annotations[2].message)
            self.assertIn("Suppress with the suppress input:", both_off.annotations[2].message)
            self.assertIn("> [!NOTE]", both_off.summary)
            plain_summary = re.sub(r"<[^>]+>", "", both_off.summary)
            self.assertIn("ignored (suppress_from_commits is false)", plain_summary)
            self.assertIn("ignored (suppress_from_description is false)", plain_summary)
            self.assertIs(both_off.json_output("commit_suppressions")[0]["applied"], False)
            self.assertIs(both_off.json_output("description_suppressions")[0]["applied"], False)

    def test_disabling_all_sources_ignores_malformed_directives_without_failing(self) -> None:
        """A bare `blockwatch-suppress:` fails when active, or emits a notice when disabled."""
        self.commit_notes("revise the notes\n\nblockwatch-suppress:\n", content="Prose, revised.\n")
        body = "blockwatch-suppress:"

        self.assertEqual(self.run_pull_request(body).exit_code, 2)

        result = self.run_pull_request(
            body, INPUT_SUPPRESS_FROM_COMMITS="false", INPUT_SUPPRESS_FROM_DESCRIPTION="false"
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["notice", "notice"])
        self.assertIn(
            "ignored suppression in commit message", result.annotations[0].properties["title"]
        )
        self.assertIn(
            "ignored suppression in pull request description",
            result.annotations[1].properties["title"],
        )


class SuppressionReportingTest(ActionTestCase):
    """How suppression attempts and violations render in annotations, summaries, and outputs."""

    def test_summary_and_outputs_record_active_commit_and_description_suppressions(self) -> None:
        """Active commit and PR suppressions populate warnings, summary tables, and step outputs."""
        sha = self.break_both("break both\n\nblockwatch-suppress: docs/a.md:fruit\n")
        body = "Context\n\nblockwatch-suppress: docs/b.md"

        result = self.run_pull_request(body)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning", "warning", "warning", "warning"])
        self.assertEqual(
            result.annotations[0].properties["title"],
            "blockwatch%3A suppression in commit message",
        )
        self.assertIn("[matched 1]", result.annotations[0].message)
        self.assertEqual(
            result.annotations[1].properties["title"],
            "blockwatch%3A suppression in pull request description",
        )
        self.assertIn(
            "edits take effect on re-run without a new commit", result.annotations[1].message
        )
        self.assertIn("[matched 1]", result.annotations[1].message)
        self.assertIn("> [!WARNING]", result.summary)
        self.assertIn("blockwatch: 2 suppression directive(s)", result.summary)
        self.assertIn("applied (matched 1 violation)", result.summary)
        self.assertIn("(suppressed via commits)", result.summary)
        self.assertIn("(suppressed via description)", result.summary)

        self.assertEqual(set(result.outputs), {"commit_suppressions", "description_suppressions"})
        commit_items = result.json_output("commit_suppressions")
        self.assertEqual(len(commit_items), 1)
        self.assertEqual(commit_items[0]["commit"], sha[:7])
        self.assertEqual(commit_items[0]["subject"], "break both")
        self.assertEqual(commit_items[0]["address"], "docs/a.md:fruit")
        self.assertIs(commit_items[0]["applied"], True)
        self.assertEqual(
            result.json_output("description_suppressions"),
            [{"line": "blockwatch-suppress: docs/b.md", "address": "docs/b.md", "applied": True}],
        )

    def test_sarif_preserves_suppression_annotations_summary_and_outputs(self) -> None:
        """SARIF output reports the same suppression annotations, summary, and outputs as JSON."""
        self.break_fruit("suppress\n\nblockwatch-suppress: docs/a.md:fruit\n")

        as_json = self.run_action()
        as_sarif = self.run_action(INPUT_FORMAT="sarif")

        self.assertEqual(as_sarif.exit_code, 0)
        self.assertEqual(as_sarif.levels, ["warning", "warning"])
        self.assertEqual(as_sarif.annotations, as_json.annotations)
        self.assertEqual(as_sarif.summary, as_json.summary)
        self.assertEqual(as_sarif.outputs, as_json.outputs)

    def test_annotations_limit_caps_violations_only_not_suppression_warnings(self) -> None:
        """`annotations_limit` caps violation annotations without counting directive warnings."""
        self.break_both("suppress fruit only\n\nblockwatch-suppress: docs/a.md:fruit\n")

        with self.subTest("limit=1 keeps directive warning and one violation"):
            limited = self.run_action(INPUT_ANNOTATIONS_LIMIT="1")
            self.assertEqual(limited.exit_code, 1)
            self.assertEqual(limited.levels, ["warning", "warning", "notice"])
            self.assertIn(
                "1 further violation(s) were not annotated (annotations_limit=1)",
                limited.annotations[2].message,
            )

        with self.subTest("limit=0 still emits directive warning and omits all violations"):
            zero = self.run_action(INPUT_ANNOTATIONS_LIMIT="0")
            self.assertEqual(zero.exit_code, 1)
            self.assertEqual(zero.levels, ["warning", "notice"])
            self.assertIn("blockwatch-suppress: docs/a.md:fruit", zero.annotations[0].message)
            self.assertIn(
                "2 further violation(s) were not annotated (annotations_limit=0)",
                zero.annotations[1].message,
            )

    def test_annotations_truncate_beyond_twenty_directives(self) -> None:
        """Directive warning annotations truncate beyond 20 entries while summaries keep all."""
        directives = "\n".join("blockwatch-suppress: docs/notes.md" for _ in range(22))
        self.commit_notes("many suppressions\n\n" + directives + "\n")

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.levels, ["warning"])
        self.assertIn("... and 2 more (see job summary).", result.annotations[0].message)
        self.assertIn("blockwatch: 22 suppression directive(s)", result.summary)
        self.assertNotIn("further directive(s) not listed", result.summary)
        self.assertEqual(len(result.json_output("commit_suppressions")), 22)

    def test_large_directive_lists_cap_summary_rows_and_bound_output_json(self) -> None:
        """Summaries cap at 1000 directive rows and `GITHUB_OUTPUT` JSON stays within 512 KiB."""
        directives = "\n".join("blockwatch-suppress: docs/notes.md" for _ in range(4500))
        self.commit_notes("huge suppression list\n\n" + directives + "\n")

        result = self.run_action()

        self.assertEqual(result.exit_code, 0)
        self.assertIn("blockwatch: 4500 suppression directive(s)", result.summary)
        self.assertIn("_3500 further directive(s) not listed._", result.summary)
        raw_json = result.outputs["commit_suppressions"]
        self.assertLessEqual(len(raw_json.encode("utf-8")), 512 * 1024)
        parsed = result.json_output("commit_suppressions")
        self.assertGreater(len(parsed), 1000)
        self.assertLess(len(parsed), 4500)

    def test_outputs_are_written_even_when_annotations_and_summary_are_off(self) -> None:
        """Step outputs remain populated even when both annotations and summary are disabled."""
        self.break_fruit("suppress\n\nblockwatch-suppress: docs/a.md:fruit\n")

        result = self.run_pull_request(
            "blockwatch-suppress: docs/b.md",
            INPUT_ANNOTATIONS="false",
            INPUT_SUMMARY="false",
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.annotations, [])
        self.assertEqual(result.summary, "")
        self.assertEqual(set(result.outputs), {"commit_suppressions", "description_suppressions"})
        self.assertEqual(len(result.json_output("commit_suppressions")), 1)
        self.assertEqual(len(result.json_output("description_suppressions")), 1)
        self.assertIs(result.json_output("commit_suppressions")[0]["applied"], True)
        self.assertIs(result.json_output("description_suppressions")[0]["applied"], True)

    def test_run_failure_omits_match_counts_from_annotations_and_summary(self) -> None:
        """When blockwatch fails with exit 2, directives show `applied` without match counts."""
        self.commit_notes("valid commit suppress\n\nblockwatch-suppress: docs/a.md\n")

        result = self.run_pull_request("blockwatch-suppress:")

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.levels, ["warning", "warning", "error"])
        self.assertNotIn("[matched", result.annotations[0].message)
        self.assertNotIn("[matched", result.annotations[1].message)
        self.assertNotIn("applied (matched", result.summary)
        self.assertRegex(result.summary, r"\|\s*applied\s*\|.*blockwatch-suppress: docs/a\.md")
        self.assertRegex(result.summary, r"\|\s*applied\s*\|.*blockwatch-suppress:")

    def test_failed_diff_still_records_detected_attempts_in_outputs(self) -> None:
        """When git diff aborts early, detected suppression attempts still populate step outputs."""
        self.break_fruit()

        result = self.run_pull_request(
            "blockwatch-suppress: docs/a.md",
            base_ref="no-such-branch",
            sync_base=False,
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("no diff to check", result.stderr)
        self.assertEqual(result.annotations, [])
        self.assertEqual(set(result.outputs), {"commit_suppressions", "description_suppressions"})
        self.assertEqual(result.json_output("commit_suppressions"), [])
        self.assertEqual(result.json_output("description_suppressions")[0]["address"], "docs/a.md")

    def test_untrusted_text_in_directives_subjects_and_violations_is_escaped(self) -> None:
        """Workflow command metacharacters, HTML tags, pipes, and CRs are escaped in outputs."""
        self.write("docs/bad%0A|name.md", UNSORTED_FRUIT)
        self.commit("pipe | <tag> %0A\n\nblockwatch-suppress: docs/a.md | <bad>\n")

        result = self.run_pull_request("blockwatch-suppress: docs/b.md | <xss>")

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.levels, ["warning", "warning", "error"])
        self.assertIn("%250A", result.annotations[0].message)
        self.assertEqual(result.annotations[2].properties["file"], "docs/bad%250A|name.md")
        self.assertIn(
            "Suppress with: blockwatch-suppress: docs/bad%250A|name.md",
            result.annotations[2].message,
        )
        for expected in (
            "&#124;",
            "&lt;tag&gt;",
            "&lt;bad&gt;",
            "&lt;xss&gt;",
            r"docs/bad%0A\|name.md",
        ):
            self.assertIn(expected, result.summary)
        for raw in ("<tag>", "<bad>", "<xss>"):
            self.assertNotIn(raw, result.summary)
