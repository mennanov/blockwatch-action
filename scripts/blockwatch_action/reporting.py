"""Render GitHub workflow annotations, GFM job summaries, and step outputs."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

from blockwatch_action.config import RUN_FAILED, SUPPRESSION_SOURCES, JsonObject, env
from blockwatch_action.diagnostics import Violation
from blockwatch_action.github_api import PullRequestLinks
from blockwatch_action.suppressions import SuppressionDirective

# GitHub renders a limited number of annotations per step and drops the rest
# without saying so, so the step stops at the caller's limit and reports the
# remainder. The job summary is rejected outright above 1 MiB, hence its own cap,
# and issue/PR comments are rejected above 65,536 characters.
SUMMARY_ROW_LIMIT: int = 1000
ANNOTATION_DIRECTIVE_LIMIT: int = 20
OUTPUT_JSON_LIMIT: int = 512 * 1024
COMMENT_MARKER: str = "<!-- blockwatch-action:suppressions -->"
COMMENT_ROW_LIMIT: int = 50
COMMENT_CHAR_LIMIT: int = 60000

_SOURCE_LABELS: dict[str, str] = {
    "commits": "commit message",
    "description": "pull request description",
}


@dataclass(frozen=True)
class SuppressionAnnotationText:
    """Annotation title and intro copy for one directive source."""

    source: str
    active_title: str
    ignored_title: str
    active_intro: str
    ignored_intro: str


SUPPRESSION_ANNOTATION_COPY: tuple[SuppressionAnnotationText, ...] = (
    SuppressionAnnotationText(
        source="commits",
        active_title="blockwatch: suppression in commit message",
        ignored_title="blockwatch: ignored suppression in commit message",
        active_intro="Commit message(s) contain blockwatch-suppress directives:",
        ignored_intro=(
            "Commit message(s) contain blockwatch-suppress directives "
            "(ignored because suppress_from_commits is false):"
        ),
    ),
    SuppressionAnnotationText(
        source="description",
        active_title="blockwatch: suppression in pull request description",
        ignored_title="blockwatch: ignored suppression in pull request description",
        active_intro=(
            "Pull request description contains blockwatch-suppress directives "
            "(edits take effect on re-run without a new commit):"
        ),
        ignored_intro=(
            "Pull request description contains blockwatch-suppress directives "
            "(ignored because suppress_from_description is false):"
        ),
    ),
)


def escape_data(value: str) -> str:
    """Percent-encode a workflow command's message.

    A command is delimited text: a raw newline would end it. GitHub's own
    toolkit encodes exactly these, and the runner decodes them again.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    """The same, plus the separators between properties."""
    return escape_data(value).replace(":", "%3A").replace(",", "%2C")


def truncate_text(value: str, limit: int = 72) -> str:
    clean = value.replace("\r", " ").replace("\n", " ").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def _format_directive_entry(directive: SuppressionDirective, run_failed: bool) -> str:
    prefix = (
        "commit %s (%s): " % (directive.commit, truncate_text(directive.subject))
        if directive.commit
        else ""
    )
    suffix = (
        " [matched %d]" % directive.matched_violations
        if directive.applied and not run_failed
        else ""
    )
    return "%s%s%s" % (prefix, directive.line, suffix)


def emit_suppression_annotations(
    directives: list[SuppressionDirective],
    lines_suppress: bool,
    run_failed: bool,
) -> None:
    """Highlight commit and PR-description suppression attempts as workflow annotations.

    Emits `::warning` when commit or description suppressions can reach the run
    (`lines_suppress` is true), and `::notice` when neither source applies.
    Not counted against `annotations_limit`, which caps violation cards only.
    """
    if not directives:
        return
    level = "warning" if lines_suppress else "notice"
    for copy in SUPPRESSION_ANNOTATION_COPY:
        group = [directive for directive in directives if directive.source == copy.source]
        if not group:
            continue
        applied = group[0].applied
        title = copy.active_title if applied else copy.ignored_title
        body_lines = [copy.active_intro if applied else copy.ignored_intro]
        body_lines.extend(
            _format_directive_entry(directive, run_failed)
            for directive in group[:ANNOTATION_DIRECTIVE_LIMIT]
        )
        if len(group) > ANNOTATION_DIRECTIVE_LIMIT:
            body_lines.append(
                "... and %d more (see job summary)." % (len(group) - ANNOTATION_DIRECTIVE_LIMIT)
            )
        print(
            "::%s title=%s::%s"
            % (level, escape_property(title), escape_data("\n".join(body_lines)))
        )


