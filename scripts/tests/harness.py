"""Shared fixtures, GitHub Action environment harness, and fake API server."""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from typing import Any, Dict

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_PY = os.path.join(SCRIPTS_DIR, "run.py")
ACTION_YML = os.path.join(os.path.dirname(SCRIPTS_DIR), "action.yml")
JsonObject = Dict[str, Any]

# A sorted block, and the same block with its entries swapped.
SORTED_FRUIT = '<!-- <block name="fruit" keep-sorted> -->\n- apple\n- banana\n<!-- </block> -->\n'
UNSORTED_FRUIT = '<!-- <block name="fruit" keep-sorted> -->\n- banana\n- apple\n<!-- </block> -->\n'
SORTED_VEG = '<!-- <block name="veg" keep-sorted> -->\n- carrot\n- potato\n<!-- </block> -->\n'
UNSORTED_VEG = '<!-- <block name="veg" keep-sorted> -->\n- potato\n- carrot\n<!-- </block> -->\n'


def pinned_version() -> str:
    """The blockwatch version action.yml installs, from its "Set blockwatch version" step."""
    with open(ACTION_YML, encoding="utf-8") as handle:
        match = re.search(r'"version=([^"]+)"', handle.read())
    if match is None:
        raise RuntimeError("No pinned blockwatch version found in %s." % ACTION_YML)
    return match.group(1)


@functools.lru_cache(maxsize=1)
def ensure_pinned_blockwatch() -> None:
    pinned = pinned_version()
    if shutil.which("blockwatch") is None:
        raise RuntimeError(
            "blockwatch is not on PATH. These tests feed the action real diagnostics "
            "rather than a frozen copy of them. Install it with: cargo binstall blockwatch@%s"
            % pinned
        )

    # The tests check run.py against the blockwatch the action installs. Another
    # version can fail them, or pass them, for a reason no consumer will see.
    version = subprocess.run(
        ["blockwatch", "--version"], capture_output=True, text=True, check=True
    ).stdout
    installed = version.strip().rpartition(" ")[2]
    if installed != pinned:
        raise RuntimeError(
            "blockwatch %s is on PATH, but action.yml pins %s. Install it with: "
            "cargo binstall blockwatch@%s" % (installed, pinned, pinned)
        )


def _read_optional(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class Annotation:
    """One `::level key=value,...::message` line, parsed for assertions."""

    def __init__(self, line: str) -> None:
        head, _, self.message = line[2:].partition("::")
        level, _, properties = head.partition(" ")
        self.level = level
        self.properties: dict[str, str] = {}
        for pair in properties.split(",") if properties else []:
            key, _, value = pair.partition("=")
            self.properties[key] = value

    @property
    def address(self) -> str:
        """The suppression address the annotation offers, if it carries one."""
        _, _, address = self.message.partition("Suppress with: blockwatch-suppress: ")
        return address

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Annotation):
            return NotImplemented
        return (self.level, self.properties, self.message) == (
            other.level,
            other.properties,
            other.message,
        )

    def __repr__(self) -> str:
        return "Annotation(%s, %r, %r)" % (self.level, self.properties, self.message)


class Result:
    def __init__(
        self,
        process: subprocess.CompletedProcess[str],
        summary: str = "",
        outputs_raw: str = "",
    ) -> None:
        self.exit_code = process.returncode
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.summary = summary
        self.outputs: dict[str, str] = {}
        for line in outputs_raw.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                self.outputs[key] = value

    def json_output(self, name: str) -> list[JsonObject]:
        parsed: list[JsonObject] = json.loads(self.outputs[name])
        return parsed

    @property
    def annotations(self) -> list[Annotation]:
        return [Annotation(line) for line in self.stdout.splitlines() if line.startswith("::")]

    @property
    def levels(self) -> list[str]:
        return [a.level for a in self.annotations]

    @property
    def diagnostics(self) -> JsonObject:
        """What blockwatch reported, as the script replayed it to the log.

        Comparing the annotations against this is what keeps these tests about
        the translation rather than about blockwatch's numbers.
        """
        document: JsonObject = json.loads(self.stderr[self.stderr.index("{") :])
        return document


