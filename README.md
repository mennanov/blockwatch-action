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

| Input           | Default     | What it does                                                                                                 |
| --------------- | ----------- | ------------------------------------------------------------------------------------------------------------ |
| `only_changed`  | `"false"`   | `"true"` checks only the blocks the diff touched. See [What gets checked](#what-gets-checked)                |
| `globs`         |             | Check only files matching these patterns, e.g. `"**/*.md,**/*.yml"`                                          |
| `ignore`        |             | Skip files matching these patterns, e.g. `"target/**,dist/**"`                                               |
| `suppress`      |             | Report these violations without failing, e.g. `"docs/cli.md:cli-docs:keep-sorted"`                           |
| `suppress_from` |             | Read extra addresses from files, e.g. `"notes.txt"`. See [Suppressing a violation](#suppressing-a-violation) |
| `enable`        |             | Run only these validators, e.g. `"keep-sorted,keep-unique"`                                                  |
| `disable`       |             | Run all validators except these, e.g. `"check-ai"`. Cannot be used with `enable`                             |
| `extensions`    |             | Treat one file extension as another, e.g. `"cxx=cpp"`                                                        |
| `verbosity`     | `"summary"` | `"none"`, `"summary"` (a line of counts) or `"full"` (JSON)                                                  |
| `format`        | `"json"`    | `"sarif"` reports the violations as a SARIF 2.1.0 log instead of JSON diagnostics                            |
| `diff_pathspec` |             | Extra git pathspecs for the diff, e.g. `":(exclude).github/"`                                                |

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

Hidden directories such as `.github/` are scanned like any others; only the state directories of a version
control system (`.git`, `.hg`, `.jj`, `.svn`) are skipped, along with whatever `.gitignore` and `ignore`
exclude.

> Before blockwatch 0.4.0 this action always behaved like `only_changed: "true"`. Set it to `"true"` to keep
> that behaviour.

## Suppressing a violation

`suppress` takes the address of a violation and stops it failing the run. The violation is still reported —
only the exit code changes — so use it when a rule is wrong at one particular site and you would rather not
edit the source or turn the validator off everywhere with `disable`:

```yaml
suppress: |
  docs/cli.md:cli-docs:keep-sorted
  legacy/generated.py
```

An address is `FILE[:BLOCK_NAME[:VALIDATOR[:HASH]]]`, and the usual way to write one is to copy it out of a
previous run's output. Every length is valid, and the shorter it is, the more it covers: `FILE` alone
suppresses every violation in that file, which is also the only way to reach a block that has no `name`. An
address that covers nothing is ignored; a malformed one fails the step. See
[Suppressing a Violation](https://github.com/mennanov/blockwatch/blob/main/docs/cli.md#suppressing-a-violation)
for the full grammar.

### Suppressing from a commit message or pull request description

A suppression can also travel with the work that needs it, instead of being written into the workflow. The
action always reads the commit messages under test, and on a pull request its description, for lines of the
form:

```
Blockwatch-suppress: docs/cli.md:cli-docs:keep-sorted
```

Write one in the commit that introduces the violation, or in the pull request description, and it applies to
that run. No input is needed and nothing else in the text matters — every other line is ignored, so an
ordinary commit message is valid input.

Which messages are read follows the diff exactly, so only the work being checked can suppress anything:

| Event               | Messages read                                     |
| ------------------- | ------------------------------------------------- |
| `pull_request`      | Every commit the PR adds, plus the PR description |
| `push`              | Every commit the push added                       |
| `push` (new branch) | The head commit only, as with the diff            |
| Anything else       | The head commit only                              |

A commit elsewhere in the repository cannot reach in and silence a violation. The addresses that are found
are echoed into the job log, since a suppression coming from a commit message is otherwise invisible to
anyone reading the workflow file.

`suppress_from` adds further files to the same mechanism, for addresses that live somewhere else entirely:

```yaml
- uses: mennanov/blockwatch-action@v1
  with:
    suppress_from: "ci/known-violations.txt"
```

All three sources accumulate: `suppress`, `suppress_from`, and the messages. A path `suppress_from` names
that the run cannot read fails the step, so a typo is reported rather than silently dropping the
suppressions.

## Seeing what ran

A green check means nothing failed, not that anything was checked. `verbosity: "summary"` logs one line of
counts, and `verbosity: "full"` prints the same report as JSON:

```
blockwatch: mode=all+diff, 240/240 files, 61 blocks (3 unchecked), 73 checks, 0 violations
```

See [Run Reports](https://github.com/mennanov/blockwatch/blob/main/docs/cli.md#run-reports) for how to read
it.

## Reporting violations as SARIF

`format: "sarif"` makes blockwatch write the violations as a
[SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) log instead of its JSON
diagnostics, for code scanning and anything else that reads the format:

```yaml
- uses: mennanov/blockwatch-action@v1
  with:
    format: "sarif"
```

Each violation carries its address as a `partialFingerprints` entry, so a consumer can recognise the same
violation across runs, and one named by `suppress` is marked with SARIF's own
`"suppressions": [{"kind": "external"}]`. Unlike the JSON diagnostics, a SARIF log is written even when the
run finds nothing, because a code-scanning service expects a log from every run.

## Known limitations

- **An empty diff fails the step.** blockwatch errors on input it cannot read as a diff. That happens when
  `diff_pathspec` excludes everything a push changed, or after a force-push to an older commit. Add an `if:`
  condition if your workflow can produce one.
- **`diff_pathspec` does not exclude files from the default run**, only from the diff. Use `ignore` for that.
- **Anyone who can open a pull request can suppress a violation in it**, by putting a `Blockwatch-suppress:`
  line in the description or in a commit message — including someone pushing from a fork. The addresses are
  echoed into the job log, but if that trade is wrong for your repository, require a review of the log or
  check the blocks in a separate workflow that does not read these messages.
- **A malformed address in a commit message fails the step.** A line beginning `Blockwatch-suppress:` whose
  value is not a valid address (too many `:` segments, or a trailing one) is an error, not a line to skip.
  Prose that merely mentions the prefix mid-sentence, or quotes it with `>`, is not matched and is safe.
- **The SARIF log is not written to a file.** Like the JSON diagnostics it goes to stderr, which the action
  leaves attached to the job log, so there is nothing for `github/codeql-action/upload-sarif` to pick up. Run
  `blockwatch --format sarif 2> results.sarif` in your own step if you need to upload it.
- **The first push of a new branch only diffs its last commit.** GitHub reports no previous tip for a new
  branch, so the action compares the head commit against its parent. Changes from earlier commits in the same
  push are not marked as changed; they get checked when you open a pull request.
- **Other events have no diff.** On `workflow_dispatch` or `schedule` the whole repository is still checked,
  but a check that needs to know what changed (such as whether an `affects` target was updated alongside its
  source) does not run, and `only_changed` is ignored. `affects` still verifies that the blocks it names
  exist.

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
