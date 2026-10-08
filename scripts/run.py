#!/usr/bin/env python3
"""Run blockwatch over the diff for this event and report what it found.

This is the whole of the action's seventh step. It lived inline in `action.yml`
until the script outgrew GitHub's 21000-character template limit, which a `run:`
block is subject to because it embeds `${{ }}` expressions. A file is not a
template, so there is no ceiling here — and every input arrives through the
environment, so there is no expression expansion to inject into either.

Inputs arrive as INPUT_* variables, the event as GITHUB_EVENT_PATH, and nothing
is read from the command line.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterator

# `from __future__ import annotations` is what lets the modern spellings below
# (`list[str]`, `list[str] | None`) work on the oldest Python the README claims:
# annotations are stored as strings and never evaluated at runtime.
#
# This alias is the exception, and the reason it is spelled `Dict` and not
# `dict`: it is an ordinary assignment, evaluated when the module loads, and
# `dict[str, Any]` is only subscriptable from 3.9.
JsonObject = Dict[str, Any]

ZERO_SHA: str = "0" * 40

# The address prefix blockwatch reads out of commit messages and descriptions.
# Matched here only to name them in the log; blockwatch decides what they do.
SUPPRESS_LINE: re.Pattern[str] = re.compile(r"^[ \t]*blockwatch-suppress:", re.IGNORECASE)

# GitHub renders a limited number of annotations per step and drops the rest
# without saying so, so the step stops at the caller's limit and reports the
# remainder. The job summary is rejected outright above 1 MiB, hence its own cap.
SUMMARY_ROW_LIMIT: int = 1000


class Failure(Exception):
    """An input or environment problem that should fail the step by itself."""


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def split_list(value: str) -> list[str]:
    """Split a list input on commas or newlines, trimming and dropping blanks.

    Both spellings exist because a YAML block scalar is the readable way to
    write a long list and a quoted string is the terse one. Items keep their
    internal spaces; only the padding a block scalar or a ", " leaves goes.
    """
    if not value:
        return []
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def scalar(value: str) -> str:
    """Normalize a single-token input. Whitespace is never meaningful in one."""
    return re.sub(r"\s+", "", value or "")


def parse_bool(name: str, value: str, default: bool = False) -> bool:
    """Read a boolean input strictly.

    Composite actions have no typed inputs, so these arrive as strings. Only the
    spellings GitHub itself produces are accepted: anything else is refused
    rather than read as false, because a mistyped value would otherwise turn off
    reporting, or widen a run to the whole repository, with nothing downstream to
    say it had happened.

    An absent value falls to `default`, which repeats what `action.yml` declares
    — the same duplication `parse_limit` already carries. GitHub substitutes the
    declared default before the step runs, so the script only sees an empty
    string when it is run outside the action; reading that as false would turn
    reporting off silently, which is the failure this function exists to refuse.
    Keep the two in step when changing either.
    """
    token = scalar(value)
    if token in ("true", "True", "TRUE"):
        return True
    if token in ("false", "False", "FALSE"):
        return False
    if token == "":
        return default
    raise Failure('%s must be "true" or "false", got %r' % (name, token))


def parse_limit(value: str) -> int:
    token = scalar(value)
    if not token:
        return 50
    if not token.isdigit():
        raise Failure("annotations_limit must be a whole number, got %r" % token)
    return int(token)


def event_payload() -> JsonObject:
    """The event that triggered this run, as the runner wrote it to disk."""
    path = env("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    # A payload that is not an object would fail later, at the first .get().
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------
# Suppression sources
# --------------------------------------------------------------------------


def pull_request_body(payload: JsonObject) -> tuple[str, str]:
    """The description, read live, with the event payload as the fallback.

    The payload is a snapshot taken when the run was created, and re-running a
    workflow replays it. A suppression added to the description afterwards would
    stay invisible until the next push, which defeats the obvious loop: read the
    annotation, paste its address into the description, press Re-run, watch the
    violation stop blocking.

    The live copy replaces the snapshot rather than being merged with it, so
    *removing* an address takes effect too.
    """
    snapshot = (payload.get("pull_request") or {}).get("body") or ""
    number = (payload.get("pull_request") or {}).get("number")
    token = env("GH_TOKEN")
    repository = env("GITHUB_REPOSITORY")
    if not (token and number and repository):
        return snapshot, "the event payload"

    url = "%s/repos/%s/pulls/%s" % (
        env("GITHUB_API_URL", "https://api.github.com"),
        repository,
        number,
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "blockwatch-action",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = json.load(response)
        body = (document.get("body") if isinstance(document, dict) else "") or ""
        return str(body), "the API, as it reads now"
    except urllib.error.HTTPError as error:
        return snapshot, "the event payload (the API answered HTTP %s)" % error.code
    except Exception as error:  # network, DNS, timeout, malformed JSON
        return snapshot, "the event payload (the API read failed: %s)" % error


def push_base(event_name: str, payload: JsonObject) -> str | None:
    """The tip a push diffs from, or None when only its head commit can be checked.

    Resolved once, for both the diff and the commit messages read for
    suppressions, so the two always cover the same commits.

    A push that creates a branch reports an all-zero "before": there is no prior
    tip. A force-push reports the tip it replaced, which no branch or tag points
    at any more, and actions/checkout fetches only those — so the commit is not
    in the clone, and diffing from it fails outright. Either way the head commit
    is the one thing left to check.
    """
    if event_name != "push":
        return None
    before = payload.get("before") or ""
    if not before or before == ZERO_SHA:
        return None
    present = subprocess.run(
        ["git", "cat-file", "-e", "%s^{commit}" % before],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if present.returncode:
        print(
            "Note: the push replaced %s, which is not in the checkout (a force-push), "
            "so only the head commit is checked." % before
        )
        return None
    return before


def git_log_messages(event_name: str, before: str | None, current_sha: str) -> list[str]:
    """The commit messages this run is allowed to read suppressions from.

    The range mirrors the diff below exactly, so only the work being checked can
    suppress anything: a commit elsewhere in the repository cannot reach in and
    silence a violation.
    """
    if event_name == "pull_request":
        # Two dots, not the three the diff uses: this wants the commits the pull
        # request adds, not a patch. The merge commit GitHub checks out is
        # included and harmless, its generated message carrying no address.
        base = env("GITHUB_BASE_REF")
        return ["git", "log", "--format=%B", "origin/%s..%s" % (base, current_sha)]
    if event_name == "push":
        if before:
            return ["git", "log", "--format=%B", "%s..%s" % (before, current_sha)]
        # A new branch, or a force-push whose prior tip is gone, leaves no range
        # to enumerate, so only the head commit is read — exactly the commit the
        # diff covers.
        return ["git", "log", "-1", "--format=%B", current_sha]
    # No push range and no pull request, so the commit being scanned is the only
    # one that can be meant. Reading it keeps a workflow_dispatch re-run of a
    # commit behaving like the push that first checked it.
    return ["git", "log", "-1", "--format=%B", "HEAD"]


def write_suppression_file(
    event_name: str, payload: JsonObject, before: str | None, current_sha: str
) -> str:
    """Collect the commit messages, and any description, into one file.

    blockwatch reads `blockwatch-suppress: ADDRESS` lines out of any text and
    ignores every other line, so no extraction is needed — a commit message is
    valid input as it stands. This is additive: the `suppress` and
    `suppress_from` inputs still apply.
    """
    handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    with handle:
        log = subprocess.run(
            git_log_messages(event_name, before, current_sha),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        handle.write(log.stdout.decode("utf-8", "replace"))
        if event_name == "pull_request":
            body, source = pull_request_body(payload)
            handle.write("\n%s\n" % body)
            print("Note: pull request description read from %s." % source)

    # A suppression picked up here changes the exit code without appearing
    # anywhere in the workflow file, so the log is the only place a reader can
    # find out it happened. This only labels the lines; blockwatch remains the
    # one deciding which of them take effect.
    with open(handle.name, encoding="utf-8") as written:
        found = [line.rstrip() for line in written if SUPPRESS_LINE.match(line)]
    if found:
        print("Note: suppression addresses found in the commit messages/description under test:")
        for line in found:
            print(line)
    return handle.name


# --------------------------------------------------------------------------
# blockwatch invocation
# --------------------------------------------------------------------------


def blockwatch_args(suppression_file: str) -> list[str]:
    """Everything after `blockwatch`, in the order blockwatch needs it."""
    args: list[str] = []
    for flag, value in (
        ("-E", env("INPUT_EXTENSIONS")),
        ("-e", env("INPUT_ENABLE")),
        ("-d", env("INPUT_DISABLE")),
        # Addresses of named blocks, FILE:BLOCK_NAME, whose `:` split_list
        # leaves alone. Unlike a suppression, one that matches no block fails
        # the run, and blockwatch is the one able to say so: it reads the file.
        ("--only-block", env("INPUT_ONLY_BLOCKS")),
        ("--skip-block", env("INPUT_SKIP_BLOCKS")),
        ("--ignore", env("INPUT_IGNORE")),
        # Addresses of violations to report but not fail on. Nothing is checked
        # here: blockwatch rejects a malformed address naming the segment at
        # fault, and silently ignores a well-formed one that covers no
        # violation — an address is meant to be copied out of a previous run, so
        # the run that emits it is the one able to say whether it still matches.
        ("--suppress", env("INPUT_SUPPRESS")),
        ("--suppress-from", env("INPUT_SUPPRESS_FROM")),
    ):
        for item in split_list(value):
            args += [flag, item]

    args += ["--suppress-from", suppression_file]

    # Scalars, not lists: a comma in either would emit the flag twice, and clap
    # answers a repeated single-value flag by silently keeping the last. Unknown
    # values are left to blockwatch, which names the ones it accepts.
    verbosity = scalar(env("INPUT_VERBOSITY"))
    if verbosity:
        args += ["--verbosity", verbosity]
    output_format = scalar(env("INPUT_FORMAT"))
    if output_format:
        args += ["--format", output_format]
    # A single path too, and blockwatch refuses a repeated --config outright.
    # Only trimmed: `scalar` strips every space, including one inside a path. A
    # missing file is left to blockwatch, which names it.
    config = env("INPUT_CONFIG").strip()
    if config:
        args += ["--config", config]

    # Globs are positional, so they come last and nothing may follow them. Since
    # blockwatch 0.4.0 they only ever narrow a run, and since 0.9.0 one that
    # selects no file fails it.
    args += split_list(env("INPUT_GLOBS"))
    return args


def head_commit_diff(current_sha: str) -> list[str]:
    """The change the head commit made, against its first parent.

    `git diff-tree`, not `git diff`, for the root commit: --root is a diff-tree
    option that git diff accepts silently and then ignores, comparing the
    working tree instead — on this action's clean checkout, an empty patch.
    `--no-commit-id` drops a line blockwatch does not document as part of its
    input.

    Any other commit is diffed against its first parent by naming it. This was
    `diff-tree -m --first-parent`, but --first-parent only steers a history walk
    and diff-tree ignores it: -m printed one patch per parent, concatenated, and
    blockwatch kept the last one per file — the second parent's. That got a
    merge at the head of a branch backwards. `--diff-merges=first-parent` would
    do it as well, but needs git 2.31, and the README promises 2.18; the
    two-tree form works on every git.
    """
    parent = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "%s^1" % current_sha],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if parent.returncode:
        return ["git", "diff-tree", "--patch", "--root", "--no-commit-id", current_sha]
    return ["git", "diff-tree", "--patch", parent.stdout.decode().strip(), current_sha]


def diff_command(event_name: str, before: str | None, current_sha: str) -> list[str] | None:
    """The diff for this event, or None for an event that has none."""
    pathspec = env("INPUT_DIFF_PATHSPEC").split()

    if event_name == "pull_request":
        # Three dots: diffing against the merge base shows what the pull request
        # itself changed, excluding commits the base branch gained since it
        # forked. Two dots would attribute those to the pull request.
        command = [
            "git",
            "diff",
            "--patch",
            "origin/%s...%s" % (env("GITHUB_BASE_REF"), current_sha),
        ]
    elif event_name == "push":
        if before:
            command = ["git", "diff", "--patch", "%s...%s" % (before, current_sha)]
        else:
            # A new branch, or a force-push whose prior tip is not in the clone.
            # Only the head commit is covered: there is no base for the earlier
            # commits, which get checked when the branch is opened as a PR.
            command = head_commit_diff(current_sha)
    else:
        print(
            "Note: event '%s' provides no diff, so the whole repository is scanned. "
            "Diff-driven checks (e.g. affects) cannot run, and 'only_changed' does "
            "not apply." % event_name
        )
        return None

    if pathspec:
        # `--` keeps git from reading a pathspec as a revision name. The split
        # above is on whitespace so one input can carry several pathspecs; as a
        # list each stays one argument, and a pathspec holding a glob reaches git
        # instead of being matched against the working directory on the way.
        command += ["--"] + pathspec
    return command


def run_blockwatch(
    command: list[str] | None, args: list[str], only_changed: bool
) -> tuple[int, str]:
    """Run blockwatch, returning its exit code and its captured diagnostics.

    stderr is captured rather than streamed so the diagnostics can be read back
    as annotations, then replayed unchanged: what a reader could previously copy
    out of the log is still there, after the run rather than during it. stdout —
    the verbosity report — is left attached to the log as it happens.
    """
    with tempfile.TemporaryFile() as diff:
        if command is not None:
            git = subprocess.run(command, stdout=diff, stderr=None)
            # Checked before blockwatch runs, not after: a git that dies partway
            # leaves a shorter but well-formed patch, which would pass as a
            # clean run over changes nobody read. Its own error is already in
            # the log, on the stderr it inherited.
            if git.returncode:
                raise Failure(
                    "`%s` failed with exit code %d, so there is no diff to check"
                    % (" ".join(command), git.returncode)
                )
            # git succeeded and printed nothing: nothing in the range changed a
            # file, or diff_pathspec excluded every change. Since 0.9.0 blockwatch
            # reads an empty diff as exactly that, so it goes in like any other.
            # Before, blockwatch rejected one and this ran without --diff, which
            # made only_changed check every block instead of none.
            if not diff.seek(0, os.SEEK_END):
                print(
                    "Note: the diff for this event is empty, so no block counts as changed. %s"
                    % (
                        "With 'only_changed', no block is checked."
                        if only_changed
                        else "Every block is still checked, but diff-driven checks "
                        "(e.g. affects) have nothing to compare."
                    )
                )

        # The notes above say how the run is about to go, and blockwatch writes
        # its report to the same stdout; unflushed, they would land after it.
        sys.stdout.flush()
        if command is None:
            # Without --diff blockwatch never reads stdin, so there is no
            # descriptor here to block on.
            result = subprocess.run(["blockwatch"] + args, stderr=subprocess.PIPE)
        else:
            diff.seek(0)
            flags = ["--diff"] + (["--only-changed"] if only_changed else [])
            result = subprocess.run(
                ["blockwatch"] + flags + args, stdin=diff, stderr=subprocess.PIPE
            )

    return result.returncode, result.stderr.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


@dataclass
class Violation:
    """One reported violation, in whichever format blockwatch wrote it."""

    path: str
    start_line: int
    end_line: int
    start_column: int
    # Inclusive, as GitHub wants it — already converted from the half-open
    # range blockwatch reports. See github_end_column.
    end_column: int
    level: str
    code: str
    address: str
    suppressed: bool
    message: str

    @property
    def single_line(self) -> bool:
        # GitHub renders a column range only within one line and ignores one
        # that spans several. Column 0 means blockwatch reported no column.
        return bool(self.start_line == self.end_line and self.start_column and self.end_column)


def github_end_column(start_column: int, end_column: int) -> int:
    """Turn blockwatch's exclusive end column into GitHub's inclusive one.

    Since blockwatch 0.6.0 a range is half-open, `[start, end)`, in the JSON
    diagnostics and in SARIF alike, so the last highlighted character sits one
    before the reported end; GitHub's `endColumn` names that character itself.
    Passing the value straight through highlighted one character too many.

    Zero means blockwatch reported no column at all and stays zero, which is
    what `single_line` reads to leave the range off. A zero-width range is
    held at the start column rather than inverted.
    """
    if not end_column:
        return 0
    return max(start_column, end_column - 1)


def level_for(suppressed: bool, severity: int | None) -> str:
    # A suppressed violation does not fail the run, so it is a notice rather
    # than an error. blockwatch's severities follow LSP: 1 error, 2 warning,
    # 3 information, 4 hint.
    if suppressed:
        return "notice"
    if severity == 2:
        return "warning"
    if severity and severity >= 3:
        return "notice"
    return "error"


def parse_json_diagnostics(document: JsonObject) -> Iterator[Violation]:
    """blockwatch's default output: one object of diagnostics, keyed by file."""
    for path, entries in document.items():
        for entry in entries:
            span = entry.get("range") or {}
            start = span.get("start") or {}
            end = span.get("end") or {}
            suppressed = bool(entry.get("suppressed"))
            yield Violation(
                path=path,
                start_line=start.get("line", 1),
                end_line=end.get("line", start.get("line", 1)),
                start_column=start.get("character", 0),
                end_column=github_end_column(start.get("character", 0), end.get("character", 0)),
                level=level_for(suppressed, entry.get("severity", 1)),
                code=entry.get("code", "blockwatch"),
                address=entry.get("address", ""),
                suppressed=suppressed,
                message=entry.get("message", ""),
            )


