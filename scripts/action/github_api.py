"""GitHub REST API client for reading pull request bodies and syncing suppression comments."""

from __future__ import annotations

import json
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import IO, Any

from action.config import JsonObject, dict_field, env, escape_data, event_pull_request

API_TIMEOUT_SECONDS: int = 30
MAX_COMMENT_PAGES: int = 20
_NEXT_LINK: re.Pattern[str] = re.compile(r'<([^>]+)>;\s*rel="next"')
_SAFE_SHA: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _http_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    stripped = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(stripped)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return stripped


def _repo_web_url(server_url: str, repository: object) -> str:
    if not server_url or not isinstance(repository, str):
        return ""
    slug = repository.strip("/")
    return "%s/%s" % (server_url, urllib.parse.quote(slug, safe="/")) if slug else ""


def _pr_number(pr: JsonObject) -> str:
    number = pr.get("number")
    if not number or isinstance(number, bool) or not isinstance(number, (int, str)):
        return ""
    return str(number)


@dataclass(frozen=True)
class PullRequestLinks:
    """Resolved GitHub web URLs for linking PR comment locations and commit diffs."""

    pr_url: str
    repo_url: str
    blob_repo_url: str
    head_sha: str

    def code_url(self, path: str, start_line: int, end_line: int) -> str | None:
        """Build a blob permalink to the flagged line range on the PR head revision."""
        if not path or not self.blob_repo_url or not self.head_sha:
            return None
        rel_path = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
        if not rel_path or rel_path == "." or rel_path.startswith("../"):
            return None
        encoded = urllib.parse.quote(rel_path, safe="/")
        anchor = (
            "#L%d-L%d" % (start_line, end_line) if end_line > start_line else "#L%d" % start_line
        )
        return "%s/blob/%s/%s%s" % (self.blob_repo_url, self.head_sha, encoded, anchor)

    def commit_url(self, commit_sha: str) -> str | None:
        """Build a PR commit diff link (falling back to repo commit link) for `commit_sha`."""
        if not commit_sha or not _SAFE_SHA.match(commit_sha):
            return None
        if self.pr_url:
            return "%s/commits/%s" % (self.pr_url, commit_sha)
        if self.repo_url:
            return "%s/commit/%s" % (self.repo_url, commit_sha)
        return None


def pull_request_links(payload: JsonObject) -> PullRequestLinks:
    """Resolve GitHub web URLs for code lines and commit diffs on a pull request."""
    server_url = _http_url(env("GITHUB_SERVER_URL", "https://github.com"))
    repo_url = _repo_web_url(server_url, env("GITHUB_REPOSITORY"))

    pr = event_pull_request(payload)
    number = _pr_number(pr)
    pr_url = _http_url(pr.get("html_url"))
    if not pr_url and repo_url and number:
        pr_url = "%s/pull/%s" % (repo_url, number)

    head = dict_field(pr, "head")
    head_repo = dict_field(head, "repo")
    head_repo_url = _http_url(head_repo.get("html_url")) or _repo_web_url(
        server_url, head_repo.get("full_name")
    )

    # Line numbers come from the checked-out revision (the merge commit on PRs),
    # which lives on the base repository even for fork PRs. Only fall back to
    # `pull_request.head` when `GITHUB_SHA` is unavailable.
    checkout_sha = env("GITHUB_SHA").strip()
    if _SAFE_SHA.match(checkout_sha):
        head_sha = checkout_sha
        blob_repo_url = repo_url or head_repo_url
    else:
        raw_head_sha = str(head.get("sha") or "").strip()
        head_sha = raw_head_sha if _SAFE_SHA.match(raw_head_sha) else ""
        blob_repo_url = head_repo_url or repo_url

    return PullRequestLinks(
        pr_url=pr_url,
        repo_url=repo_url,
        blob_repo_url=blob_repo_url,
        head_sha=head_sha,
    )


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
    """Resolve pull request API coordinates, returning an empty target and optional reason otherwise."""
    number = _pr_number(event_pull_request(payload))
    repository = env("GITHUB_REPOSITORY")
    token = env("GH_TOKEN")
    if not (token and repository and number):
        return None, ""

    api_url = env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not api_url.lower().startswith(("https://", "http://")):
        return None, "unsupported API URL scheme"

    return (
        PullRequestTarget(api_url=api_url, repository=repository, number=number, token=token),
        "",
    )


