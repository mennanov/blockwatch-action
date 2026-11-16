"""Action inputs, environment parsing, event payload helpers, and shared constants."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict

# `from __future__ import annotations` is what lets the modern spellings below
# (`list[str]`, `list[str] | None`) work on the oldest Python the README claims:
# annotations are stored as strings and never evaluated at runtime.
#
# This alias is the exception, and the reason it is spelled `Dict` and not
# `dict`: it is an ordinary assignment, evaluated when the module loads, and
# `dict[str, Any]` is only subscriptable from 3.9.
JsonObject = Dict[str, Any]

COMMIT_SOURCE: str = "commits"
DESCRIPTION_SOURCE: str = "description"
SUPPRESSION_SOURCES: tuple[str, str] = (COMMIT_SOURCE, DESCRIPTION_SOURCE)

# What blockwatch exits with, since 0.11.0, when the run cannot finish: a bad
# flag, config file or tag, an unreadable file. 1 means only a violation.
RUN_FAILED: int = 2
DEFAULT_ANNOTATIONS_LIMIT: int = 50


class Failure(Exception):
    """An input or environment problem that should fail the step by itself."""


@dataclass(frozen=True)
class ActionConfig:
    """Validated boolean and reporting configuration read from step inputs."""

    annotations: bool
    annotations_limit: int
    summary: bool
    comment: bool
    only_changed: bool
    suppress_from_commits: bool
    suppress_from_description: bool
    output_format: str


def env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


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
        return DEFAULT_ANNOTATIONS_LIMIT
    if not token.isdigit():
        raise Failure("annotations_limit must be a whole number, got %r" % token)
    return int(token)


def read_config() -> ActionConfig:
    """Parse and validate all typed step inputs from environment variables."""
    return ActionConfig(
        annotations=parse_bool("annotations", env("INPUT_ANNOTATIONS"), default=True),
        annotations_limit=parse_limit(env("INPUT_ANNOTATIONS_LIMIT")),
        summary=parse_bool("summary", env("INPUT_SUMMARY"), default=True),
        comment=parse_bool("comment", env("INPUT_COMMENT"), default=True),
        only_changed=parse_bool("only_changed", env("INPUT_ONLY_CHANGED")),
        suppress_from_commits=parse_bool(
            "suppress_from_commits", env("INPUT_SUPPRESS_FROM_COMMITS"), default=True
        ),
        suppress_from_description=parse_bool(
            "suppress_from_description", env("INPUT_SUPPRESS_FROM_DESCRIPTION"), default=True
        ),
        output_format=scalar(env("INPUT_FORMAT")),
    )


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


def escape_data(value: str) -> str:
    """Percent-encode a workflow command's message."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def dict_field(obj: JsonObject, key: str) -> JsonObject:
    """Return a nested dict field from `obj`, or `{}` if absent or not a dict."""
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def event_pull_request(payload: JsonObject) -> JsonObject:
    """Return the `pull_request` object from an event payload, or `{}` if absent."""
    return dict_field(payload, "pull_request")