def parse_sarif(document: JsonObject) -> Iterator[Violation]:
    """The same violations, as a SARIF 2.1.0 log."""
    for run in document.get("runs") or []:
        for result in run.get("results") or []:
            locations = result.get("locations") or [{}]
            physical = (locations[0] or {}).get("physicalLocation") or {}
            region = physical.get("region") or {}
            artifact = physical.get("artifactLocation") or {}
            suppressed = bool(result.get("suppressions"))
            sarif_level = result.get("level")
            severity = {"warning": 2, "note": 3, "none": 3}.get(sarif_level, 1)
            yield Violation(
                path=artifact.get("uri", ""),
                start_line=region.get("startLine", 1),
                end_line=region.get("endLine", region.get("startLine", 1)),
                start_column=region.get("startColumn", 0),
                end_column=github_end_column(
                    region.get("startColumn", 0), region.get("endColumn", 0)
                ),
                level=level_for(suppressed, severity),
                code=result.get("ruleId", "blockwatch"),
                address=(result.get("properties") or {}).get("address", ""),
                suppressed=suppressed,
                message=(result.get("message") or {}).get("text", ""),
            )


def parse_diagnostics(text: str, output_format: str) -> list[Violation]:
    """Read the captured diagnostics, or give up quietly.

    Output that is not a parseable document is left alone: a usage error
    (a glob that selects no file, a malformed suppression address) arrives on the
    same stream, is already in the log verbatim, and names no line to point at.

    Which parser to use is decided by the input rather than by inspecting the
    document: the JSON diagnostics are keyed by file path, so a repository
    holding a file named "runs" would otherwise read as a SARIF log.
    """
    if not text.strip():
        return []
    try:
        document = json.loads(text)
    except ValueError:
        return []
    if not isinstance(document, dict):
        return []
    if output_format == "sarif":
        return list(parse_sarif(document))
    return list(parse_json_diagnostics(document))


