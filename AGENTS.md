# AGENTS.md

Guidance for coding agents working on `blockwatch-action`.

## Overview & Layout

A single **composite GitHub Action** ([action.yml](action.yml)) with no build step that installs and runs [`blockwatch`](https://github.com/mennanov/blockwatch) against a repository's git diff.

- [`action.yml`](action.yml) — inputs, outputs, and the 6 install/cache steps plus a minimal step-7 wrapper.
- [`scripts/run.py`](scripts/run.py) — thin step-7 entrypoint backed by focused modules in [`scripts/action/`](scripts/action/):
  - `config.py` — env/input parsing (`ActionConfig`, `DEFAULT_ANNOTATIONS_LIMIT`), `GITHUB_EVENT_PATH` loading.
  - `runner.py` — per-event diff command selection, CLI arg assembly, and `blockwatch` execution.
  - `suppressions.py` — commit-log and PR-description `blockwatch-suppress:` collection and suppression file creation.
  - `diagnostics.py` — JSON/SARIF parsing, `Violation` classification, inclusive GitHub `endColumn` conversion.
  - `github_api.py` — live PR description fetch and sticky PR suppression comment sync.
  - `reporting.py` — GitHub annotations, job summary tables, sticky comment rendering, and `GITHUB_OUTPUT` writing.
- [`scripts/tests/`](scripts/tests/) — black-box `unittest` suite (`test_core.py`, `test_suppressions.py`, `test_pr_api.py`, `harness.py`, parallel `load_tests` in `__init__.py`).
- [`blockwatch.toml`](blockwatch.toml) — `blockwatch` sync rules tying code/config/docs together.
- Documentation — [`README.md`](README.md) (quick start + inputs/outputs), [`docs/`](docs/), and [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Commands & Tooling

```shell
# Unit tests (runs real git + real blockwatch pinned to action.yml's version)
python3 -m unittest discover -s scripts -v
UNITTEST_WORKERS=1 python3 -m unittest discover -s scripts -v  # sequential

# Formatting & type checking (settings in pyproject.toml: width 100, strict preset)
black --check scripts
pyrefly check

# Blockwatch checks
blockwatch
git diff --patch | blockwatch --diff
git diff --patch | blockwatch --diff --only-changed

# All pre-commit hooks (black, pyrefly-check, zizmor, local run-tests)
pre-commit run --all-files

# Local workflow simulation with act
act workflow_dispatch -W .github/workflows/local-test.yml
printf '{"pull_request": {"base": {"ref": "BASE_BRANCH"}, "head": {"ref": "main"}}}' > /tmp/pr.json
act pull_request -W .github/workflows/local-test.yml -e /tmp/pr.json
printf '{"before": "%s", "after": "%s"}' "$(git rev-parse HEAD~1)" "$(git rev-parse HEAD)" > /tmp/push.json
act push -W .github/workflows/local-test.yml -e /tmp/push.json
```

### Testing & CI conventions

- **Tests treat `blockwatch` as a black box**: `scripts/tests/` spawns `scripts/run.py` against a throwaway git repo and the exact `blockwatch` version pinned in `action.yml` (step 3). Assert what `blockwatch-action` does with blockwatch's output—never hardcode blockwatch's own diagnostic wording or column rules, and never import internals from `run.py` in tests.
- **`testdata/*.bwfixture`**: Both files intentionally fail `keep-sorted` and use an unknown extension so normal runs ignore them while annotation and `only_blocks`/`skip_blocks` steps in `local-test.yml` pass `extensions: "bwfixture=md"`. Do not "fix" those fixtures, and give any new annotating test case its own fixture so annotations don't collide on one line.
- **`pre-commit`**: `black` reformats in place and fails the commit (re-stage and commit again). `run-tests` runs with `language: system` whenever `scripts/**/*.py` or `action.yml` changes.
- **Workflow lockfile**: `.github/workflows/*.yml` use version tags and `uses: $/` locked in `.github/workflows/actions.lock` (checked in CI via `gh actions-lock --verify`; `act` does not support `uses: $/`).

## Critical Invariants

### `action.yml` & supply chain

- **Never inline logic into `action.yml` `run:`**: Any `run:` block containing `${{ }}` hits GitHub's 21,000-character template limit and injects untrusted values (such as PR descriptions) into shell code. Pass inputs via `INPUT_*` env vars and read event payloads from `GITHUB_EVENT_PATH`.
- **SHA-pinned nested actions**: Every `uses:` in `action.yml` (`pinned-actions` block) must use a full commit SHA with `# vX.Y.Z`, never a moving `@v...` tag. Resolve SHAs with `gh api repos/OWNER/REPO/commits/TAG --jq .sha` and verify the commit lives on the upstream default branch.
- **Two-place `cargo-binstall` pin**: `binstall-ref` and `binstall-input` in `action.yml` must match (enforced by `same-as`). Dependabot bumps only the ref/comment, so update `version:` to match on Dependabot PRs.
- **Keep install/cache guards intact**: Step 1 requires `fetch-depth: 0` for diff base resolution; step 2 must append `$CARGO_BIN` to `GITHUB_PATH` unconditionally (cache hits skip install steps) without falling back on unwritable paths; step 4 keys cache on `runner.os` + `runner.arch`; step 6 must keep `--disable-strategies compile,quick-install` and `--only-signed` (requires `blockwatch >= 0.18.1`) plus `GITHUB_TOKEN` to avoid anonymous API rate limits.
- **Runner floor sync**: When bumping pinned action versions or `blockwatch`, check [docs/runners-and-security.md](docs/runners-and-security.md) for runner/glibc/tool minimum updates.

### Runtime (`scripts/run.py` & `scripts/action/`)

- **Python 3.7+ with zero dependencies**: Keep `from __future__ import annotations`, no third-party imports, and use `typing.Dict` (e.g., `JsonObject`) for type aliases evaluated at module load.
- **CLI arg ordering & validation**: `globs` are positional and must be appended last. Scalar inputs (`verbosity`, `format`, `config`) must not go through `split_list`; booleans use `parse_bool` (rejecting unknown tokens instead of silently defaulting to `false`).
- **Diff & suppression scope parity**: `push_base` feeds both `diff_command` and `git_log_command` so diff range and commit suppression range stay identical. `head_commit_diff` uses `git diff-tree --patch <sha>^1 <sha>` (or `--root --no-commit-id`) for `git 2.18+` compatibility. Empty diffs are valid (`only_changed` checks nothing); git errors abort before running `blockwatch`. `diff_pathspec` uses `str.split()` without shell globbing.
- **Untrusted inputs & fallback behavior**: Never expand commit messages or PR bodies into shell expressions. Live PR body fetches fall back to `GITHUB_EVENT_PATH` on missing/restricted tokens or HTTP errors, and sticky PR comment sync skips cleanly on `403` / read-only fork tokens.
- **Exit codes & outputs**: `blockwatch`'s exit code (`1` = unsuppressed error violation, `2` = run/syntax failure) is preserved past reporting; reporting errors downgrade to `::warning`. Step outputs (`commit_suppressions`, `description_suppressions`, `suppressions_truncated`, capped at 512 KiB) are always written, even when reporting is off or the step fails.

## `blockwatch.toml` & Docs Sync

- `action.yml`, `scripts/run.py`, and `scripts/action/*.py` declare `affects = 'README.md:quick-start'`.
- `README.md:quick-start` syncs via `same-as` with the workflow snippets in [docs/configuration.md](docs/configuration.md) and [docs/suppressions.md](docs/suppressions.md); all forbid moving `@vN` tags.
- `README.md:inputs` / `outputs` sync with `action.yml#/inputs` and `#/outputs`; `README.md:docs-index` syncs with `docs/*.md` via `same-as-paths`.
- Duplicated defaults/caps (`DEFAULT_ANNOTATIONS_LIMIT` across `config.py`, `action.yml`, `README.md`, `docs/reporting.md`; `SUMMARY_ROW_LIMIT` across `reporting.py` and `docs/reporting.md`) are enforced via `same-as` in [blockwatch.toml](blockwatch.toml).

## Releasing

Consumers typically pin `@v1` or an exact `@vX.Y.Z` tag. Merging to `main` runs [`.github/workflows/release.yml`](.github/workflows/release.yml), which automatically cuts `vX.Y.(Z+1)` and force-moves `v1` to that commit when:

1. `checks.yml` and `local-test.yml` both passed on the commit,
2. the commit is ahead of the latest `v*.*.*` tag, and
3. `action.yml`, `scripts/run.py`, or `scripts/action/` differs from the latest version tag.

Always determine the next version from git tags—never from memory:

```shell
git fetch --tags
git tag --list 'v*.*.*' --sort=-v:refname | head -1
```

(Version sorting is required; historical `v1.0.11`–`v1.0.17` tags exist alongside `v1.1.5`–`v1.1.11`—never continue `v1.0.x`.) When a PR changes runtime files (`action.yml`, `scripts/run.py`, or `scripts/action/`), bump the `uses: mennanov/blockwatch-action@v...` tag in `README.md:quick-start` and its synced `docs/` blocks to that next patch version.
