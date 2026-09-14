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

By default, this checks every block in the repository and uses the git diff to identify changes. All inputs are optional.

## Inputs

| Input | Default | Description |
|---|---|---|
| `only_changed` | `"false"` | Check only blocks modified in the diff. See [Diff checking and scope](#diff-checking-and-scope) |
| `globs` | | File patterns to check (e.g. `"**/*.md,**/*.yml"`) |
| `ignore` | | File patterns to skip (e.g. `"target/**,dist/**"`) |
| `suppress` | | Violations to report without failing the run (e.g. `"docs/cli.md:cli-docs:keep-sorted"`) |
| `suppress_from` | | Files containing suppression addresses. See [Suppressing violations](#suppressing-violations) |
| `enable` | | Comma-separated validators to run (e.g. `"keep-sorted,keep-unique"`) |
| `disable` | | Comma-separated validators to skip (cannot be used with `enable`) |
| `extensions` | | File extension mappings, e.g. `"cxx=cpp"` |
| `verbosity` | `"summary"` | Output verbosity: `"none"`, `"summary"` (counts), or `"full"` (JSON) |
| `format` | `"json"` | Output format: `"json"` or `"sarif"` (SARIF 2.1.0) |
| `diff_pathspec` | | Additional git diff pathspecs (e.g. `":(exclude).github/"`) |

List inputs accept comma-separated strings or multi-line YAML:

```yaml
ignore: |
  target/**
  dist/**
```

## Diff checking and scope

On `push` and `pull_request` events, the action generates a git diff and feeds it to blockwatch:

- **Default (`only_changed: "false"`)**: Scans all blocks across the repository, using the diff to flag modified blocks. This allows cross-block validators like `affects` to verify that related blocks stay in sync.
- **`only_changed: "true"`**: Scans only the blocks touched by the diff.

`globs` and `ignore` filter which files are scanned. `diff_pathspec` only filters the diff itself. Hidden directories (like `.github/`) are scanned; only VCS directories (`.git`, `.hg`, `.jj`, `.svn`) and paths ignored by `.gitignore` or `ignore` are skipped.

Events without a diff (e.g. `workflow_dispatch`, `schedule`) scan the full repository, and `only_changed` is ignored.

## Suppressing violations

Suppressed violations are still reported in the output, but will not fail the step.

Addresses follow the syntax:
```
FILE[:BLOCK_NAME[:VALIDATOR[:HASH]]]
```
Shorter addresses match more broadly. For example, `path/to/file.md` suppresses all violations in that file. You can find exact violation addresses in previous run logs. See the [blockwatch documentation](https://github.com/mennanov/blockwatch/blob/main/docs/cli.md#suppressing-a-violation) for syntax details.

Suppressions can come from three sources, which all combine:

### 1. Action input (`suppress`)
```yaml
suppress: |
  docs/cli.md:cli-docs:keep-sorted
  legacy/generated.py
```

### 2. Commit messages or PR descriptions
Add a line to a commit message or PR description:
```text
Blockwatch-suppress: docs/cli.md:cli-docs:keep-sorted
```
The action inspects commits in the push or PR (and the PR body) for lines starting with `Blockwatch-suppress:`. Any detected suppressions are printed in the job log.

### 3. External files (`suppress_from`)
```yaml
- uses: mennanov/blockwatch-action@v1
  with:
    suppress_from: "ci/known-violations.txt"
```
Files use the same format: lines starting with `Blockwatch-suppress: ADDRESS` are parsed, and all other lines are ignored.

## Verbosity and reports

By default (`verbosity: "summary"`), blockwatch prints a one-line count to stdout:

```text
blockwatch: mode=all+diff, 240/240 files, 61 blocks (3 unchecked), 73 checks, 0 violations
```

Use `verbosity: "full"` for JSON output, or `"none"` to silence the summary.

## SARIF output

Set `format: "sarif"` to output a [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) log to stderr.

> **Note:** The action streams SARIF directly to the job log rather than saving it to a file, so it cannot be picked up by `github/codeql-action/upload-sarif`. If you need to upload a SARIF file to GitHub Code Scanning, invoke `blockwatch` directly in a custom step:
> ```bash
> blockwatch --format sarif 2> results.sarif
> ```

## Known limitations

- **Empty diffs fail the step**: If `diff_pathspec` excludes all changed files, or after certain force-pushes, git produces an empty diff. blockwatch treats an empty diff input as an error.
- **`diff_pathspec` does not exclude files from scanning**: It only shapes the diff. In default mode (`only_changed: "false"`), use `ignore` to exclude files from being scanned.
- **PR author suppressions**: Anyone who can open a PR or push a commit can suppress violations via `Blockwatch-suppress:` lines. Review the job log if this is a concern for your repo.
- **Malformed suppression syntax**: A line starting with `Blockwatch-suppress:` with invalid address syntax will fail the step.
- **Initial push on new branch**: Because GitHub provides no previous base commit for a newly pushed branch, only the head commit is diffed against its parent. Earlier commits in that push are checked once a PR is opened.
- **Events without diffs**: `workflow_dispatch` and `schedule` scan the full repository without diff context, so validators that depend on knowing what changed (such as `affects`) cannot check for simultaneous updates.

## Runner requirements

GitHub-hosted runners (`ubuntu-latest`, `macos-latest`, `windows-latest`) work out of the box. Prebuilt binaries are installed with `cargo-binstall` (no Rust toolchain needed).

Self-hosted runners require:
- **Actions Runner 2.327.1+ and glibc 2.28+** (Node 24 required for checkout and cache actions; Alpine, CentOS 7, and Ubuntu 18.04 are not supported)
- **git 2.18+**
- **bash** (Git Bash on Windows)
- **curl**, plus **tar** (Linux) or **unzip** (macOS/Windows)
- **Python 3.7+**, as `python3` or `python` on `PATH` — the action's logic runs there
- Network access to `github.com` and `crates.io`
- Writable `CARGO_HOME` or `CARGO_INSTALL_ROOT`

## Running tests locally

```shell
act workflow_dispatch -W .github/workflows/local-test.yml
```
