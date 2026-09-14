#!/usr/bin/env python3
"""Run blockwatch over the diff for this event.

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
from typing import Any, Dict

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


def parse_bool(name: str, value: str) -> bool:
    """Read a boolean input strictly.

    Composite actions have no typed inputs, so these arrive as strings. Only the
    spellings GitHub itself produces are accepted: anything else is refused
    rather than read as false, because a mistyped value would otherwise turn off
    reporting, or widen a run to the whole repository, with nothing downstream to
    say it had happened.

    """
    token = scalar(value)
    if token in ("true", "True", "TRUE"):
        return True
    if token in ("false", "False", "FALSE", ""):
        return False
    raise Failure('%s must be "true" or "false", got %r' % (name, token))


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


def pull_request_body(payload: JsonObject) -> str:
    """The pull request description, as the event payload carries it.

    It reaches the script as data — through the payload file, never through an
    expression expansion. It is written by whoever opened the request, a
    stranger on a fork included, and a `${{ github.event.pull_request.body }}`
    inside `run:` would paste their text into a shell for bash to execute.
    """
    body = (payload.get("pull_request") or {}).get("body") or ""
    return str(body)


def git_log_messages(event_name: str, payload: JsonObject, current_sha: str) -> list[str]:
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
        before = payload.get("before") or ""
        if before and before != ZERO_SHA:
            return ["git", "log", "--format=%B", "%s..%s" % (before, current_sha)]
        # A new branch has no prior tip to enumerate from, so only the head
        # commit is read — exactly the commit the diff covers.
        return ["git", "log", "-1", "--format=%B", current_sha]
    # No push range and no pull request, so the commit being scanned is the only
    # one that can be meant. Reading it keeps a workflow_dispatch re-run of a
    # commit behaving like the push that first checked it.
    return ["git", "log", "-1", "--format=%B", "HEAD"]


def write_suppression_file(event_name: str, payload: JsonObject, current_sha: str) -> str:
    """Collect the commit messages, and any description, into one file.

    blockwatch reads `Blockwatch-suppress: ADDRESS` lines out of any text and
    ignores every other line, so no extraction is needed — a commit message is
    valid input as it stands. This is additive: the `suppress` and
    `suppress_from` inputs still apply.
    """
    handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    with handle:
        log = subprocess.run(
            git_log_messages(event_name, payload, current_sha),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        handle.write(log.stdout.decode("utf-8", "replace"))
        if event_name == "pull_request":
            handle.write("\n%s\n" % pull_request_body(payload))

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

    # Globs are positional, so they come last and nothing may follow them. Since
    # blockwatch 0.4.0 they only ever narrow a run.
    args += split_list(env("INPUT_GLOBS"))
    return args


def diff_command(event_name: str, payload: JsonObject, current_sha: str) -> list[str] | None:
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
        before = payload.get("before") or ""
        if before and before != ZERO_SHA:
            command = ["git", "diff", "--patch", "%s...%s" % (before, current_sha)]
        else:
            # A push that creates a branch reports an all-zero "before": there is
            # no prior tip, so the head commit is diffed against the empty tree.
            #
            # `git diff-tree`, not `git diff`: --root is a diff-tree option that
            # git diff accepts silently and then ignores, comparing the working
            # tree instead — on this action's clean checkout, an empty patch that
            # lets every validator pass without examining anything. `-m
            # --first-parent` is what makes a merge commit emit anything at all,
            # and a new branch can have a merge at its head. `--no-commit-id`
            # drops a line blockwatch does not document as part of its input.
            #
            # Only the head commit is covered; a new branch has no base for its
            # earlier commits, which get checked when it is opened as a PR.
            command = [
                "git",
                "diff-tree",
                "--patch",
                "--root",
                "-m",
                "--first-parent",
                "--no-commit-id",
                current_sha,
            ]
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


def run_blockwatch(command: list[str] | None, args: list[str], only_changed: bool) -> int:
    """Run blockwatch and return its exit code.

    Both of its streams stay attached to the job log, as they were when a shell
    pipeline ran this.
    """
    if command is None:
        # Without --diff blockwatch never reads stdin, so there is no descriptor
        # here to block on.
        return subprocess.run(["blockwatch"] + args).returncode

    flags = ["--diff"] + (["--only-changed"] if only_changed else [])
    with tempfile.TemporaryFile() as diff:
        git = subprocess.run(command, stdout=diff)
        diff.seek(0)
        result = subprocess.run(["blockwatch"] + flags + args, stdin=diff)

    # Mirrors what `set -o pipefail` reported before: blockwatch's own code
    # whenever it failed — an unreadable diff included, since an empty stdin is
    # an error it raises itself — and git's only if blockwatch still exited 0.
    return result.returncode or git.returncode


# --------------------------------------------------------------------------


def main() -> int:
    try:
        only_changed = parse_bool("only_changed", env("INPUT_ONLY_CHANGED"))
    except Failure as error:
        print("Error: %s." % error, file=sys.stderr)
        return 1

    payload = event_payload()
    event_name = env("GITHUB_EVENT_NAME")
    current_sha = env("GITHUB_SHA")

    suppression_file = write_suppression_file(event_name, payload, current_sha)
    args = blockwatch_args(suppression_file)
    command = diff_command(event_name, payload, current_sha)

    # blockwatch's exit code is what fails the job.
    return run_blockwatch(command, args, only_changed)


if __name__ == "__main__":
    sys.exit(main())
