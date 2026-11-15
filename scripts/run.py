#!/usr/bin/env python3
"""Run blockwatch over the diff for this event and report what it found.

This is the whole of the action's seventh step. It lived inline in `action.yml`
until the script outgrew GitHub's 21000-character template limit, which a `run:`
block is subject to because it embeds `${{ }}` expressions. A file is not a
template, so there is no ceiling here — and every input arrives through the
environment, so there is no expression expansion to inject into either.

Inputs arrive as INPUT_* variables, the event as GITHUB_EVENT_PATH, and nothing
is read from the command line. Implementation modules live under
`scripts/blockwatch_action/`.
"""

from __future__ import annotations

import sys

from blockwatch_action.config import (
    RUN_FAILED,
    ActionConfig,
    Failure,
    env,
    event_payload,
    read_config,
)
from blockwatch_action.diagnostics import classify_violations, parse_diagnostics
from blockwatch_action.reporting import (
    emit_annotations,
    emit_run_error,
    emit_suppression_annotations,
    write_outputs,
    write_summary,
)
from blockwatch_action.runner import (
    blockwatch_args,
    diff_command,
    push_base,
    run_blockwatch,
)
from blockwatch_action.suppressions import (
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
) -> None:
    """Emit annotations and job summary without affecting step verdict."""
    # Reporting must never decide the outcome: the violations are in the log
    # either way, so a failure here is worth a warning and no more.
    lines_suppress = collection.lines_suppress
    run_failed = status == RUN_FAILED
    try:
        violations = parse_diagnostics(diagnostics, config.output_format)
        classify_violations(violations, collection.directives)
        if config.annotations:
            emit_suppression_annotations(collection.directives, lines_suppress, run_failed)
            emit_annotations(violations, config.annotations_limit, lines_suppress)
            if run_failed:
                emit_run_error(diagnostics)
        if config.summary:
            write_summary(violations, collection.directives, lines_suppress, run_failed)
    except Exception as error:
        print(
            "::warning title=blockwatch::Could not turn the blockwatch "
            "diagnostics into annotations (%s). They are in the job log above." % error
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
        try:
            status, diagnostics = run_blockwatch(
                command, blockwatch_args(collection.file_path), config.only_changed
            )
        except Failure as error:
            print("Error: %s." % error, file=sys.stderr)
            return 1

        sys.stdout.flush()
        sys.stderr.write(diagnostics)
        sys.stderr.flush()
        report_run_outcome(config, collection, status, diagnostics)

    # blockwatch's exit code is what fails the job. Reporting only changes where
    # diagnostics and suppression attempts are shown.
    return status


if __name__ == "__main__":
    sys.exit(main())
