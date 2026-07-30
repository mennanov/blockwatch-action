# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single **composite GitHub Action** that installs and runs the [`blockwatch`](https://github.com/mennanov/blockwatch) Rust CLI against a repository's git diff. There is no application source and no build step — `action.yml` *is* the product. Everything else (README, test workflow, dependabot) supports it.

## Commands

```shell
# Run the whole test workflow locally (requires `act` + Docker)
act -W .github/workflows/local-test.yml

# Simulate a specific event — the action takes a different diff path per event
act push -W .github/workflows/local-test.yml
act pull_request -W .github/workflows/local-test.yml
```

`act` cannot run a single step; `local-test.yml` has one job (`test-action`) whose steps are the individual cases (varied inputs, no inputs, `enable`, `disable`). To exercise one case in isolation, temporarily comment out the others.

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
5. **Install cargo-binstall** via `cargo-bins/cargo-binstall`, skipped on cache hit.
6. **Install blockwatch** by calling `cargo-binstall`, *not* `cargo binstall` — the previous step ships binstall alone and no toolchain, so `cargo` may not exist. Passes `--disable-strategies compile`: without that, a target with no prebuilt binary would silently fall through to `cargo install`, which needs `cargo` and would just fail with a confusing "command not found" instead of a clear "no binary available".
7. **Run** — pipes `git diff --patch` into `blockwatch`. blockwatch's exit code is what fails the job.

The version is pinned deliberately, not resolved dynamically: bump the string in step 3 when a new blockwatch release should be picked up. There's no Dependabot or other automation for this — it doesn't track arbitrary strings inside `action.yml`.

Self-hosted runners are supported but assume no Rust toolchain; the requirements they *do* have to satisfy (runner ≥ 2.327.1, glibc ≥ 2.28, git ≥ 2.18, `bash`, `curl` + `tar`/`unzip`, egress) are listed in the README. Keep those two in sync when bumping the pinned action versions.

### Input handling

Every input accepts comma-separated *or* newline-separated values. The `add_args` bash helper normalizes newlines to commas, splits on comma, trims whitespace, and appends each item to the `BLOCKWATCH_ARGS` array (an array, not a string, so values containing spaces survive). Flag mapping:

| input | flag |
|---|---|
| `extensions` | `-E` |
| `enable` | `-e` |
| `disable` | `-d` |
| `ignore` | `--ignore` |
| `globs` | *(positional)* |

`enable` and `disable` are mutually exclusive in blockwatch itself; the action does not validate this.

### Diff selection per event

- `pull_request`: `origin/${{ github.base_ref }}...${{ github.sha }}`
- `push`: `${{ github.event.before }}...${{ github.sha }}`, falling back to `--patch --root <sha>` when `before` is all zeros (first push to a new branch) — `--root` rather than an empty-tree SHA so `diff_pathspec` still applies.
- anything else: prints a warning and skips, meaning the job **passes silently**. If you add support for a new event, add its branch here or it will be a no-op.

`diff_pathspec` is intentionally unquoted (`-- $DIFF_PATHSPEC`) so callers can pass multiple space-separated pathspecs like `:(exclude).github/`; the `shellcheck disable=SC2086` comments mark this as deliberate.

## Releasing

Consumers reference `mennanov/blockwatch-action@v1`. Cut a new patch tag (`v1.0.N`) **and** force-move the `v1` tag to the same commit — `v1` currently tracks the latest release, and a release that only creates the patch tag reaches nobody.

Dependabot bumps the pinned third-party actions (`actions/checkout`, `actions/cache`, `cargo-bins/cargo-binstall`) monthly as one grouped PR.