def _api_error_text(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return "the API answered HTTP %s" % error.code
    return str(error)


def _same_origin(base_url: str, candidate_url: str) -> bool:
    base = urllib.parse.urlparse(base_url)
    candidate = urllib.parse.urlparse(candidate_url)
    return (
        candidate.scheme.lower() in ("http", "https")
        and candidate.scheme.lower() == base.scheme.lower()
        and candidate.netloc == base.netloc
    )


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects that leave the request origin so `Authorization` cannot leak."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        if not _same_origin(req.full_url, resolved):
            fp.close()
            raise urllib.error.URLError("blocked cross-origin redirect from GitHub API request")
        return super().redirect_request(req, fp, code, msg, headers, resolved)


_API_OPENER: urllib.request.OpenerDirector = urllib.request.build_opener(
    _SameOriginRedirectHandler()
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
    with _API_OPENER.open(request, timeout=API_TIMEOUT_SECONDS) as response:
        raw = response.read()
        link = response.headers.get("Link") or ""
    parsed = json.loads(raw.decode("utf-8")) if raw else None
    return parsed, link


def _next_page_url(link_header: str, api_url: str) -> str | None:
    match = _NEXT_LINK.search(link_header)
    if not match:
        return None
    candidate = match.group(1)
    return candidate if _same_origin(api_url, candidate) else None


def fetch_pull_request_body(payload: JsonObject) -> tuple[str, str]:
    """Read the live PR description from the REST API, falling back to the event payload.

    The event payload is a snapshot taken when the run was created, so re-running
    a workflow replays stale text and would hide edits made after the push. The
    live copy replaces the snapshot rather than merging with it so removals also
    take effect.
    """
    pr = event_pull_request(payload)
    snapshot = str(pr.get("body") or "")
    target, skip_reason = resolve_pull_request_target(payload)
    if target is None:
        detail = " (%s)" % skip_reason if skip_reason else ""
        return snapshot, "the event payload" + detail

    try:
        document, _ = _github_request("GET", target.pull_url, target.token)
    except urllib.error.HTTPError as error:
        return snapshot, "the event payload (%s)" % _api_error_text(error)
    except Exception as error:  # network, DNS, timeout, malformed JSON
        return snapshot, "the event payload (the API read failed: %s)" % error

    body = (document.get("body") if isinstance(document, dict) else "") or ""
    return str(body), "the API, as it reads now"


def _is_bot_comment(item: JsonObject) -> bool:
    user = dict_field(item, "user")
    user_type = user.get("type")
    if user_type == "Bot":
        return True
    login = user.get("login")
    return user_type is None and isinstance(login, str) and login.endswith("[bot]")


def _find_sticky_comment(target: PullRequestTarget, marker: str) -> tuple[int, str] | None:
    url: str | None = target.comments_url + "?per_page=100"
    for _ in range(MAX_COMMENT_PAGES):
        if not url:
            return None
        page, link = _github_request("GET", url, target.token)
        if not isinstance(page, list):
            return None
        for item in page:
            if not isinstance(item, dict) or not _is_bot_comment(item):
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
    if url:
        raise RuntimeError(
            "exceeded %d comment pages without finding sticky comment" % MAX_COMMENT_PAGES
        )
    return None


def _delete_comment(target: PullRequestTarget, comment_id: int) -> None:
    try:
        _github_request("DELETE", target.comment_url(comment_id), target.token)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise


def _apply_comment_change(
    target: PullRequestTarget,
    existing: tuple[int, str] | None,
    body: str | None,
) -> None:
    if existing is None:
        if body is not None:
            _github_request("POST", target.comments_url, target.token, {"body": body})
        return

    comment_id, existing_body = existing
    if body is None:
        _delete_comment(target, comment_id)
        return
    if existing_body != body:
        _github_request("PATCH", target.comment_url(comment_id), target.token, {"body": body})


def _warn(message: str) -> None:
    print("::warning title=blockwatch::%s" % escape_data(message))


def sync_pull_request_comment(payload: JsonObject, marker: str, body: str | None) -> None:
    """Create, update, or remove a sticky PR comment without failing the workflow step."""
    target, skip_reason = resolve_pull_request_target(payload)
    if target is None:
        if body is not None:
            detail = skip_reason or "missing token, repository, or pull request number"
            _warn("Skipped pull request suppression comment (%s)." % detail)
        return

    try:
        existing = _find_sticky_comment(target, marker)
    except Exception as error:
        _warn("Could not read pull request suppression comments (%s)." % _api_error_text(error))
        return

    try:
        _apply_comment_change(target, existing, body)
    except Exception as error:
        if isinstance(error, urllib.error.HTTPError) and error.code == 403:
            _warn(
                "Skipped pull request suppression comment "
                "(HTTP 403 — workflow may lack 'pull-requests: write' permission or be from a fork)."
            )
            return
        _warn("Could not update pull request suppression comment (%s)." % _api_error_text(error))