def _format_violation_properties(violation: Violation) -> list[str]:
    properties = []
    if violation.path:
        properties.append("file=%s" % escape_property(violation.path))
        properties.append("line=%s" % violation.start_line)
        properties.append("endLine=%s" % violation.end_line)
        if violation.single_line:
            properties.append("col=%s" % violation.start_column)
            properties.append("endColumn=%s" % violation.end_column)
    properties.append("title=%s" % escape_property("blockwatch: %s" % violation.code))
    return properties


def _source_labels(violation: Violation) -> list[str]:
    return [_SOURCE_LABELS[s] for s in violation.suppression_sources if s in _SOURCE_LABELS]


def _format_violation_message(violation: Violation, lines_suppress: bool) -> str:
    message = escape_data(violation.message)
    by = _source_labels(violation)
    if by:
        message += "%0ASuppressed by: " + ", ".join(by)
    # The address is what a reader needs in order to act on the annotation, and
    # it appears nowhere else on the line. A violation in an unnamed block has
    # none, so nothing is added for it.
    if violation.address:
        # Concatenated, not %-formatted: the literal "%0A" is a newline for
        # the runner and a format specifier for Python.
        encoded_address = escape_data(violation.address)
        if lines_suppress:
            message += "%0ASuppress with: blockwatch-suppress: " + encoded_address
        else:
            message += "%0ASuppress with the suppress input: " + encoded_address
    return message


def emit_annotations(violations: list[Violation], limit: int, lines_suppress: bool) -> None:
    """Print one workflow command per violation, up to `limit`.

    `lines_suppress` is whether a `blockwatch-suppress:` line can reach this
    run. Offering one when it cannot would send the reader to a fix that does
    nothing.
    """
    emitted = 0
    omitted = 0
    for violation in violations:
        if emitted >= limit:
            omitted += 1
            continue
        properties = _format_violation_properties(violation)
        message = _format_violation_message(violation, lines_suppress)
        print("::%s %s::%s" % (violation.level, ",".join(properties), message))
        emitted += 1

    if omitted:
        print(
            "::notice title=blockwatch::%d further violation(s) were not annotated "
            "(annotations_limit=%d). All of them are in the job log above." % (omitted, limit)
        )


def emit_run_error(stderr: str) -> None:
    """Annotate a run blockwatch could not finish, with blockwatch's own error.

    Such a run reports no violation, so without this the check run shows only
    the exit code and the reason is in the job log alone. The error names no
    line to point at, so the annotation is attached to no file. It is not a
    violation either, so `annotations_limit` does not count it.
    """
    message = stderr.strip() or "blockwatch exited with code %d and gave no reason." % RUN_FAILED
    title = escape_property("blockwatch: the run could not finish")
    print("::error title=%s::%s" % (title, escape_data(message)))


def escape_html_cell(value: str) -> str:
    """Escape untrusted text for inclusion inside `<code>...</code>` in a GFM table."""
    clean = value.replace("\r", " ").replace("\n", " ")
    return html.escape(clean).replace("|", "&#124;")


def _html_code(value: str) -> str:
    return "<code>%s</code>" % escape_html_cell(value)


def _escape_html_multiline(value: str) -> str:
    """Escape untrusted text for Markdown table cells while preserving line breaks via `<br>`."""
    escaped = html.escape(value).replace("|", "&#124;")
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _directive_source_cell(directive: SuppressionDirective) -> str:
    if directive.source != "commits":
        return "pull request description"
    if directive.commit:
        return "commit %s (%s)" % (
            _html_code(directive.commit),
            escape_html_cell(truncate_text(directive.subject)),
        )
    return "commit message"


