# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single **composite GitHub Action** that installs and runs the [`blockwatch`](https://github.com/mennanov/blockwatch) Rust CLI against a repository's git diff. There is no build step. `action.yml` declares the inputs and the six install steps; the seventh step's logic — argument building, suppression sources, the diff, and the reporting — lives in [`scripts/run.py`](scripts/run.py), which is where almost every change belongs. Everything else (README, test workflow, dependabot) supports the two.

## Commands

```shell
# Simulate an event — the action takes a different diff path per event
act workflow_dispatch -W .github/workflows/local-test.yml
# pull_request also needs a payload: act synthesizes one with no base ref, so
# the diff resolves `origin/...<sha>` and dies on "ambiguous argument". Name a
# base that is actually behind the head, or the diff comes out empty instead.
printf '{"pull_request": {"base": {"ref": "BASE_BRANCH"}, "head": {"ref": "main"}}}' > /tmp/pr.json
act pull_request -W .github/workflows/local-test.yml -e /tmp/pr.json

# push needs a payload: act synthesizes one with no `before`, so the diff comes
# out empty and every step dies on "diff in stdin is empty." before it runs
# blockwatch. Give it two real revisions instead.
printf '{"before": "%s", "after": "%s"}' "$(git rev-parse HEAD~1)" "$(git rev-parse HEAD)" > /tmp/push.json
act push -W .github/workflows/local-test.yml -e /tmp/push.json
```

The bare `act -W ...` defaults to `push`, so it hits that same empty-diff failure — use one of the forms above.

`act` cannot run a single step; `local-test.yml` has one job (`test-action`) whose steps are the individual cases (varied inputs, no inputs, `enable`, `disable`, `verbosity`, `format`, `suppress`, `suppress_from`, `only_changed`, and the `annotations` group at the end). To exercise one case in isolation, temporarily comment out the others.

Every case but the last group passes, which cannot exercise the reporting: a clean run
produces no diagnostics to annotate. `testdata/annotations-fixture.bwfixture` violates
`keep-sorted` on purpose for those, and carries an extension blockwatch does not know, so
every other case walks past it — only the annotation cases make it visible, by passing
`extensions: "bwfixture=md"`. They are `continue-on-error: true` because the step is
*meant* to fail; a `git`-tracked file that failed every run would make the workflow
useless as a smoke test. Don't "fix" that fixture.

Only **one** of those cases emits an error annotation, and that is deliberate. They all point at
the same fixture, so each case that annotates puts an identical card on its single violating line —
and the workflow used to run twice per pull request (`push` *and* `pull_request`, for a branch in
this repository), doubling that again: eight cards on one line. The trigger is now
`push: branches: [main]`, and the other cases exercise their input while emitting nothing.
`annotations_limit: "0"` counts every violation as omitted, which reaches the cap's own branch and
emits only the "further violation(s)" notice; the SARIF case runs its filter into the job summary
with `annotations: "false"`. Adding a case that annotates adds a card to every pull request that
touches the fixture.

