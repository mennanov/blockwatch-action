"""Build blockwatch CLI flags, compute git diffs, and run the blockwatch binary."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import IO

from action.config import Failure, JsonObject, env, scalar, split_list

ZERO_SHA: str = "0" * 40

_LIST_INPUT_FLAGS: tuple[tuple[str, str], ...] = (
    ("-E", "INPUT_EXTENSIONS"),
    ("-e", "INPUT_ENABLE"),
    ("-d", "INPUT_DISABLE"),
    # Addresses of named blocks, FILE:BLOCK_NAME, whose `:` split_list leaves
    # alone. Unlike a suppression, one that matches no block fails the run, and
    # blockwatch is the one able to say so: it reads the file.
    ("--only-block", "INPUT_ONLY_BLOCKS"),
    ("--skip-block", "INPUT_SKIP_BLOCKS"),
    ("--ignore", "INPUT_IGNORE"),
    # Addresses of violations to report but not fail on. Nothing is checked
    # here: blockwatch rejects a malformed address naming the segment at fault,
    # and silently ignores a well-formed one that covers no violation — an
    # address is meant to be copied out of a previous run, so the run that emits
    # it is the one able to say whether it still matches.
    ("--suppress", "INPUT_SUPPRESS"),
    ("--suppress-from", "INPUT_SUPPRESS_FROM"),
)


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
    if not isinstance(before, str) or not before or before == ZERO_SHA or before.startswith("-"):
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


def _append_scalar_flags(args: list[str]) -> None:
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


def blockwatch_args(suppression_file: str | None) -> list[str]:
    """Everything after `blockwatch`, in the order blockwatch needs it."""
    args: list[str] = []
    for flag, env_name in _LIST_INPUT_FLAGS:
        for item in split_list(env(env_name)):
            args += [flag, item]

    if suppression_file:
        args += ["--suppress-from", suppression_file]

    _append_scalar_flags(args)

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


def _capture_diff(command: list[str], diff_file: IO[bytes], only_changed: bool) -> None:
    """Run `command`, write its patch into `diff_file`, and rewind to byte 0."""
    git = subprocess.run(command, stdout=diff_file, stderr=None)
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
    if not diff_file.seek(0, os.SEEK_END):
        print(
            "Note: the diff for this event is empty, so no block counts as changed. %s"
            % (
                "With 'only_changed', no block is checked."
                if only_changed
                else "Every block is still checked, but diff-driven checks "
                "(e.g. affects) have nothing to compare."
            )
        )
    diff_file.seek(0)


def _invoke_blockwatch(args: list[str], stdin_file: IO[bytes] | None = None) -> tuple[int, str]:
    result = subprocess.run(["blockwatch"] + args, stdin=stdin_file, stderr=subprocess.PIPE)
    return result.returncode, result.stderr.decode("utf-8", "replace")


def run_blockwatch(
    command: list[str] | None, args: list[str], only_changed: bool
) -> tuple[int, str]:
    """Run blockwatch, returning its exit code and its captured diagnostics.

    stderr is captured rather than streamed so the diagnostics can be read back
    as annotations, then replayed unchanged: what a reader could previously copy
    out of the log is still there, after the run rather than during it. stdout —
    the verbosity report — is left attached to the log as it happens.
    """
    # The notes above say how the run is about to go, and blockwatch writes
    # its report to the same stdout; unflushed, they would land after it.
    sys.stdout.flush()
    if command is None:
        # Without --diff blockwatch never reads stdin, so there is no
        # diff file descriptor to open or block on.
        return _invoke_blockwatch(args)

    with tempfile.TemporaryFile() as diff:
        _capture_diff(command, diff, only_changed)
        sys.stdout.flush()
        flags = ["--diff"] + (["--only-changed"] if only_changed else [])
        return _invoke_blockwatch(flags + args, stdin_file=diff)