def _directive_status_cell(directive: SuppressionDirective, run_failed: bool) -> str:
    if not directive.applied:
        flag = (
            "suppress_from_commits"
            if directive.source == "commits"
            else "suppress_from_description"
        )
        return "ignored (<code>%s</code> is false)" % flag
    if run_failed:
        return "applied"
    noun = "violation" if directive.matched_violations == 1 else "violations"
    return "applied (matched %d %s)" % (directive.matched_violations, noun)


def format_directive_row(directive: SuppressionDirective, run_failed: bool) -> str:
    """Format one row in the job summary's suppression directives table."""
    return "| %s | %s | %s |" % (
        _directive_source_cell(directive),
        _directive_status_cell(directive, run_failed),
        _html_code(directive.line),
    )


def _summary_truncation_note(total: int, shown: int, noun: str) -> list[str]:
    if total <= shown:
        return []
    return ["", "_%d further %s(s) not listed._" % (total - shown, noun)]


def directives_summary_section(
    directives: list[SuppressionDirective],
    lines_suppress: bool,
    run_failed: bool,
) -> str:
    """Build the Markdown section for suppression directives."""
    if lines_suppress:
        alert = [
            "> [!WARNING]",
            (
                "> Suppression directives were found in commit messages "
                "or the pull request description."
            ),
        ]
    else:
        alert = [
            "> [!NOTE]",
            (
                "> Suppression directives were found in commit messages or the pull request "
                "description, but were ignored because these sources are disabled for this run."
            ),
        ]
    rows = [
        format_directive_row(directive, run_failed) for directive in directives[:SUMMARY_ROW_LIMIT]
    ]
    lines = (
        alert
        + [
            "",
            "### blockwatch: %d suppression directive(s)" % len(directives),
            "",
            "| Source | Status | Directive |",
            "| --- | --- | --- |",
        ]
        + rows
        + _summary_truncation_note(len(directives), len(rows), "directive")
    )
    return "\n".join(lines)


def format_violation_row(violation: Violation) -> str:
    """Format one row in the job summary's violations table."""
    location = (
        ("%s:%s" % (violation.path, violation.start_line)).replace("|", r"\|")
        if violation.path
        else "—"
    )
    # Markdown here, not a workflow command: a pipe would end the cell and a
    # newline has to become a line break.
    cell = (
        violation.message.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("|", r"\|")
        .replace("\n", "<br>")
    )
    if not violation.suppressed:
        code = violation.code
    elif any(s in SUPPRESSION_SOURCES for s in violation.suppression_sources):
        code = "%s (suppressed via %s)" % (
            violation.code,
            ", ".join(violation.suppression_sources),
        )
    else:
        code = violation.code + " (suppressed)"
    address = (violation.address or "—").replace("|", r"\|")
    return "| `%s` | %s | %s | %s |" % (location, code.replace("|", r"\|"), cell, address)


def violations_summary_section(violations: list[Violation]) -> str:
    """Build the Markdown job summary section for blockwatch violations."""
    rows = [format_violation_row(violation) for violation in violations[:SUMMARY_ROW_LIMIT]]
    lines = (
        [
            "### blockwatch: %d violation(s)" % len(violations),
            "",
            "| Location | Validator | Message | Suppress address |",
            "| --- | --- | --- | --- |",
        ]
        + rows
        + _summary_truncation_note(len(violations), len(rows), "violation")
    )
    return "\n".join(lines)


def write_summary(
    violations: list[Violation],
    directives: list[SuppressionDirective],
    lines_suppress: bool,
    run_failed: bool,
) -> None:
    path = env("GITHUB_STEP_SUMMARY")
    if not path or (not violations and not directives):
        return

    sections: list[str] = []
    if directives:
        sections.append(directives_summary_section(directives, lines_suppress, run_failed))
    if violations:
        sections.append(violations_summary_section(violations))

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n\n".join(sections) + "\n")


