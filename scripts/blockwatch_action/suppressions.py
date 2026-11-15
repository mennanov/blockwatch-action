"""Collect `blockwatch-suppress:` directives from commit messages and PR descriptions."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

from blockwatch_action.config import JsonObject, env, event_pull_request
from blockwatch_action.github_api import fetch_pull_request_body

# The address prefix blockwatch reads out of commit messages and descriptions.
# Matched here only to name them in the log; blockwatch decides what they do.
SUPPRESS_LINE: re.Pattern[str] = re.compile(r"^[ \t]*blockwatch-suppress:", re.IGNORECASE)


@dataclass
class SuppressionDirective:
    """One `blockwatch-suppress:` line found in a commit message or PR description."""

    source: str  # "commits" or "description"
    line: str
    address: str
    applied: bool
    commit: str = ""
    subject: str = ""
    matched_violations: int = 0

    def to_output_dict(self) -> JsonObject:
        """Serialize this directive for `$GITHUB_OUTPUT` JSON arrays."""
        entry: JsonObject = {
            "line": self.line,
            "address": self.address,
            "applied": self.applied,
        }
        if self.source == "commits":
            return {"commit": self.commit, "subject": self.subject, **entry}
        return entry


@dataclass
class SuppressionCollection:
    """Temporary suppression file (when any source applies) and all discovered directives."""

    file_path: str | None
    directives: list[SuppressionDirective]

    @property
    def lines_suppress(self) -> bool:
        """Whether `blockwatch-suppress:` lines in commits or PR descriptions can take effect."""
        return self.file_path is not None

    def cleanup(self) -> None:
        """Delete the temporary suppression file, if one was created."""
        if not self.file_path:
            return
        try:
            os.unlink(self.file_path)
        except OSError:
            pass

    def __enter__(self) -> SuppressionCollection:
        return self

    def __exit__(self, *args: object) -> None:
        self.cleanup()


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
    return fetch_pull_request_body(payload)


def git_log_command(event_name: str, before: str | None, current_sha: str) -> list[str]:
    """The git log command reading this run's commits for suppressions.

    The range mirrors the diff below exactly, so only the work being checked can
    suppress anything: a commit elsewhere in the repository cannot reach in and
    silence a violation. `-z` separates commits with NUL bytes so multi-line
    bodies parse unambiguously without colliding with commit content.
    """
    fmt = "--format=%h%x00%s%x00%B"
    if event_name == "pull_request":
        # Two dots, not the three the diff uses: this wants the commits the pull
        # request adds, not a patch. The merge commit GitHub checks out is
        # included and harmless, its generated message carrying no address.
        base = env("GITHUB_BASE_REF")
        return ["git", "log", "-z", fmt, "origin/%s..%s" % (base, current_sha)]
    if event_name == "push":
        if before:
            return ["git", "log", "-z", fmt, "%s..%s" % (before, current_sha)]
        # A new branch, or a force-push whose prior tip is gone, leaves no range
        # to enumerate, so only the head commit is read — exactly the commit the
        # diff covers.
        return ["git", "log", "-1", "-z", fmt, current_sha]
    # No push range and no pull request, so the commit being scanned is the only
    # one that can be meant. Reading it keeps a workflow_dispatch re-run of a
    # commit behaving like the push that first checked it.
    return ["git", "log", "-1", "-z", fmt, "HEAD"]


def parse_git_log(raw: bytes) -> list[tuple[str, str, str]]:
    """Decode `-z --format=%h%x00%s%x00%B` output into `(short_sha, subject, body)` tuples."""
    text = raw.decode("utf-8", "replace")
    if not text:
        return []
    parts = text.split("\0")
    return [
        (parts[index], parts[index + 1], parts[index + 2]) for index in range(0, len(parts) - 2, 3)
    ]


def suppression_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.split("\n") if SUPPRESS_LINE.match(line)]


def suppression_address(line: str) -> str:
    _, _, rest = line.partition(":")
    return rest.strip()


def log_directive_lines(header: str, lines: list[str]) -> None:
    """Print `header` followed by each directive line when `lines` is non-empty."""
    if not lines:
        return
    print(header)
    for line in lines:
        print(line)


def collect_commit_suppressions(
    event_name: str,
    before: str | None,
    current_sha: str,
    read_commits: bool,
) -> tuple[list[SuppressionDirective], str | None]:
    """Collect commit-message suppression directives and blockwatch input when enabled."""
    log = subprocess.run(
        git_log_command(event_name, before, current_sha),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    commits: list[tuple[str, str, str]] = parse_git_log(log.stdout) if log.returncode == 0 else []
    directives = [
        SuppressionDirective(
            source="commits",
            line=line,
            address=suppression_address(line),
            applied=read_commits,
            commit=short_sha,
            subject=subject,
        )
        for short_sha, subject, body in commits
        for line in suppression_lines(body)
    ]
    if not read_commits:
        log_directive_lines(
            "Note: suppress_from_commits is false, so these commit lines were ignored:",
            [d.line for d in directives],
        )
        return directives, None
    messages = "\n".join(body for _, _, body in commits) + ("\n" if commits else "")
    return directives, messages


def collect_description_suppressions(
    payload: JsonObject,
    read_description: bool,
) -> tuple[list[SuppressionDirective], str | None]:
    """Collect PR-description suppression directives and blockwatch input when enabled."""
    if read_description:
        body, source = pull_request_body(payload)
        print("Note: pull request description read from %s." % source)
        source_text: str | None = "\n%s\n" % body
    else:
        # Read only the event payload when description suppressions are off,
        # avoiding an API call while still surfacing any attempted directives.
        body = str(event_pull_request(payload).get("body") or "")
        print("Note: pull request description not read: suppress_from_description is false.")
        source_text = None

    directives = [
        SuppressionDirective(
            source="description",
            line=line,
            address=suppression_address(line),
            applied=read_description,
        )
        for line in suppression_lines(body)
    ]
    if not read_description:
        log_directive_lines(
            "Note: suppress_from_description is false, "
            "so these pull request description lines were ignored:",
            [d.line for d in directives],
        )
    return directives, source_text


def _write_temp_suppression_file(text: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    with handle:
        handle.write(text)
    return handle.name


def write_suppression_file(
    event_name: str,
    payload: JsonObject,
    before: str | None,
    current_sha: str,
    read_commits: bool,
    read_description: bool,
) -> SuppressionCollection:
    """Collect the commit messages, and any description, into one file.

    blockwatch reads `blockwatch-suppress: ADDRESS` lines out of any text and
    ignores every other line, so no extraction is needed — a commit message is
    valid input as it stands. This is additive: the `suppress` and
    `suppress_from` inputs still apply.

    Even when a source is turned off, its `blockwatch-suppress:` lines are
    still collected (with `applied=False`) so reviewers see that an attempt
    was made. `file_path` is None when neither source applies to this run, so
    nobody can suppress a violation with a line in a message or description.
    """
    directives, commit_text = collect_commit_suppressions(
        event_name, before, current_sha, read_commits
    )
    sources = [commit_text] if commit_text is not None else []

    if event_name == "pull_request":
        desc_directives, desc_text = collect_description_suppressions(payload, read_description)
        directives.extend(desc_directives)
        if desc_text is not None:
            sources.append(desc_text)

    if not sources:
        return SuppressionCollection(file_path=None, directives=directives)

    file_path = _write_temp_suppression_file("".join(sources))

    # A suppression picked up here changes the exit code without appearing
    # anywhere in the workflow file, so the log is the only place a reader can
    # find out it happened. This only labels the lines; blockwatch remains the
    # one deciding which of them take effect.
    log_directive_lines(
        "Note: suppression addresses found in the commit messages/description under test:",
        [d.line for d in directives if d.applied],
    )
    return SuppressionCollection(file_path=file_path, directives=directives)
