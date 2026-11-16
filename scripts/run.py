#!/usr/bin/env python3
"""Run blockwatch over the diff for this event and report what it found.

This is the whole of the action's seventh step. It lived inline in `action.yml`
until the script outgrew GitHub's 21000-character template limit, which a `run:`
block is subject to because it embeds `${{ }}` expressions. A file is not a
template, so there is no ceiling here — and every input arrives through the
environment, so there is no expression expansion to inject into either.

Inputs arrive as INPUT_* variables, the event as GITHUB_EVENT_PATH, and nothing
is read from the command line. Implementation modules live under
`scripts/action/`.
"""

from __future__ import annotations

import sys

from action.config import (
    RUN_FAILED,
    ActionConfig,
    Failure,
    JsonObject,
    env,
    event_payload,
    read_config,
)
from action.diagnostics import classify_violations, parse_diagnostics
from action.github_api import pull_request_links, sync_pull_request_comment
from action.reporting import (
    COMMENT_MARKER,
    emit_annotations,
    emit_run_error,
    emit_suppression_annotations,
    render_pr_comment,
    write_outputs,
    write_summary,
)
from action.runner import (
    blockwatch_args,
    diff_command,
    push_base,
    run_blockwatch,
)
from action.suppressions import (
    SuppressionCollection,
    SuppressionDirective,
    write_suppression_file,
)


def emit_outputs_safely(directives: list[SuppressionDirective]) -> None:
    """Write step outputs once directives are known, warning without failing on I/O errors."""
    try:
        write_outputs(directives)
    except OSError as error:
        print("::warning title=blockwatch::Could not write step outputs (%s)." % error)


def report_run_outcome(
    config: ActionConfig,
    collection: SuppressionCollection,
    status: int,
    diagnostics: str,
    event_name: str,
    payload: JsonObject,
) -> None:
    """Emit annotations, job summary, and sticky PR comment without affecting step verdict."""
    # Reporting must never decide the outcome: the violations are in the log
    # either way, so a failure here is worth a warning and no more.
    lines_suppress = collection.lines_suppress
    run_failed = status == RUN_FAILED
    try:
        violations = parse_diagnostics(diagnostics, config.output_format)
        classify_violations(violations, collection.directives)
    except Exception as error:
        print(
            "::warning title=blockwatch::Could not turn the blockwatch "
            "diagnostics into annotations (%s). They are in the job log above." % error
        )
        if config.annotations and run_failed:
            emit_run_error(diagnostics)
        return

    if config.annotations:
        try:
            emit_suppression_annotations(collection.directives, lines_suppress, run_failed)
            emit_annotations(violations, config.annotations_limit, lines_suppress)
            if run_failed:
                emit_run_error(diagnostics)
        except Exception as error:
            print("::warning title=blockwatch::Could not emit workflow annotations (%s)." % error)

    if config.summary:
        try:
            write_summary(violations, collection.directives, lines_suppress, run_failed)
        except Exception as error:
            print("::warning title=blockwatch::Could not write job summary (%s)." % error)

    # Preserve any existing sticky comment when blockwatch could not finish evaluating.
    if config.comment and event_name == "pull_request" and not run_failed:
        try:
            comment_body = render_pr_comment(violations, pull_request_links(payload))
            sync_pull_request_comment(payload, COMMENT_MARKER, comment_body)
        except Exception as error:
            print(
                "::warning title=blockwatch::Could not sync pull request suppression "
                "comment (%s)." % error
            )


def main() -> int:
    try:
        config = read_config()
    except Failure as error:
        print("Error: %s." % error, file=sys.stderr)
        return 1

    payload = event_payload()
    event_name = env("GITHUB_EVENT_NAME")
    current_sha = env("GITHUB_SHA")
    before = push_base(event_name, payload)

    try:
        with write_suppression_file(
            event_name,
            payload,
            before,
            current_sha,
            config.suppress_from_commits,
            config.suppress_from_description,
        ) as collection:
            emit_outputs_safely(collection.directives)
            command = diff_command(event_name, before, current_sha)
            status, diagnostics = run_blockwatch(
                command, blockwatch_args(collection.file_path), config.only_changed
            )
            sys.stdout.flush()
            sys.stderr.write(diagnostics)
            sys.stderr.flush()
            report_run_outcome(config, collection, status, diagnostics, event_name, payload)
    except Failure as error:
        print("Error: %s." % error, file=sys.stderr)
        return 1

    # blockwatch's exit code is what fails the job. Reporting only changes where
    # diagnostics and suppression attempts are shown.
    return status


if __name__ == "__main__":
    sys.exit(main())