def escape_data(value: str) -> str:
    """Percent-encode a workflow command's message.

    A command is delimited text: a raw newline would end it. GitHub's own
    toolkit encodes exactly these, and the runner decodes them again.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    """The same, plus the separators between properties."""
    return escape_data(value).replace(":", "%3A").replace(",", "%2C")


def emit_annotations(violations: list[Violation], limit: int) -> None:
    emitted = 0
    omitted = 0
    for violation in violations:
        if emitted >= limit:
            omitted += 1
            continue
        properties = []
        if violation.path:
            properties.append("file=%s" % escape_property(violation.path))
            properties.append("line=%s" % violation.start_line)
            properties.append("endLine=%s" % violation.end_line)
            if violation.single_line:
                properties.append("col=%s" % violation.start_column)
                properties.append("endColumn=%s" % violation.end_column)
        properties.append("title=%s" % escape_property("blockwatch: %s" % violation.code))

        message = escape_data(violation.message)
        # The address is what a reader needs in order to act on the annotation,
        # and it appears nowhere else on the line. A violation in an unnamed
        # block has none, so nothing is added for it.
        if violation.address:
            # Concatenated, not %-formatted: the literal "%0A" is a newline for
            # the runner and a format specifier for Python.
            message += "%0ASuppress with: blockwatch-suppress: " + violation.address
        print("::%s %s::%s" % (violation.level, ",".join(properties), message))
        emitted += 1

    if omitted:
        print(
            "::notice title=blockwatch::%d further violation(s) were not annotated "
            "(annotations_limit=%d). All of them are in the job log above." % (omitted, limit)
        )


def write_summary(violations: list[Violation]) -> None:
    path = env("GITHUB_STEP_SUMMARY")
    if not path or not violations:
        return
    rows = []
    for violation in violations[:SUMMARY_ROW_LIMIT]:
        location = "%s:%s" % (violation.path, violation.start_line) if violation.path else "—"
        # Markdown here, not a workflow command: a pipe would end the cell and a
        # newline has to become a line break.
        cell = violation.message.replace("|", r"\|").replace("\n", "<br>")
        code = violation.code + (" (suppressed)" if violation.suppressed else "")
        rows.append("| `%s` | %s | %s | %s |" % (location, code, cell, violation.address or "—"))

    lines = [
        "### blockwatch: %d violation(s)" % len(violations),
        "",
        "| Location | Validator | Message | Suppress address |",
        "| --- | --- | --- | --- |",
    ] + rows
    if len(violations) > len(rows):
        lines += ["", "_%d further violation(s) not listed._" % (len(violations) - len(rows))]

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------


def main() -> int:
    try:
        annotations = parse_bool("annotations", env("INPUT_ANNOTATIONS"), default=True)
        summary = parse_bool("summary", env("INPUT_SUMMARY"), default=True)
        only_changed = parse_bool("only_changed", env("INPUT_ONLY_CHANGED"))
        limit = parse_limit(env("INPUT_ANNOTATIONS_LIMIT"))
    except Failure as error:
        print("Error: %s." % error, file=sys.stderr)
        return 1

    payload = event_payload()
    event_name = env("GITHUB_EVENT_NAME")
    current_sha = env("GITHUB_SHA")

    before = push_base(event_name, payload)
    suppression_file = write_suppression_file(event_name, payload, before, current_sha)
    args = blockwatch_args(suppression_file)
    command = diff_command(event_name, before, current_sha)

    try:
        status, diagnostics = run_blockwatch(command, args, only_changed)
    except Failure as error:
        print("Error: %s." % error, file=sys.stderr)
        return 1
    sys.stdout.flush()
    sys.stderr.write(diagnostics)
    sys.stderr.flush()

    # Reporting must never decide the outcome: the violations are in the log
    # either way, so a failure to annotate them is worth a warning and no more.
    if annotations or summary:
        try:
            violations = parse_diagnostics(diagnostics, scalar(env("INPUT_FORMAT")))
            if annotations:
                emit_annotations(violations, limit)
            if summary:
                write_summary(violations)
        except Exception as error:
            print(
                "::warning title=blockwatch::Could not turn the blockwatch "
                "diagnostics into annotations (%s). They are in the job log above." % error
            )

    # blockwatch's exit code is what fails the job. Everything above only
    # changes where the violations are shown.
    return status


if __name__ == "__main__":
    sys.exit(main())
