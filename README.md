# blockwatch-action

GitHub Action for [mennanov/blockwatch](https://github.com/mennanov/blockwatch) linter.

## Use in a GitHub Action

Add the following to your workflow `.yml` file:

```yaml
jobs:

  blockwatch:
    runs-on: ubuntu-latest

    steps:
      - uses: mennanov/blockwatch-action@v1
        with:
          # Optional: pass extensions to blockwatch, comma-separated key=value or on new lines
          extensions: "foo=bar,baz=qux"

          # Optional: Validators to enable (comma separated)
          enable: "keep-sorted,keep-unique"

          # Optional: Validators to disable (comma separated), can't be used with "enable"
          # disable: "check-ai"

          # Optional: Glob patterns for files to ignore (comma separated)
          ignore: "target/**,dist/**"

          # Optional: Glob patterns for files to check (comma separated)
          # If provided, blockwatch will run on these files in addition to the diff
          globs: "**/*.md,**/*.yml"

          # Optional: report what the run actually checked, on stdout.
          # One of "none" (default), "summary" (a line of counts) or "full" (JSON).
          # Violations still go to stderr, so the two can be parsed separately.
          verbosity: "summary"

          # Optional: control git diff pathspecs, e.g. exclude .github directory
          # Works like: git diff --patch ... -- ':(exclude).github/'
          # You can provide multiple space-separated pathspecs/options
          diff_pathspec: ":(exclude).github/ :(exclude)docs/"
```

## Seeing what ran

A green check means nothing failed — not that anything was checked. `verbosity: "summary"` puts one line of
counts in the job log so a run that validated nothing is visible rather than indistinguishable from a clean one:

```
blockwatch: 34/240 files, 61 blocks (3 unchecked), 73 checks, 0 violations
```

`verbosity: "full"` prints the same thing as JSON, listing every block in scope and the validators that checked
it. The report goes to **stdout** and violations go to **stderr**, so each can be piped and parsed on its own.
Under a diff the report covers only the blocks that diff touched.

## Known limitations

- **`globs` does not reach into hidden directories.** A pattern like `.github/**/*.yml` matches nothing,
  because blockwatch's file walker skips dot-directories before the pattern is applied
  ([mennanov/blockwatch#100](https://github.com/mennanov/blockwatch/issues/100), open as of 0.3.10). This
  affects only the whole-tree `globs` scan — files under `.github/` **are** still checked when they show up
  in the diff, which is the normal case on a pull request or push. The gap is that listing such a glob looks
  like it adds a whole-tree check on those files, and it does not.
- **On the first push to a new branch, only the head commit is checked.** That push reports no previous
  revision to diff against, and a brand-new branch has no base to substitute, so earlier commits in the same
  push are not examined. They get checked normally on the pull request.
- **Events other than `pull_request` and `push` run glob checks only.** There is no diff on a
  `workflow_dispatch` or `schedule` run, so diff-driven validators such as `affects` cannot run. If `globs`
  is set, the whole-tree scan still runs; if it isn't, the step logs a warning and does nothing.

## Runner requirements

Works out of the box on GitHub-hosted runners. A **self-hosted** runner needs a few things in place:

- **Actions Runner 2.327.1 or newer** — `actions/checkout@v7` and `actions/cache@v6` run on Node 24, which requires it.
- **glibc 2.28 or newer** — also a Node 24 requirement. Rules out CentOS 7, Ubuntu 18.04, and musl-based images like Alpine.
- **git 2.18 or newer** — older versions make `actions/checkout` silently fall back to downloading a tarball with no `.git` directory, which breaks the `git diff` this action depends on.
- **bash** — every step runs with `shell: bash`; on Windows that means Git Bash needs to be on `PATH`.
- **curl, plus tar (Linux) or unzip (macOS/Windows)** — used by `cargo-binstall` to fetch the prebuilt binary.
- **Network access to `github.com` and `crates.io`** — needed by `cargo-binstall` to download the binary. There's no offline install path.
- **A writable `CARGO_HOME`** (or `CARGO_INSTALL_ROOT`, if that's set instead) — the resolved directory is used as-is, with no fallback, so the install step fails outright if it isn't writable.

A Rust toolchain is **not** required — the binary comes from `cargo-binstall`, never built from source.

The binary is installed into, and cached from, whichever of these is set first: `$CARGO_INSTALL_ROOT/bin`,
`$CARGO_HOME/bin`, or `~/.cargo/bin`. That directory is added to `PATH` for the rest of the job.

## Running tests locally

```shell
act -W .github/workflows/local-test.yml
```
