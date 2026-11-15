"""Parse blockwatch JSON/SARIF diagnostics and classify suppressed violations."""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Iterator

from blockwatch_action.config import SUPPRESSION_SOURCES, JsonObject
from blockwatch_action.suppressions import SuppressionDirective


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
    # Where a suppressed violation's suppression came from ("commits",
    # "description", or "workflow" when neither matched). Empty when active.
    suppression_sources: tuple[str, ...] = ()

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
    # than an error (commit/PR-suppressed ones are later elevated to warnings).
    # blockwatch's severities follow LSP: 1 error, 2 warning, 3 information, 4 hint.
    if suppressed:
        return "notice"
    if severity == 2:
        return "warning"
    if severity and severity >= 3:
        return "notice"
    return "error"


def normalize_repo_path(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def directive_matches_violation(address: str, violation: Violation) -> bool:
    """Whether a suppression address covers `violation` under blockwatch's segment rules."""
    if not address or not violation.path:
        return False
    parts = address.split(":")
    if len(parts) > 4 or any(not part for part in parts):
        return False
    if normalize_repo_path(parts[0]) != normalize_repo_path(violation.path):
        return False
    if len(parts) == 1:
        return True
    if not violation.address:
        return False
    v_parts = violation.address.split(":")
    return len(v_parts) >= len(parts) and v_parts[1 : len(parts)] == parts[1:]


def classify_violations(
    violations: list[Violation],
    directives: list[SuppressionDirective],
) -> None:
    """Tag suppressed violations with their source and count matches per directive."""
    suppressed = [violation for violation in violations if violation.suppressed]
    applied = [directive for directive in directives if directive.applied]
    for directive in applied:
        directive.matched_violations = sum(
            1
            for violation in suppressed
            if directive_matches_violation(directive.address, violation)
        )
    for violation in suppressed:
        matched = tuple(
            source
            for source in SUPPRESSION_SOURCES
            if any(
                directive.source == source
                and directive_matches_violation(directive.address, violation)
                for directive in applied
            )
        )
        if matched:
            violation.suppression_sources = matched
            violation.level = "warning"
        else:
            violation.suppression_sources = ("workflow",)


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
    `emit_run_error` annotates it instead.

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
