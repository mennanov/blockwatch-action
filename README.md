# blockwatch-action

GitHub Action that runs the [blockwatch](https://github.com/mennanov/blockwatch) linter on your repository.

## Quick start

<!-- <block name="quick-start" same-as="docs/configuration.md:scheduled-example,docs/suppressions.md:suppress-from-example,docs/suppressions.md:disable-commit-pr-suppress" same-as-pattern="uses:\s+mennanov/blockwatch-action@(?P<value>\S+)" line-pattern-forbidden="uses:\s+mennanov/blockwatch-action@v\d+(\s|$)"> -->
```yaml
on: [push, pull_request]

permissions:
  contents: read       # Read repository files and git history
  pull-requests: write # Optional: post/update sticky PR suppression comments

jobs:
  blockwatch:
    runs-on: ubuntu-latest
    steps:
      - uses: mennanov/blockwatch-action@v1.2.1
```
<!-- </block> -->

Checks all blocks against the git diff (using `blockwatch.toml` if present); all inputs are optional.

<!-- <block name="inputs" same-as="action.yml:action-inputs" same-as-format="identifier" same-as-pattern="^(?:## |\| `)(?P<value>[A-Za-z0-9_-]+)"> -->
## Inputs

| Input | Default | Description |
|---|---|---|
| `only_changed` | `"false"` | Check only blocks modified in the diff. See [Diff checking and scope](docs/configuration.md#diff-checking-and-scope) |
| `globs` | | Files, directories or patterns to check (e.g. `"src,**/*.md"`) |
| `ignore` | | File patterns to skip (e.g. `"target/**,dist/**"`) |
| `config` | | Config file to read instead of `blockwatch.toml`. See [Config file](docs/configuration.md#config-file) |
| `suppress` | | Violations to report without failing the run (e.g. `"docs/cli.md:cli-docs:keep-sorted"`) |
| `suppress_from` | | Files containing suppression addresses. See [Suppressing violations](docs/suppressions.md) |
| `suppress_from_commits` | `"true"` | Read suppressions from the messages of the commits in the pull request or push. See [Choosing who can suppress](docs/suppressions.md#choosing-who-can-suppress) |
| `suppress_from_description` | `"true"` | Read suppressions from a pull request description |
| `enable` | | Comma-separated validators to run (e.g. `"keep-sorted,keep-unique"`) |
| `disable` | | Comma-separated validators to skip (cannot be used with `enable`) |
| `only_blocks` | | Named blocks to check, and no others, as `FILE:BLOCK_NAME` (e.g. `"docs/cli.md:cli-docs"`). See [Selecting blocks](docs/configuration.md#selecting-blocks) |
| `skip_blocks` | | Named blocks to leave unchecked (cannot be used with `only_blocks`) |
| `extensions` | | Map an extension or a whole file name to a supported one, e.g. `"cu=cpp,Earthfile=dockerfile"` |
| `verbosity` | `"summary"` | Output verbosity: `"none"`, `"summary"` (counts), or `"full"` (JSON) |
| `format` | `"json"` | Output format: `"json"` or `"sarif"` ([SARIF 2.1.0](docs/reporting.md#sarif-output)) |
| `annotations` | `"true"` | Report violations and commit/PR suppression directives as GitHub annotations. See [Annotations](docs/reporting.md#annotations) |
| `annotations_limit` | `50` | How many violation annotations to emit at most |
| `summary` | `"true"` | Write violations and commit/PR suppression directives to the job summary as tables |
| `comment` | `"true"` | Post or update a sticky PR comment when commit/PR suppressions are found (needs `pull-requests: write`) |
| `diff_pathspec` | | Additional git diff pathspecs (e.g. `":(exclude).github/"`) |
<!-- </block> -->

List inputs accept comma-separated strings or multi-line YAML:

```yaml
ignore: |
  target/**
  dist/**
```

<!-- <block name="outputs" same-as="action.yml:action-outputs" same-as-format="identifier" same-as-pattern="^(?:## |\| `)(?P<value>[A-Za-z0-9_-]+)"> -->
## Outputs

| Output | Description |
|---|---|
| `commit_suppressions` | JSON array of entries found in commit messages (`commit`, `subject`, `line`, `address`, `applied`) |
| `description_suppressions` | JSON array of entries found in the PR description (`line`, `address`, `applied`) |
| `suppressions_truncated` | `"true"` when either JSON output exceeded 512 KiB and was truncated, otherwise `"false"` |
<!-- </block> -->

Outputs are always populated, even when reporting is disabled or the step fails, so downstream steps with `if: always()` can inspect them.

## Documentation

<!-- <block name="docs-index" same-as-paths="docs/*.md" same-as-pattern="(?P<value>docs/[^)\s]+\.md)"> -->
- [Configuration, diff scope, and block selection](docs/configuration.md) — `only_changed`, `globs`/`ignore`/`diff_pathspec`, `blockwatch.toml` precedence, and `only_blocks`/`skip_blocks`
- [Reporting, annotations, PR comments, and SARIF](docs/reporting.md) — verbosity, annotation severity and limits, job summary tables, sticky PR comments, and SARIF caveats
- [Runner requirements and supply-chain security](docs/runners-and-security.md) — self-hosted runner prerequisites and what a commit SHA pin covers
- [Suppressing violations and trust controls](docs/suppressions.md) — suppression syntax, `suppress`/`suppress_from`/commit/PR sources, and restricting who can suppress
<!-- </block> -->

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for running tests locally and pre-commit setup.