`scripts/run.py` has black-box tests in [`scripts/run_test.py`](scripts/run_test.py) and is fully
annotated for Pyrefly. Both are fast, and the [pre-commit hooks](#the-pre-commit-hooks) run them:

```shell
python3 scripts/run_test.py     # or: python3 -m pytest scripts/run_test.py
pyrefly check                   # preset and disabled codes come from pyproject.toml
```

The tests run the script as a subprocess against a **real git repository and the real blockwatch
binary** — nothing is stubbed, and the module refuses to run if `blockwatch` is not on `PATH`. The
binary is there to supply realistic input, not to be the subject: **every assertion is about what
`run.py` does with what it was given.** blockwatch's own contract — the column base, the address
format, the wording of a message — is read back out of the diagnostics the script replayed and
compared against the annotation, never hardcoded, so a blockwatch release that changes any of it is
not a failure here. That is deliberate: it is not this repository's job to test the linter.

The question to ask of a new case is *would this fail if `run.py` broke, or only if blockwatch
changed?* Two earlier cases failed it and were dropped: one asserting that `globs` narrow a run
(blockwatch's glob semantics) and one suppressing through the `suppress` input (generic list
plumbing, and the commit-message case already covers the notice mapping). Dropping the first left
the "nothing may follow the positional globs" invariant with no automated guard — it lives in
review and in the flag table above.

They import nothing from `run.py` — rearranging its internals must not touch them — and each test
builds its own repository with `HOME` redirected into it, so no global git config reaches in. The
file is ordered by importance, most critical first; unittest still runs them alphabetically.
Coverage is deliberately partial: the nine cases are the ones whose failure would be worst, not
every branch. The job summary is not among them.

Annotations use the modern spellings (`list[str]`, `list[str] | None`), which `from __future__
import annotations` keeps lazy, so they cost nothing at runtime on an older interpreter. The one
alias that *is* evaluated, `JsonObject`, is spelled with `typing.Dict` for that reason.

### The pre-commit hooks

[`.pre-commit-config.yaml`](.pre-commit-config.yaml) runs formatting, types and tests through
[pre-commit](https://pre-commit.com/). Install once per clone — pre-commit does not arm itself:

```shell
pipx install pre-commit     # or: brew install pre-commit
pre-commit install
pre-commit run --all-files  # on demand, outside a commit
```

`git commit --no-verify` bypasses them.

The first two hooks come from upstream (`psf/black-pre-commit-mirror` and
`facebook/pyrefly-pre-commit`), so pre-commit installs and pins those tools itself and a
contributor needs nothing but pre-commit; both already scope themselves to Python files, so editing
the README or `action.yml` runs neither. Only the tests are a `local` hook, and the one thing to
know about it is `language: system`: they drive the real blockwatch binary and real git, which the
isolated virtualenv pre-commit would otherwise build does not have.

Three details worth keeping:

- **Black rewrites the file and fails, rather than only reporting.** That is the upstream hook's
  behaviour: re-stage and commit again.
- **The width is 100, set in `pyproject.toml`**, which exists for that and the Pyrefly settings
  alone — this repository ships no Python package. A manual `black scripts/` therefore agrees with
  the hook instead of reformatting to black's default 88, which rewrites three times as many lines
  and breaks apart comment-aligned argument lists. `ruff format` was the other candidate and may
  well be the better one; it went unmeasured because its wheel produced no runnable binary here.
- **Pyrefly runs the `strict` preset**, the closest thing to the `mypy --strict` it replaced, with
  `missing-override-decorator` turned off: satisfying it needs `typing.override`, which is Python
  3.12+, and this repository takes no dependencies, so `typing_extensions` is not an option either.
  The decorator would only ever appear on unittest's `setUp`.

To check CLI behaviour without the Action wrapper (`blockwatch` is installed locally):

```shell
git diff --patch | blockwatch -e keep-sorted '**/*.md'
```

## How `action.yml` works

Seven sequential steps, each with a non-obvious constraint:

1. **Checkout** with `fetch-depth: 0` — required, otherwise the diff revisions below don't resolve.
2. **Resolve cargo bin directory** — derives `${CARGO_INSTALL_ROOT:-${CARGO_HOME:-$HOME/.cargo}}/bin` (cargo's own precedence) and exports it as both a step output and a `GITHUB_PATH` entry. Never hardcode `~/.cargo`: container images like the official `rust` one relocate `CARGO_HOME`, which would silently desync the cache path from the install location. There's deliberately no fallback for an unwritable resolved directory — a fallback here would only affect this step's own output, not where `cargo-binstall` actually installs, so it would silently disagree with reality instead of failing clearly. The `GITHUB_PATH` append is deliberately *ungated* — the install steps are skipped on a cache hit, so this is the only thing making a restored binary reachable on a runner without a preinstalled Rust toolchain.
3. **Set blockwatch version** — a single `echo "version=X.Y.Z" >> "$GITHUB_OUTPUT"` that pins the version. This is the *only* place the version string lives; the cache key and the install step both read `steps.blockwatch-version.outputs.version` rather than repeating the literal, so bumping blockwatch to a new release is a one-line change.
4. **Cache** `blockwatch{,.exe}` under the step-2 directory, keyed on `runner.os` + `runner.arch` + the step-3 version. `runner.arch` matters because the cache is shared across a repo's runners, so a mixed-architecture fleet would otherwise restore a binary for the wrong arch.
5. **Install cargo-binstall** via `cargo-bins/cargo-binstall`, skipped on cache hit. The version appears **twice**, deliberately: the `uses:` ref pins the action and its install script, while the `version:` input pins the executable that script downloads. Omitting the input is not a smaller pin but a different one — the script then fetches from `releases/latest`, so a new cargo-binstall release reaches every `@v1` consumer with no change here, and only on a cache miss, which makes any resulting breakage intermittent. Dependabot bumps the `uses:` ref but not the input, so re-sync the two by hand when the grouped PR lands.
6. **Install blockwatch** by calling `cargo-binstall`, *not* `cargo binstall` — the previous step ships binstall alone and no toolchain, so `cargo` may not exist. Passes `--disable-strategies compile`: without that, a target with no prebuilt binary would silently fall through to `cargo install`, which needs `cargo` and would just fail with a confusing "command not found" instead of a clear "no binary available".
7. **Run** — a five-line wrapper that picks an interpreter and calls `scripts/run.py`. The script builds blockwatch's arguments, assembles the suppression sources, produces the diff for this event, pipes it into blockwatch, captures its stderr, replays it to the log, and reports the violations as annotations and a job-summary table. blockwatch's exit code, taken before any reporting runs, is what fails the job. See [Run mode](#run-mode) and [Reporting](#reporting).

The version is pinned deliberately, not resolved dynamically: bump the string in step 3 when a new blockwatch release should be picked up. There's no Dependabot or other automation for this — it doesn't track arbitrary strings inside `action.yml`.

Self-hosted runners are supported but assume no Rust toolchain; the requirements they *do* have to satisfy (runner ≥ 2.327.1, glibc ≥ 2.28, git ≥ 2.18, `bash`, `curl` + `tar`/`unzip`, egress) are listed in the README. Keep those two in sync when bumping the pinned action versions.

### Why the logic is in a script

A `run:` block that embeds `${{ }}` expressions is parsed as one GitHub *template*, and a template
cannot exceed **21000 characters**. The step reached 22203 and every run of the action died with
`The template is not valid ... Exceeded max expression length 21000` — a parse error, before the
first line executes, which no amount of testing the logic itself would have caught. Comments were
half the script, so the ceiling was being spent on documentation.

`scripts/run.py` is not a template, so the limit is gone rather than merely avoided: the wrapper in
`action.yml` is 200 characters and contains no expressions at all. **Don't move logic back inline.**

Two consequences worth keeping:

- **Inputs reach the script as environment variables, never as expansions.** A `${{ inputs.globs }}`
  spliced into a shell script is code; `INPUT_GLOBS` is data. One of these values — the pull request
  description — is written by whoever opened the request, a stranger on a fork included, and it is
  now read from `GITHUB_EVENT_PATH` rather than passed through an expression at all.
- **`jq` and `curl` are no longer needed.** `json` and `urllib` come with Python, so the runner
  requirement is Python 3 (which every GitHub-hosted runner has) instead of two CLI tools. The
  README lists it.

The script reads `GITHUB_EVENT_PATH` for the event, so `github.event.before`, the PR number and the
PR body need no `env:` entry: adding an input means adding one `INPUT_*` line to `action.yml` and
reading it in `run.py`.

### Input handling

Every list input accepts comma-separated *or* newline-separated values. `split_list` normalizes newlines to commas, splits, trims, and drops blanks; the results are appended to an argument *list*, so a value containing spaces stays one argv entry. Flag mapping:

| input          | flag                                    |
| -------------- | --------------------------------------- |
| `extensions`   | `-E`                                    |
| `enable`       | `-e`                                    |
| `disable`      | `-d`                                    |
| `ignore`       | `--ignore`                              |
| `suppress`     | `--suppress`                            |
| `suppress_from`| `--suppress-from`                       |
| `verbosity`    | `--verbosity` *(scalar, see below)*     |
| `format`       | `--format` *(scalar, see below)*        |
| `only_changed` | `--only-changed` *(boolean, see below)* |
| `globs`        | *(positional)*                          |

`annotations`, `annotations_limit` and `summary` are the exception to that table: they are
the action's own, do not reach blockwatch at all, and are described under
[Reporting](#reporting).

`enable` and `disable` are mutually exclusive in blockwatch itself; the action does not validate this.

`suppress` (blockwatch 0.5.2+) carries violation addresses, `FILE[:BLOCK_NAME[:VALIDATOR[:HASH]]]`, and is a
plain list like the rest — the `:` separators survive `split_list` untouched, since it splits only on commas and
newlines. That splitting is the one limit worth knowing: an address whose file path contains a comma cannot be
passed. Nothing is validated here, because blockwatch draws the line in both directions itself — a malformed
address is rejected with the offending segment named (exit 2, failing the step), while a well-formed one that
matches no violation is silently ignored, so a suppression left behind after its violation is fixed does not
fail the run.

`suppress_from` (blockwatch 0.5.4+) is a list of *paths* to files carrying `Blockwatch-suppress: ADDRESS`
lines — the same addresses, read from a file rather than written into the workflow. Every other line in the
file is ignored. It accumulates with `suppress`; neither overrides the other. Nothing is validated here
either: an unreadable path fails the step with the path named (exit 1), which is the right outcome — an
action that skipped a missing suppression file would fail the run on the violations instead, pointing the
reader at the code rather than at the typo.

The step also builds a suppression file of its own, unconditionally, so a suppression can travel in the
commit that needs it. It collects the commit messages under test and, on a pull request, the description,
and appends one more `--suppress-from` for the result; no extraction is needed, since blockwatch already
ignores every line that isn't an address. Three details are load-bearing:

- **The revision range mirrors the diff.** `BEFORE_SHA`/`CURRENT_SHA`/`ZERO_SHA` are resolved once, above
  both this block and the diff dispatch, precisely so the two cannot drift: what is allowed to suppress a
  violation is the same work that is being checked for one. `git log` uses two dots where the diff uses
  three — it wants the commits a PR adds, not a patch. A new branch falls back to the head commit alone,
  matching the diff's own coverage, and an event with no diff reads `HEAD` so that a `workflow_dispatch`
  re-run behaves like the push that first checked that commit.
- **The PR description is read from the API at run time, with the event payload as the fallback.** The
  payload is a snapshot taken when the run was created, and re-running a workflow replays it — so a
  suppression added to the description afterwards would be invisible until the next push, which defeats
  the obvious workflow of fixing the description and pressing Re-run. `GH_TOKEN: ${{ github.token }}` is
  in the step's `env:` for this one read; a missing or restricted token, or any non-200, falls back to
  `PR_BODY` rather than failing, and the log says which source was used. The cost is in the README under
  Known limitations: an edit now takes effect without a new commit, so a suppression can arrive after a
  review without disturbing the approval.
- **Neither source is ever expanded inline.** The description is written by whoever opened the PR, a
  stranger on a fork included; a `${{ github.event.pull_request.body }}` anywhere inside `run:` would
  paste their text into the script for bash to execute. It reaches the script through the environment or
  through a file, never through an expression — the one attacker-controlled input in the step, where
  every other one comes from the workflow file or from git.
- **The addresses found are echoed to the log.** A suppression from a commit message changes the exit code
  while appearing nowhere in the workflow file. The `grep` that labels them mirrors blockwatch's own match
  (prefix at line start, leading whitespace trimmed, case-insensitive) but decides nothing; drift there
  mislabels a line, it does not suppress one.

Two consequences worth keeping in mind, both in the README under Known limitations: anyone who can open a
pull request can suppress a violation inside it, and a line that *starts* with `Blockwatch-suppress:` but
carries a malformed address (too many `:` segments, or a trailing one) fails the step rather than being
skipped. A mid-sentence mention or a `>`-quoted line is not matched, so ordinary prose is safe.

The block sits before the `globs` call on purpose: globs are positional and nothing may be appended to
the argument list after them.

`only_changed` is a boolean, so it does not go through `split_list` either. It arrives as a string — composite actions have no typed inputs — and `parse_bool` accepts `true`/`True`/`TRUE` and `false`/`False`/`FALSE`/empty. Anything else raises and exits 1 rather than being read as false: nothing downstream would report `only_changed: yes` silently turning into a whole-repository scan. `annotations` and `summary` use the same helper for the same reason — a mistyped value there would silently turn reporting off.

`verbosity` is the one input that does **not** go through `split_list`: it's a single clap enum (`none`/`summary`/`full`), not a list, and splitting a comma-separated value would emit `--verbosity` twice, which clap resolves by silently keeping the last one. `scalar` strips its whitespace instead, and it is appended only when non-empty. The level isn't validated here — blockwatch rejects an unknown one with a clear error. Its report goes to stdout while violations go to stderr, so the two stay separable.

`format` (blockwatch 0.5.3+) is the other scalar enum (`json`/`sarif`) and is kept out of `split_list` for
exactly the reason `verbosity` is — a comma in it would emit `--format` twice and clap would silently keep the
last. `sarif` swaps the JSON diagnostics for a SARIF 2.1.0 log, and unlike them writes one even when the run
found nothing. Both formats go to stderr, and the run step leaves stderr attached to the job log rather than
redirecting it to a file, so `format: sarif` alone gives a caller nothing to hand to
`github/codeql-action/upload-sarif`; the README says so under Known limitations. Adding a file output would
mean redirecting stderr in step 7, which would also hide the JSON diagnostics from the log for everyone else,
so it hasn't been done.

`globs` are positional, so they are appended last and nothing may follow them in the argument list. Since blockwatch 0.4.0 they only ever *narrow* a run — they intersect with whatever the mode selected instead of adding files back — so the array no longer needs to record whether any glob survived.

### Reporting

blockwatch's diagnostics are a report, not just a log line, so the step reads them back and
turns each violation into a GitHub annotation (on the line itself in a pull request) and a
job-summary row. `annotations` (default `true`), `annotations_limit` (default 50) and
`summary` (default `true`) control it; all three are the action's own and change nothing
about what blockwatch checks.

The mechanics, in the order they bite:

- **stderr is captured to a file and replayed**, rather than streamed. Reading the
  diagnostics requires capturing them, and the JSON a reader used to copy out of the log
  has to stay there, so the file is `cat`-ed back after the run. The only visible cost is
  ordering: diagnostics now print after the stdout report instead of interleaved with it.
- **The exit code is taken before any reporting and re-raised at the end.** A composite
  action's bash runs with `-eo pipefail`, so the run is wrapped in `set +e`/`set -e` and
  `$STATUS` carries blockwatch's own code past the reporting to the final `exit`
  — pipefail reports the rightmost failure, so a git failure surfaces only if blockwatch
  somehow exited 0; an empty diff it raises as an error of its own. Reporting failures are caught with `if ! report` and
  downgraded to a warning: the violations are in the log either way, and an annotator bug
  must not turn a passing run into a failing one, or the reverse.
- **Which parser runs is decided by the `format` input, never by sniffing the document.**
  The JSON diagnostics are keyed by file path, so a repository containing a file named
  `runs` would otherwise read as a SARIF log. Both filters emit the same 10 fields, so
  everything downstream is format-agnostic — `format: sarif` and annotations are
  orthogonal.
- **The diagnostics are parsed with `json`, into `Violation` objects.** The shell version
  had to flatten them through a delimited text stream, which needed U+001F separators and a
  newline placeholder to survive `read`; none of that exists any more. A multi-line message
  (`check-ai` produces them) is now simply a string.
- **Workflow-command text is percent-encoded** the way GitHub's own toolkit does it: `%`,
  CR and LF in the message; additionally `:` and `,` in property values, since a comma
  starts the next property and a file path may legitimately contain one.
- **A column range is emitted only when the violation sits on one line.** GitHub ignores
  `col`/`endColumn` on a multi-line annotation. blockwatch's ranges are 1-based with an
  *inclusive* end column, which is what GitHub wants; note this differs from SARIF's own
  spec, where `endColumn` is exclusive.
- **Suppressed violations become notices, not errors**, because they do not fail the run,
  and the annotation carries the violation's suppression address when the block has a name
  (unnamed blocks have no address). That address is otherwise only in the JSON.
- **Python 3 is the only runtime requirement** the reporting adds, and the wrapper prefers
  `python3`, falling back to `python` for Git Bash on the Windows runners. `jq` and `curl`
  are not used by this step at all any more. The README lists it under requirements.

Two caps, both in the README under Known limitations: GitHub renders only a limited number
of annotations per step and drops the rest silently, so the step emits at most
`annotations_limit` and says how many it left out; and the job summary is rejected outright
above 1 MiB, so it lists at most 1000 rows and counts the remainder.

### Run mode

blockwatch 0.4.0 stopped inferring the run mode from stdin. `--diff` is the only thing that makes it read one, and `--only-changed` narrows the run to the blocks that diff touched. `DIFF_FLAGS` holds that pair: always `--diff`, plus `--only-changed` when the `only_changed` input says so. Events with no diff run a bare `blockwatch` and use neither.

The default (`only_changed: false`) validates **every block in the repository** and uses the diff only to mark which blocks changed — the comparison `affects` is built on — so a pull request reports what it inherited as well as what it introduced. Two things follow from the repository walk, rather than the diff, defining the scope:

- **A changed file the walk never reaches is dropped, not checked.** Since blockwatch 0.5.0 the walk descends into dot-directories, so a block under `.github/` is checked like any other ([blockwatch#100](https://github.com/mennanov/blockwatch/issues/100)); what it still refuses to enter is the state directory of a version control system (`.git`, `.hg`, `.jj`, `.svn`), and `.gitignore` plus `ignore` still remove files. Before 0.5.0 every dot-directory was skipped, so a block under `.github/` went unchecked even when the diff touched it.
- **`diff_pathspec` no longer keeps a file out of the run**, since the diff only decides which blocks count as changed. `ignore` is what excludes files from the scan.

`only_changed: true` makes the diff the scope again, exactly as before 0.4.0, and both of those revert with it.

**An empty diff fails the step** in either mode (`diff in stdin is empty.`), where it used to pass silently. `diff_pathspec` excluding everything a push touched, and a force-push resetting to an older commit, are the two realistic ways to hit it. A guard was considered and rejected — a linter that silently passes on an input it cannot read is the failure mode 0.4.0 exists to remove.

### Diff selection per event

- `pull_request`: `origin/${{ github.base_ref }}...${{ github.sha }}`
- `push`: `${{ github.event.before }}...${{ github.sha }}`, falling back to `git diff-tree --patch --root -m --first-parent --no-commit-id <sha>` when `before` is all zeros (first push to a new branch). Every flag there is load-bearing: this was `git diff --patch --root <sha>`, but `--root` is a *diff-tree* option that `git diff` accepts silently rather than rejecting, so the command compared the working tree against `<sha>` — always a 0-byte patch on a clean checkout, making every `affects` check pass vacuously. `-m --first-parent` is what makes merge commits emit anything; without it `diff-tree` prints nothing for a merge and the empty patch returns whenever a new branch has a merge at its head. `--no-commit-id` suppresses the bare SHA line `git diff` never emits (blockwatch has tolerated it so far, but the pipeline shouldn't lean on that — it's outside the diff format blockwatch documents as its input contract). An empty-tree SHA would also work with `diff_pathspec` — contrary to what this file used to claim — but it renders `affects` useless, since a whole-tree patch marks both sides of every pair as modified. Only the head commit is covered; a new branch has no base, so earlier commits in the push are checked on the PR instead.
- anything else: no diff exists, so the step runs a bare `blockwatch` — the same whole-tree scan as the default mode, minus the marks saying which blocks changed, and with `only_changed` inapplicable (the note says so unconditionally rather than reading the input). No `< /dev/null` guard is needed: without `--diff` blockwatch never touches stdin, so there is no descriptor to block on. This branch used to run only when `globs` was non-empty and warn otherwise, because a diff-less run with no globs checked nothing; since 0.4.0 a bare run scans the tree, so there is always real work to do. The comparison `affects` is built on still can't run here — with nothing marked as changed it has nothing to compare — though since 0.4.3 `affects` does check without a diff that every block it references still exists. If you add real support for a new event, add its branch above.

`diff_command` returns the command as a list, or `None` for an event that has no diff; the
single call site in `run_blockwatch` is what makes one stderr capture possible (see
[Reporting](#reporting)).

`diff_pathspec` is split on whitespace with `str.split()`, so callers can still pass several
pathspecs like `:(exclude).github/`. This replaced an unquoted `-- $DIFF_PATHSPEC` at each of the
six former call sites: word splitting was the point there, but pathname expansion came with it, so
a pathspec containing a glob was matched against the working directory before git ever saw it.
Splitting a string keeps the one and drops the other, and there is no shell in this path at all
now.

## Releasing

Consumers reference `mennanov/blockwatch-action@v1`. Cut a new patch tag (`v1.0.N`) **and** force-move the `v1` tag to the same commit — `v1` currently tracks the latest release, and a release that only creates the patch tag reaches nobody.

Dependabot bumps the pinned third-party actions (`actions/checkout`, `actions/cache`, `cargo-bins/cargo-binstall`) monthly as one grouped PR.
