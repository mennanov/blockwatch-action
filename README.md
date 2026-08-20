# blockwatch-action

GitHub Action that runs the [blockwatch](https://github.com/mennanov/blockwatch) linter on your repository.

## Quick start

```yaml
on: [push, pull_request]

jobs:
  blockwatch:
    runs-on: ubuntu-latest
    steps:
      - uses: mennanov/blockwatch-action@v1
```

This checks every block in the repository. All inputs below are optional.

## Inputs

| Input           | Default     | What it does                                                                                  |
| --------------- | ----------- | --------------------------------------------------------------------------------------------- |
| `only_changed`  | `"false"`   | `"true"` checks only the blocks the diff touched. See [What gets checked](#what-gets-checked) |
| `globs`         |             | Check only files matching these patterns, e.g. `"**/*.md,**/*.yml"`                           |
| `ignore`        |             | Skip files matching these patterns, e.g. `"target/**,dist/**"`                                |
| `enable`        |             | Run only these validators, e.g. `"keep-sorted,keep-unique"`                                   |
| `disable`       |             | Run all validators except these, e.g. `"check-ai"`. Cannot be used with `enable`              |
| `extensions`    |             | Treat one file extension as another, e.g. `"cxx=cpp"`                                         |
| `verbosity`     | `"summary"` | `"none"`, `"summary"` (a line of counts) or `"full"` (JSON)                                   |
| `diff_pathspec` |             | Extra git pathspecs for the diff, e.g. `":(exclude).github/"`                                 |

Every list input takes commas or one value per line:

```yaml
ignore: |
  target/**
  dist/**
```

## What gets checked

On a `push` or `pull_request` the action builds a git diff and hands it to blockwatch. `only_changed` decides
what that diff is used for:

| `only_changed`      | Blocks checked                   | What the diff does         |
| ------------------- | -------------------------------- | -------------------------- |
| `"false"` (default) | Every block in the repository    | Marks which blocks changed |
| `"true"`            | Only the blocks the diff touched | Picks what to check        |

`globs` and `ignore` narrow both modes. `diff_pathspec` only shapes the diff.

> Before blockwatch 0.4.0 this action always behaved like `only_changed: "true"`. Set it to `"true"` to keep
> that behaviour.

## Seeing what ran

A green check means nothing failed, not that anything was checked. `verbosity: "summary"` logs one line of
counts, and `verbosity: "full"` prints the same report as JSON:

```
blockwatch: mode=all+diff, 240/240 files, 61 blocks (3 unchecked), 73 checks, 0 violations
```

See [Run Reports](https://github.com/mennanov/blockwatch/blob/main/docs/cli.md#run-reports) for how to read
it.

## Known limitations

- **Blocks in hidden directories are skipped.** blockwatch does not walk dot-directories, so a block in
  `.github/workflows/ci.yml` is never found and `.github/**/*.yml` matches nothing
  ([blockwatch#100](https://github.com/mennanov/blockwatch/issues/100)). `only_changed: "true"` does check
  them, because there the diff decides what to check.
- **An empty diff fails the step.** blockwatch errors on input it cannot read as a diff. That happens when
  `diff_pathspec` excludes everything a push changed, or after a force-push to an older commit. Add an `if:`
  condition if your workflow can produce one.
- **`diff_pathspec` does not exclude files from the default run**, only from the diff. Use `ignore` for that.
- **The first push of a new branch only diffs its last commit.** GitHub reports no previous tip for a new
  branch, so the action compares the head commit against its parent. Changes from earlier commits in the same
  push are not marked as changed; they get checked when you open a pull request.
- **Other events have no diff.** On `workflow_dispatch` or `schedule` the whole repository is still checked,
  but rules that need a diff (such as `affects`) do not run and `only_changed` is ignored.

## Runner requirements

GitHub-hosted runners work out of the box. No Rust toolchain is needed: `cargo-binstall` downloads a prebuilt
binary instead of compiling one. A self-hosted runner needs:

- **Actions Runner 2.327.1+ and glibc 2.28+** — `actions/checkout@v7` and `actions/cache@v6` need Node 24.
  Rules out Alpine, CentOS 7 and Ubuntu 18.04.
- **git 2.18+** — older versions make `actions/checkout` download a tarball with no `.git` directory, and the
  action needs `git diff`.
- **bash** — on Windows, Git Bash on `PATH`.
- **curl, plus tar (Linux) or unzip (macOS/Windows)** — used to fetch the binary.
- **Access to `github.com` and `crates.io`** — there is no offline install path.
- **A writable `CARGO_HOME`** (or `CARGO_INSTALL_ROOT`, if set) — the binary is installed and cached in its
  `bin` directory, which is added to `PATH` for the rest of the job.

## Running tests locally

```shell
act -W .github/workflows/local-test.yml
```
