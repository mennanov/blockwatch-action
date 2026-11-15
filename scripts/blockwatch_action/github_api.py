"""GitHub REST API client for reading pull request bodies and syncing suppression comments."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from blockwatch_action.config import JsonObject, env, event_pull_request

API_TIMEOUT_SECONDS: int = 30
MAX_COMMENT_PAGES: int = 20
_NEXT_LINK: re.Pattern[str] = re.compile(r'<([^>]+)>;\s*rel="next"')


@dataclass(frozen=True)
class PullRequestTarget:
    """Resolved GitHub REST API coordinates and token for a pull request."""

    api_url: str
    repository: str
    number: str
    token: str

    @property
    def pull_url(self) -> str:
        return "%s/repos/%s/pulls/%s" % (self.api_url, self.repository, self.number)

    @property
    def comments_url(self) -> str:
        return "%s/repos/%s/issues/%s/comments" % (self.api_url, self.repository, self.number)

    def comment_url(self, comment_id: int) -> str:
        return "%s/repos/%s/issues/comments/%d" % (self.api_url, self.repository, comment_id)


def resolve_pull_request_target(payload: JsonObject) -> tuple[PullRequestTarget | None, str]:
    """Resolve pull request API coordinates, or return a fallback description when unavailable."""
    pr = event_pull_request(payload)
    number = pr.get("number")
    repository = env("GITHUB_REPOSITORY")
    token = env("GH_TOKEN")
    if not (
        token
        and repository
        and number
        and isinstance(number, (int, str))
        and not isinstance(number, bool)
    ):
        return None, "the event payload"

    api_url = env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not api_url.lower().startswith(("https://", "http://")):
        return None, "the event payload (unsupported API URL scheme)"

    return (
        PullRequestTarget(api_url=api_url, repository=repository, number=str(number), token=token),
        "",
    )


def _github_request(
    method: str,
    url: str,
    token: str,
    payload: JsonObject | None = None,
) -> tuple[Any, str]:
    headers = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "blockwatch-action",
    }
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
        raw = response.read()
        link = response.headers.get("Link") or ""
    parsed = json.loads(raw.decode("utf-8")) if raw else None
    return parsed, link


def _next_page_url(link_header: str, api_url: str) -> str | None:
    match = _NEXT_LINK.search(link_header)
    if not match:
        return None
    candidate = match.group(1)
    base_parts = urllib.parse.urlparse(api_url)
    next_parts = urllib.parse.urlparse(candidate)
    if (
        next_parts.scheme.lower() not in ("http", "https")
        or next_parts.scheme.lower() != base_parts.scheme.lower()
        or next_parts.netloc != base_parts.netloc
    ):
        return None
    return candidate


def fetch_pull_request_body(payload: JsonObject) -> tuple[str, str]:
    """Read the live PR description from the REST API, falling back to the event payload."""
    pr = event_pull_request(payload)
    snapshot = str(pr.get("body") or "")
    target, skip_reason = resolve_pull_request_target(payload)
    if target is None:
        return snapshot, skip_reason

    try:
        document, _ = _github_request("GET", target.pull_url, target.token)
    except urllib.error.HTTPError as error:
        return snapshot, "the event payload (the API answered HTTP %s)" % error.code
    except Exception as error:  # network, DNS, timeout, malformed JSON
        return snapshot, "the event payload (the API read failed: %s)" % error

    body = (document.get("body") if isinstance(document, dict) else "") or ""
    return str(body), "the API, as it reads now"


def _find_sticky_comment(target: PullRequestTarget, marker: str) -> tuple[int, str] | None:
    url: str | None = target.comments_url + "?per_page=100"
    for _ in range(MAX_COMMENT_PAGES):
        if not url:
            return None
        page, link = _github_request("GET", url, target.token)
        if not isinstance(page, list):
            return None
        for item in page:
            if not isinstance(item, dict):
                continue
            comment_id = item.get("id")
            comment_body = item.get("body")
            if (
                isinstance(comment_id, int)
                and not isinstance(comment_id, bool)
                and isinstance(comment_body, str)
                and comment_body.lstrip().startswith(marker)
            ):
                return comment_id, comment_body
        url = _next_page_url(link, target.api_url)
    return None


def _apply_comment_change(
    target: PullRequestTarget,
    existing: tuple[int, str] | None,
    body: str | None,
) -> None:
    if body is None:
        if existing is not None:
            try:
                _github_request("DELETE", target.comment_url(existing[0]), target.token)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
        return

    if existing is None:
        _github_request("POST", target.comments_url, target.token, {"body": body})
    elif existing[1] != body:
        _github_request("PATCH", target.comment_url(existing[0]), target.token, {"body": body})


def sync_pull_request_comment(payload: JsonObject, marker: str, body: str | None) -> None:
    """Create, update, or remove a sticky PR comment without failing the workflow step."""
    target, skip_reason = resolve_pull_request_target(payload)
    if target is None:
        if body is not None:
            print("Note: skipped pull request suppression comment (%s)." % skip_reason)
        return

    try:
        existing = _find_sticky_comment(target, marker)
    except urllib.error.HTTPError as error:
        print(
            "Note: could not read pull request suppression comments (the API answered HTTP %s)."
            % error.code
        )
        return
    except Exception as error:
        print("Note: could not read pull request suppression comments (%s)." % error)
        return

    try:
        _apply_comment_change(target, existing, body)
    except urllib.error.HTTPError as error:
        if error.code == 403:
            print(
                "Note: skipped pull request suppression comment "
                "(HTTP 403 — workflow may lack 'pull-requests: write' permission or be from a fork)."
            )
            return
        print(
            "Note: could not update pull request suppression comment (the API answered HTTP %s)."
            % error.code
        )
    except Exception as error:
        print("Note: could not update pull request suppression comment (%s)." % error)