class FakeGitHubApi:
    """Minimal localhost HTTP server emulating PR description and issue comment APIs."""

    def __init__(
        self,
        status: int = 200,
        body: str | None = "",
        raw_body: bytes | None = None,
        comments: list[JsonObject] | None = None,
        comments_status: int | None = None,
        comment_write_status: int = 200,
        comments_page_size: int | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.raw_body = raw_body
        self.comments: list[JsonObject] = list(comments or [])
        self.comments_status = status if comments_status is None else comments_status
        self.comment_write_status = comment_write_status
        self.comments_page_size = comments_page_size
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.comment_requests: list[tuple[str, str, dict[str, str], JsonObject | None]] = []
        self._next_id = max([int(c.get("id", 0)) for c in self.comments] + [0]) + 1
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send_json(
                self, code: int, payload: bytes, extra_headers: dict[str, str] | None = None
            ) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in (extra_headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def _read_json_body(self) -> JsonObject | None:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return None
                raw = self.rfile.read(length)
                parsed = json.loads(raw.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else None

            def _headers(self) -> dict[str, str]:
                return {key.lower(): value for key, value in self.headers.items()}

            def _comment_id(self) -> int:
                return int(self.path.rstrip("/").rpartition("/")[2])

            def _record_comment_write(self, method: str, data: JsonObject | None = None) -> bool:
                parent.comment_requests.append((method, self.path, self._headers(), data))
                if parent.comment_write_status >= 400:
                    self._send_json(parent.comment_write_status, b'{"message":"forbidden"}')
                    return False
                return True

            def _handle_comments_get(self, parsed: urllib.parse.ParseResult) -> None:
                parent.comment_requests.append(("GET", self.path, self._headers(), None))
                if parent.comments_status >= 400:
                    self._send_json(parent.comments_status, b'{"message":"error"}')
                    return
                query = urllib.parse.parse_qs(parsed.query)
                page = int(query.get("page", ["1"])[0])
                page_size = parent.comments_page_size or len(parent.comments) or 100
                start = (page - 1) * page_size
                items = parent.comments[start : start + page_size]
                extra: dict[str, str] = {}
                if start + page_size < len(parent.comments):
                    extra["Link"] = '<%s%s?page=%d>; rel="next"' % (
                        parent.url,
                        parsed.path,
                        page + 1,
                    )
                self._send_json(
                    parent.comments_status,
                    json.dumps(items).encode("utf-8"),
                    extra,
                )

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path.endswith("/comments"):
                    self._handle_comments_get(parsed)
                    return

                parent.requests.append((self.path, self._headers()))
                payload = (
                    parent.raw_body
                    if parent.raw_body is not None
                    else json.dumps({"body": parent.body}).encode("utf-8")
                )
                self._send_json(parent.status, payload)

            def do_POST(self) -> None:
                data = self._read_json_body()
                if not self._record_comment_write("POST", data):
                    return
                comment: JsonObject = {
                    "id": parent._next_id,
                    "body": str((data or {}).get("body") or ""),
                }
                parent._next_id += 1
                parent.comments.append(comment)
                self._send_json(201, json.dumps(comment).encode("utf-8"))

            def do_PATCH(self) -> None:
                data = self._read_json_body()
                if not self._record_comment_write("PATCH", data):
                    return
                comment_id = self._comment_id()
                for item in parent.comments:
                    if item.get("id") == comment_id:
                        item["body"] = str((data or {}).get("body") or "")
                        self._send_json(200, json.dumps(item).encode("utf-8"))
                        return
                self._send_json(404, b'{"message":"not found"}')

            def do_DELETE(self) -> None:
                if not self._record_comment_write("DELETE"):
                    return
                comment_id = self._comment_id()
                before = len(parent.comments)
                parent.comments = [item for item in parent.comments if item.get("id") != comment_id]
                code = 204 if len(parent.comments) < before else 404
                self._send_json(code, b"")

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return "http://%s:%s" % (host, port)

    def __enter__(self) -> FakeGitHubApi:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class ActionTestCase(unittest.TestCase):
    """A throwaway repository, plus a way to run the script against it."""

    def setUp(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="blockwatch-action-test-")
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.repo = os.path.join(self.workspace, "repo")
        os.makedirs(os.path.join(self.repo, "docs"))
        self.temp_dir = os.path.join(self.workspace, "tmp")
        os.makedirs(self.temp_dir)
        self.summary_path = os.path.join(self.workspace, "summary.md")
        self.output_path = os.path.join(self.workspace, "output.txt")

        # HOME points into the workspace and the system config is ignored, so no
        # global git setting can change what these tests see.
        self.git_env = dict(os.environ)
        self.git_env.update(
            HOME=self.workspace,
            GIT_CONFIG_NOSYSTEM="1",
            TMPDIR=self.temp_dir,
            TEMP=self.temp_dir,
            TMP=self.temp_dir,
        )

        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")
        self.write("docs/a.md", SORTED_FRUIT)
        self.write("docs/b.md", SORTED_VEG)
        self.write("docs/notes.md", "Prose with no blocks in it.\n")
        self.base_sha = self.commit("initial")

    def git(self, *args: str, stdin_text: str | None = None) -> str:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=self.repo,
            env=self.git_env,
            input=stdin_text,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def write(self, relative_path: str, content: str) -> None:
        target = os.path.join(self.repo, relative_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)

    def write_workspace_file(self, relative_path: str, content: str) -> str:
        """Write an auxiliary file in `self.workspace` outside the git repository."""
        target = os.path.join(self.workspace, relative_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
        return target

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-F", "-", stdin_text=message)
        return self.git("rev-parse", "HEAD")

    def break_fruit(self, message: str = "break the fruit order") -> str:
        self.write("docs/a.md", UNSORTED_FRUIT)
        return self.commit(message)

    def break_both(self, message: str = "break both") -> str:
        self.write("docs/a.md", UNSORTED_FRUIT)
        self.write("docs/b.md", UNSORTED_VEG)
        return self.commit(message)

    def commit_notes(self, message: str = "revise notes", content: str = "Clean prose.\n") -> str:
        self.write("docs/notes.md", content)
        return self.commit(message)

    def run_action(
        self, event: JsonObject | None = None, event_name: str = "push", **inputs: str
    ) -> Result:
        for path in (self.summary_path, self.output_path):
            if os.path.exists(path):
                os.remove(path)

        event_path = os.path.join(self.workspace, "event.json")
        with open(event_path, "w", encoding="utf-8") as handle:
            json.dump(event if event is not None else {"before": self.base_sha}, handle)

        # Inherited INPUT_* would leak a developer's shell into the run.
        environment = {k: v for k, v in self.git_env.items() if not k.startswith("INPUT_")}
        environment.update(
            GITHUB_EVENT_NAME=event_name,
            GITHUB_SHA=self.git("rev-parse", "HEAD"),
            GITHUB_STEP_SUMMARY=self.summary_path,
            GITHUB_OUTPUT=self.output_path,
            GITHUB_EVENT_PATH=event_path,
            # Empty on purpose: with no token the script never reaches the network.
            GH_TOKEN="",
        )
        environment.update(inputs)

        process = subprocess.run(
            [sys.executable, RUN_PY],
            env=environment,
            capture_output=True,
            text=True,
            cwd=self.repo,
        )
        return Result(
            process,
            summary=_read_optional(self.summary_path),
            outputs_raw=_read_optional(self.output_path),
        )

    def run_pull_request(
        self,
        body: str | None = "",
        *,
        number: int = 1,
        base_ref: str = "main",
        sync_base: bool = True,
        **inputs: str,
    ) -> Result:
        if sync_base:
            self.git("update-ref", "refs/remotes/origin/%s" % base_ref, self.base_sha)
        return self.run_action(
            event={"pull_request": {"number": number, "body": body}},
            event_name="pull_request",
            GITHUB_BASE_REF=base_ref,
            **inputs,
        )

    def run_against_api(
        self,
        api: FakeGitHubApi,
        snapshot_body: str | None = "",
        *,
        number: int = 1,
        repository: str = "acme/widgets",
        token: str = "token-123",
        sync_base: bool = True,
        **inputs: str,
    ) -> Result:
        """Execute a pull request run targeting an already-running `FakeGitHubApi`."""
        return self.run_pull_request(
            snapshot_body,
            number=number,
            sync_base=sync_base,
            GITHUB_API_URL=api.url,
            GH_TOKEN=token,
            GITHUB_REPOSITORY=repository,
            **inputs,
        )

    def run_pr_api(
        self,
        *,
        status: int = 200,
        api_body: str | None = "",
        raw_body: bytes | None = None,
        comments: list[JsonObject] | None = None,
        comments_status: int | None = None,
        comment_write_status: int = 200,
        comments_page_size: int | None = None,
        snapshot_body: str | None = "",
        number: int = 1,
        repository: str = "acme/widgets",
        token: str = "token-123",
        sync_base: bool = True,
        **inputs: str,
    ) -> tuple[Result, FakeGitHubApi]:
        with FakeGitHubApi(
            status=status,
            body=api_body,
            raw_body=raw_body,
            comments=comments,
            comments_status=comments_status,
            comment_write_status=comment_write_status,
            comments_page_size=comments_page_size,
        ) as api:
            result = self.run_against_api(
                api,
                snapshot_body,
                number=number,
                repository=repository,
                token=token,
                sync_base=sync_base,
                **inputs,
            )
        return result, api