def _html_link(url: str | None, label_html: str) -> str:
    if not url:
        return label_html
    return '<a href="%s">%s</a>' % (html.escape(url, quote=True), label_html)


def _comment_directive_source(directive: SuppressionDirective, links: PullRequestLinks) -> str:
    if directive.source != "commits":
        return "pull request description"
    if directive.commit:
        commit_ref = _html_link(
            links.commit_url(directive.commit_sha),
            _html_code(directive.commit),
        )
        subject = escape_html_cell(truncate_text(directive.subject))
        return "commit %s (%s)" % (commit_ref, subject) if subject else "commit %s" % commit_ref
    return "commit message"


def _format_comment_violation_row(violation: Violation, links: PullRequestLinks) -> str:
    location_label = _html_code("%s:%s" % (violation.path, violation.start_line))
    location = (
        _html_link(
            links.code_url(violation.path, violation.start_line, violation.end_line),
            location_label,
        )
        if violation.path
        else "—"
    )
    message = _escape_html_multiline(violation.message)
    sources = "<br>".join(
        _comment_directive_source(directive, links) for directive in violation.matched_directives
    )
    directives = "<br>".join(
        _html_code(directive.line) for directive in violation.matched_directives
    )
    return "| %s | %s | %s | %s |" % (location, message, sources or "—", directives or "—")


def _truncate_comment_body(body: str, limit: int = COMMENT_CHAR_LIMIT) -> str:
    if len(body) <= limit:
        return body
    note = (
        "\n\n_Comment truncated due to GitHub size limits; "
        "see the workflow job summary for the full list._\n"
    )
    budget = limit - len(note)
    kept: list[str] = []
    used = 0
    for line in body.splitlines():
        addition = len(line) + (1 if kept else 0)
        if used + addition > budget:
            break
        kept.append(line)
        used += addition
    return "\n".join(kept) + note


def render_pr_comment(violations: list[Violation], links: PullRequestLinks) -> str | None:
    """Build the sticky PR comment listing actually suppressed violations, or None when empty."""
    suppressed = [
        violation for violation in violations if violation.suppressed and _source_labels(violation)
    ]
    if not suppressed:
        return None
    rows = [
        _format_comment_violation_row(violation, links)
        for violation in suppressed[:COMMENT_ROW_LIMIT]
    ]
    lines = (
        [
            COMMENT_MARKER,
            "",
            "> [!WARNING]",
            "> blockwatch: %d suppressed violation(s)" % len(suppressed),
            "",
            "| Location | Message | Source | Suppression directive |",
            "| --- | --- | --- | --- |",
        ]
        + rows
        + _summary_truncation_note(len(suppressed), len(rows), "violation")
    )
    return _truncate_comment_body("\n".join(lines) + "\n")


def bounded_json_array(items: list[JsonObject], limit: int = OUTPUT_JSON_LIMIT) -> str:
    """Serialize `items` as single-line JSON without exceeding `limit` bytes."""
    encoded = json.dumps(items)
    if len(encoded.encode("utf-8")) <= limit:
        return encoded
    low, high = 0, len(items)
    while low < high:
        mid = (low + high + 1) // 2
        if len(json.dumps(items[:mid]).encode("utf-8")) <= limit:
            low = mid
        else:
            high = mid - 1
    return json.dumps(items[:low])


def write_outputs(directives: list[SuppressionDirective]) -> None:
    """Write detected suppression directives to `$GITHUB_OUTPUT` when set."""
    path = env("GITHUB_OUTPUT")
    if not path:
        return

    outputs = (("commit_suppressions", "commits"), ("description_suppressions", "description"))
    with open(path, "a", encoding="utf-8") as handle:
        for key, source in outputs:
            items = [d.to_output_dict() for d in directives if d.source == source]
            value = bounded_json_array(items).replace("\r", " ").replace("\n", " ")
            handle.write("%s=%s\n" % (key, value))
