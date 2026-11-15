"""Pull request REST API tests: live description read and fallback behavior."""

from __future__ import annotations

import socket

from .harness import (
    SORTED_FRUIT,
    UNSORTED_FRUIT,
    ActionTestCase,
    FakeGitHubApi,
    ensure_pinned_blockwatch,
)


def setUpModule() -> None:
    ensure_pinned_blockwatch()


class PullRequestApiTest(ActionTestCase):
    """Specification of live pull request description fetching and fallback rules."""

    def test_api_request_targets_pull_endpoint_with_github_headers(self) -> None:
        """Fetching a PR description requests `/repos/<repo>/pulls/<n>` with GitHub headers."""
        self.break_fruit()

        _, api = self.run_pr_api(
            api_body="blockwatch-suppress: docs/a.md",
            snapshot_body="",
            number=42,
        )

        self.assertEqual(len(api.requests), 1)
        path, headers = api.requests[0]
        self.assertEqual(path, "/repos/acme/widgets/pulls/42")
        self.assertEqual(headers["authorization"], "Bearer token-123")
        self.assertEqual(headers["accept"], "application/vnd.github+json")
        self.assertEqual(headers["x-github-api-version"], "2022-11-28")
        self.assertEqual(headers["user-agent"], "blockwatch-action")

    def test_live_description_replaces_event_snapshot(self) -> None:
        """The live API PR body replaces the snapshot so edits take effect on re-run."""
        self.break_fruit()

        with self.subTest("live body adds suppression missing from snapshot"):
            added, _ = self.run_pr_api(
                api_body="Edited later\n\nblockwatch-suppress: docs/a.md",
                snapshot_body="",
                number=42,
            )
            self.assertEqual(added.exit_code, 0)
            self.assertEqual(added.levels, ["warning", "warning"])
            self.assertIn("read from the API, as it reads now", added.stdout)
            self.assertEqual(
                [item["address"] for item in added.json_output("description_suppressions")],
                ["docs/a.md"],
            )

        with self.subTest("live empty body clears stale suppression from snapshot"):
            removed, _ = self.run_pr_api(
                api_body="",
                snapshot_body="blockwatch-suppress: docs/a.md",
                number=42,
            )
            self.assertEqual(removed.exit_code, 1)
            self.assertEqual(removed.levels, ["error"])
            self.assertEqual(removed.json_output("description_suppressions"), [])

    def test_suppress_from_description_false_skips_api_and_reports_snapshot_attempts(self) -> None:
        """Disabling description suppressions skips the API and marks snapshot items ignored."""
        self.break_fruit()

        disabled, api = self.run_pr_api(
            api_body="blockwatch-suppress: docs/b.md",
            snapshot_body="blockwatch-suppress: docs/a.md",
            number=42,
            INPUT_SUPPRESS_FROM_DESCRIPTION="false",
        )

        self.assertEqual(api.requests, [])
        self.assertEqual(disabled.exit_code, 1)
        self.assertEqual(disabled.levels, ["warning", "error"])
        desc_items = disabled.json_output("description_suppressions")
        self.assertEqual([item["address"] for item in desc_items], ["docs/a.md"])
        self.assertIs(desc_items[0]["applied"], False)

    def test_http_error_falls_back_to_event_snapshot(self) -> None:
        """Non-2xx API responses fall back to the event payload snapshot with status logged."""
        self.break_fruit()

        fallback, _ = self.run_pr_api(
            status=503,
            api_body="",
            snapshot_body="blockwatch-suppress: docs/a.md",
            number=42,
        )

        self.assertEqual(fallback.exit_code, 0)
        self.assertIn("the API answered HTTP 503", fallback.stdout)
        self.assertEqual(fallback.levels, ["warning", "warning"])

    def test_null_and_non_object_api_responses_leave_description_empty(self) -> None:
        """A null body or non-object JSON API response yields an empty description."""
        self.break_both("break both\n\nblockwatch-suppress: docs/b.md\n")

        with self.subTest("null body"):
            null_body, _ = self.run_pr_api(api_body=None, snapshot_body=None, number=7)
            self.assertEqual(null_body.exit_code, 1)
            self.assertEqual(null_body.json_output("description_suppressions"), [])
            self.assertEqual(len(null_body.json_output("commit_suppressions")), 1)

        with self.subTest("non-object JSON"):
            non_object, _ = self.run_pr_api(
                raw_body=b'["unexpected"]',
                snapshot_body="blockwatch-suppress: docs/a.md",
                number=7,
            )
            self.assertEqual(non_object.exit_code, 1)
            self.assertEqual(non_object.json_output("description_suppressions"), [])

    def test_malformed_json_unreachable_endpoint_and_bad_scheme_fall_back_to_snapshot(self) -> None:
        """Malformed JSON, network errors, and non-HTTP URLs fall back cleanly to the snapshot."""
        self.break_both("break both\n\nblockwatch-suppress: docs/b.md\n")

        with self.subTest("malformed JSON"):
            malformed, _ = self.run_pr_api(
                raw_body=b"{not-json",
                snapshot_body="blockwatch-suppress: docs/a.md",
                number=7,
            )
            self.assertEqual(malformed.exit_code, 0)
            self.assertIn("the API read failed:", malformed.stdout)
            self.assertEqual(
                [item["address"] for item in malformed.json_output("commit_suppressions")],
                ["docs/b.md"],
            )
            self.assertEqual(
                [item["address"] for item in malformed.json_output("description_suppressions")],
                ["docs/a.md"],
            )

        with self.subTest("unreachable TCP port"):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                closed_port = sock.getsockname()[1]
            unreachable = self.run_pull_request(
                "blockwatch-suppress: docs/a.md",
                number=7,
                GITHUB_API_URL="http://127.0.0.1:%d" % closed_port,
                GH_TOKEN="token-123",
                GITHUB_REPOSITORY="acme/widgets",
            )
            self.assertEqual(unreachable.exit_code, 0)
            self.assertIn("the API read failed:", unreachable.stdout)

        with self.subTest("unsupported URL scheme"):
            unsupported = self.run_pull_request(
                "blockwatch-suppress: docs/a.md",
                number=7,
                GITHUB_API_URL="file:///tmp/pr.json",
                GH_TOKEN="token-123",
                GITHUB_REPOSITORY="acme/widgets",
            )
            self.assertEqual(unsupported.exit_code, 0)
            self.assertIn("unsupported API URL scheme", unsupported.stdout)

    def test_missing_repository_skips_api_even_when_token_is_set(self) -> None:
        """Without repository metadata the action skips the API request and reads the snapshot."""
        self.break_both("break both\n\nblockwatch-suppress: docs/b.md\n")

        missing_repo, api = self.run_pr_api(
            api_body="blockwatch-suppress: docs/a.md",
            snapshot_body=None,
            number=7,
            repository="",
        )

        self.assertEqual(api.requests, [])
        self.assertEqual(missing_repo.exit_code, 1)
        self.assertIn("read from the event payload.", missing_repo.stdout)

    def test_live_api_description_can_fail_run_with_malformed_directive(self) -> None:
        """A malformed directive in the live PR description fails the run with exit code 2."""
        self.commit_notes("touch notes")

        result, api = self.run_pr_api(api_body="blockwatch-suppress:", number=9)

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.levels, ["warning", "error"])
        self.assertEqual(
            result.json_output("description_suppressions"),
            [{"line": "blockwatch-suppress:", "address": "", "applied": True}],
        )
        self.assertIn("blockwatch-suppress:", result.summary)
        self.assertEqual(len(api.comments), 1)
        self.assertRegex(str(api.comments[0]["body"]), r"\|\s*applied\s*\|.*blockwatch-suppress:")

    def test_suppression_directive_posts_updates_skips_unchanged_and_deletes_sticky_comment(
        self,
    ) -> None:
        """Sticky PR comment tracks suppressions across create, skip, update, and delete."""
        self.break_fruit("suppress in commit\n\nblockwatch-suppress: docs/a.md:fruit\n")
        marker = "<!-- blockwatch-action:suppressions -->"

        with FakeGitHubApi(body="") as api:
            created = self.run_against_api(api, number=42)
            self.assertEqual(created.exit_code, 0)
            self.assertEqual(
                [req[:2] for req in api.comment_requests],
                [
                    ("GET", "/repos/acme/widgets/issues/42/comments?per_page=100"),
                    ("POST", "/repos/acme/widgets/issues/42/comments"),
                ],
            )
            self.assertEqual(len(api.comments), 1)
            first_body = str(api.comments[0]["body"])
            self.assertTrue(first_body.startswith(marker))
            self.assertIn("> [!WARNING]", first_body)
            self.assertIn("applied (matched 1 violation)", first_body)
            violation = created.diagnostics["docs/a.md"][0]
            self.assertIn("docs/a.md:%s" % violation["range"]["start"]["line"], first_body)
            self.assertIn("keep-sorted (commit message)", first_body)

            api.comment_requests.clear()
            unchanged = self.run_against_api(api, number=42)
            self.assertEqual(unchanged.exit_code, 0)
            self.assertEqual(
                [req[0] for req in api.comment_requests],
                ["GET"],
            )
            self.assertEqual(len(api.comments), 1)

            api.body = "blockwatch-suppress: docs/b.md"
            api.comment_requests.clear()
            updated = self.run_against_api(api, number=42)
            self.assertEqual(updated.exit_code, 0)
            self.assertEqual(
                [req[:2] for req in api.comment_requests],
                [
                    ("GET", "/repos/acme/widgets/issues/42/comments?per_page=100"),
                    ("PATCH", "/repos/acme/widgets/issues/comments/1"),
                ],
            )
            self.assertEqual(len(api.comments), 1)
            self.assertIn("blockwatch: 2 suppression directive(s)", str(api.comments[0]["body"]))
            self.assertIn("pull request description", str(api.comments[0]["body"]))

            # Clean PR with no remaining directives deletes the stale sticky comment.
            self.git("reset", "--hard", self.base_sha)
            self.write("docs/a.md", SORTED_FRUIT)
            self.commit_notes("clean fix without directives")
            api.body = ""
            api.comment_requests.clear()
            cleared = self.run_against_api(api, number=42)
            self.assertEqual(cleared.exit_code, 0)
            self.assertEqual(
                [req[:2] for req in api.comment_requests],
                [
                    ("GET", "/repos/acme/widgets/issues/42/comments?per_page=100"),
                    ("DELETE", "/repos/acme/widgets/issues/comments/1"),
                ],
            )
            self.assertEqual(api.comments, [])

    def test_sticky_comment_finds_existing_comment_across_paginated_comments(self) -> None:
        """Pagination locates existing sticky comments without mistaking quoted mentions."""
        self.break_fruit()
        existing = [
            {"id": 10, "body": "Reviewer discussion"},
            {"id": 11, "body": "Quoting <!-- blockwatch-action:suppressions --> mid-body"},
            {"id": 12, "body": "<!-- blockwatch-action:suppressions -->\nstale comment"},
        ]

        result, api = self.run_pr_api(
            api_body="blockwatch-suppress: docs/a.md:fruit",
            comments=existing,
            comments_page_size=2,
            number=7,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            [(req[0], req[1]) for req in api.comment_requests],
            [
                ("GET", "/repos/acme/widgets/issues/7/comments?per_page=100"),
                ("GET", "/repos/acme/widgets/issues/7/comments?page=2"),
                ("PATCH", "/repos/acme/widgets/issues/comments/12"),
            ],
        )
        self.assertEqual(len(api.comments), 3)
        self.assertIn("Reviewer discussion", str(api.comments[0]["body"]))
        self.assertIn("Quoting", str(api.comments[1]["body"]))
        self.assertIn("applied (matched 1 violation)", str(api.comments[2]["body"]))

    def test_sticky_comment_skips_gracefully_without_write_permission_or_when_disabled(
        self,
    ) -> None:
        """Read-only/fork 403s log a note cleanly without failing, and `comment: false` skips API."""
        self.break_fruit("suppress\n\nblockwatch-suppress: docs/a.md:fruit\n")

        with self.subTest("HTTP 403 on write leaves step passing"):
            readonly, api = self.run_pr_api(api_body="", comment_write_status=403, number=5)
            self.assertEqual(readonly.exit_code, 0)
            self.assertIn("HTTP 403", readonly.stdout)
            self.assertIn("pull-requests: write", readonly.stdout)
            self.assertEqual(api.comments, [])

        with self.subTest("comment=false makes no comment API requests"):
            disabled, api = self.run_pr_api(
                api_body="blockwatch-suppress: docs/a.md",
                number=5,
                INPUT_COMMENT="false",
            )
            self.assertEqual(disabled.exit_code, 0)
            self.assertEqual(api.comment_requests, [])

        with self.subTest("invalid comment boolean fails fast"):
            invalid = self.run_pull_request("blockwatch-suppress: docs/a.md", INPUT_COMMENT="yes")
            self.assertEqual(invalid.exit_code, 1)
            self.assertIn("comment", invalid.stderr)

    def test_sticky_comment_caps_rows_handles_disabled_sources_and_escapes_text(self) -> None:
        """PR comments escape untrusted HTML/pipes, reflect disabled `[!NOTE]`, and cap rows."""
        self.write("docs/bad|file.md", UNSORTED_FRUIT)
        self.commit("pipe | <tag> %0A\n\nblockwatch-suppress: docs/bad|file.md\n")

        with self.subTest("disabled sources post [!NOTE] with escaped text"):
            disabled, api = self.run_pr_api(
                api_body="blockwatch-suppress: docs/b.md | <xss>",
                snapshot_body="blockwatch-suppress: docs/b.md | <xss>",
                number=9,
                INPUT_SUPPRESS_FROM_COMMITS="false",
                INPUT_SUPPRESS_FROM_DESCRIPTION="false",
            )
            self.assertEqual(disabled.exit_code, 1)
            self.assertEqual(len(api.comments), 1)
            comment = str(api.comments[0]["body"])
            self.assertIn("> [!NOTE]", comment)
            self.assertIn("ignored (<code>suppress_from_commits</code> is false)", comment)
            for escaped in ("&lt;tag&gt;", "&lt;xss&gt;", "&#124;"):
                self.assertIn(escaped, comment)
            for raw in ("<tag>", "<xss>"):
                self.assertNotIn(raw, comment)

        with self.subTest("more than 50 directives caps PR comment rows safely"):
            many = "\n".join("blockwatch-suppress: docs/notes.md" for _ in range(60))
            self.git("reset", "--hard", self.base_sha)
            self.commit_notes("clean change")
            capped, api = self.run_pr_api(api_body=many, number=12)
            self.assertEqual(capped.exit_code, 0)
            self.assertEqual(len(api.comments), 1)
            comment = str(api.comments[0]["body"])
            self.assertIn("blockwatch: 60 suppression directive(s)", comment)
            self.assertIn("_10 further directive(s) not listed._", comment)
            self.assertLessEqual(len(comment), 65536)
