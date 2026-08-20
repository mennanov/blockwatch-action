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
5. **Install cargo-binstall** via `cargo-bins/cargo-binstall`, skipped on cache hit. The version appears **twice**, deliberately: the `uses:` ref pins the action and its install script, while the `version:` input pins the executable that script downloads. Omitting the input is not a smaller pin but a different one — the script then fetches from `releases/latest`, so a new cargo-binstall release reaches every `@v1` consumer with no change here, and only on a cache miss, which makes any resulting breakage intermittent. Dependabot bumps the `uses:` ref but not the input, so re-sync the two by hand when the grouped PR lands.
6. **Install blockwatch** by calling `cargo-binstall`, *not* `cargo binstall` — the previous step ships binstall alone and no toolchain, so `cargo` may not exist. Passes `--disable-strategies compile`: without that, a target with no prebuilt binary would silently fall through to `cargo install`, which needs `cargo` and would just fail with a confusing "command not found" instead of a clear "no binary available".
7. **Run** — pipes `git diff --patch` into `blockwatch "${DIFF_FLAGS[@]}"`. blockwatch's exit code is what fails the job. See [Run mode](#run-mode) for what that array holds.

The version is pinned deliberately, not resolved dynamically: bump the string in step 3 when a new blockwatch release should be picked up. There's no Dependabot or other automation for this — it doesn't track arbitrary strings inside `action.yml`.

Self-hosted runners are supported but assume no Rust toolchain; the requirements they *do* have to satisfy (runner ≥ 2.327.1, glibc ≥ 2.28, git ≥ 2.18, `bash`, `curl` + `tar`/`unzip`, egress) are listed in the README. Keep those two in sync when bumping the pinned action versions.

### Input handling

Every list input accepts comma-separated *or* newline-separated values. The `add_args` bash helper normalizes newlines to commas, splits on comma, trims whitespace, and appends each item to the `BLOCKWATCH_ARGS` array (an array, not a string, so values containing spaces survive). Flag mapping:

| input          | flag                                    |
| -------------- | --------------------------------------- |
| `extensions`   | `-E`                                    |
| `enable`       | `-e`                                    |
| `disable`      | `-d`                                    |
| `ignore`       | `--ignore`                              |
| `verbosity`    | `--verbosity` *(scalar, see below)*     |
| `only_changed` | `--only-changed` *(boolean, see below)* |
| `globs`        | *(positional)*                          |

`enable` and `disable` are mutually exclusive in blockwatch itself; the action does not validate this.

`only_changed` is a boolean, so it does not go through `add_args` either. It arrives as a string — composite actions have no typed inputs — and a `case` maps `true`/`True`/`TRUE` to the `--only-changed` flag and `false`/`False`/`FALSE`/empty to nothing. Anything else exits 1 rather than being read as false: nothing downstream would report `only_changed: yes` silently turning into a whole-repository scan.

`verbosity` is the one input that does **not** go through `add_args`: it's a single clap enum (`none`/`summary`/`full`), not a list, and feeding a comma-separated value to `add_args` would emit `--verbosity` twice, which clap resolves by silently keeping the last one. It's trimmed inline instead and appended only when non-empty. The level isn't validated here — blockwatch rejects an unknown one with a clear error. Its report goes to stdout while violations go to stderr, so the two stay separable.

`globs` are positional, so they are appended last and nothing may follow them in `BLOCKWATCH_ARGS`. Since blockwatch 0.4.0 they only ever *narrow* a run — they intersect with whatever the mode selected instead of adding files back — so the array no longer needs to record whether any glob survived.

### Run mode

blockwatch 0.4.0 stopped inferring the run mode from stdin. `--diff` is the only thing that makes it read one, and `--only-changed` narrows the run to the blocks that diff touched. `DIFF_FLAGS` holds that pair: always `--diff`, plus `--only-changed` when the `only_changed` input says so. Events with no diff run a bare `blockwatch` and use neither.

The default (`only_changed: false`) validates **every block in the repository** and uses the diff only to mark which blocks changed — the comparison `affects` is built on — so a pull request reports what it inherited as well as what it introduced. Two things follow from the repository walk, rather than the diff, defining the scope:

- **A changed file the walk never reaches is dropped, not checked.** The walk skips dot-directories ([blockwatch#100](https://github.com/mennanov/blockwatch/issues/100)), so a block under `.github/` goes unchecked even when the diff touches it. Verified against 0.4.0; the fix belongs in blockwatch, not here.
- **`diff_pathspec` no longer keeps a file out of the run**, since the diff only decides which blocks count as changed. `ignore` is what excludes files from the scan.

`only_changed: true` makes the diff the scope again, exactly as before 0.4.0, and both of those revert with it.

**An empty diff fails the step** in either mode (`diff in stdin is empty.`), where it used to pass silently. `diff_pathspec` excluding everything a push touched, and a force-push resetting to an older commit, are the two realistic ways to hit it. A guard was considered and rejected — a linter that silently passes on an input it cannot read is the failure mode 0.4.0 exists to remove.

### Diff selection per event

- `pull_request`: `origin/${{ github.base_ref }}...${{ github.sha }}`
- `push`: `${{ github.event.before }}...${{ github.sha }}`, falling back to `git diff-tree --patch --root -m --first-parent --no-commit-id <sha>` when `before` is all zeros (first push to a new branch). Every flag there is load-bearing: this was `git diff --patch --root <sha>`, but `--root` is a *diff-tree* option that `git diff` accepts silently rather than rejecting, so the command compared the working tree against `<sha>` — always a 0-byte patch on a clean checkout, making every `affects` check pass vacuously. `-m --first-parent` is what makes merge commits emit anything; without it `diff-tree` prints nothing for a merge and the empty patch returns whenever a new branch has a merge at its head. `--no-commit-id` suppresses the bare SHA line `git diff` never emits (blockwatch has tolerated it so far, but the pipeline shouldn't lean on that — it's outside the diff format blockwatch documents as its input contract). An empty-tree SHA would also work with `diff_pathspec` — contrary to what this file used to claim — but it renders `affects` useless, since a whole-tree patch marks both sides of every pair as modified. Only the head commit is covered; a new branch has no base, so earlier commits in the push are checked on the PR instead.
- anything else: no diff exists, so the step runs a bare `blockwatch` — the same whole-tree scan as the default mode, minus the marks saying which blocks changed, and with `only_changed` inapplicable (the note says so unconditionally rather than reading the input). No `< /dev/null` guard is needed: without `--diff` blockwatch never touches stdin, so there is no descriptor to block on. This branch used to run only when `globs` was non-empty and warn otherwise, because a diff-less run with no globs checked nothing; since 0.4.0 a bare run scans the tree, so there is always real work to do. Diff-driven validators like `affects` still can't run here. If you add real support for a new event, add its branch above.

`diff_pathspec` is intentionally unquoted (`-- $DIFF_PATHSPEC`) so callers can pass multiple space-separated pathspecs like `:(exclude).github/`; the `shellcheck disable=SC2086` comments mark this as deliberate.

## Releasing

Consumers reference `mennanov/blockwatch-action@v1`. Cut a new patch tag (`v1.0.N`) **and** force-move the `v1` tag to the same commit — `v1` currently tracks the latest release, and a release that only creates the patch tag reaches nobody.

Dependabot bumps the pinned third-party actions (`actions/checkout`, `actions/cache`, `cargo-bins/cargo-binstall`) monthly as one grouped PR.
