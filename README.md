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

          # Optional: control git diff pathspecs, e.g. exclude .github directory
          # Works like: git diff --patch ... -- ':(exclude).github/'
          # You can provide multiple space-separated pathspecs/options
          diff_pathspec: ":(exclude).github/ :(exclude)docs/"
```

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
