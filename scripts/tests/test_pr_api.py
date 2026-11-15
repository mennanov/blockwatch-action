"""Pull request REST API tests: live description read and fallback behavior."""

from __future__ import annotations

import socket

from .harness import ActionTestCase, ensure_pinned_blockwatch


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

        result, _ = self.run_pr_api(api_body="blockwatch-suppress:", number=9)

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.levels, ["warning", "error"])
        self.assertEqual(
            result.json_output("description_suppressions"),
            [{"line": "blockwatch-suppress:", "address": "", "applied": True}],
        )
        self.assertIn("blockwatch-suppress:", result.summary)
