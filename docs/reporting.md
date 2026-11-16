# Reporting, Annotations, PR Comments, and SARIF

The action reads blockwatch's diagnostics and surfaces them in the job log, GitHub annotations, the job summary, and (when enabled) a sticky pull request comment. `format` and `annotations` are independent: both JSON and SARIF outputs are parsed, so set `annotations: "false"` and `summary: "false"` if you want log-only output. Reading diagnostics requires Python 3, which every GitHub-hosted runner already has.

## Verbosity and job logs

By default (`verbosity: "summary"`), blockwatch prints a one-line count to stdout:

```text
blockwatch: mode=all+diff, 240/240 files, 61 blocks (3 unchecked), 73 checks, 0 violations
```

Use `verbosity: "full"` for JSON output, or `"none"` to silence the summary. The JSON (or SARIF) diagnostics stay in the job log either way.

## Annotations

Violations are reported as GitHub annotations as well as in the log, so each one appears on the line it refers to in the pull request and in the check run for the job:

```text
::error file=docs/cli.md,line=12,endLine=12,col=1,endColumn=40,title=blockwatch: keep-sorted::Block docs/cli.md:cli-docs defined at line 9 has an out-of-order line 12 (asc)
Suppress with: blockwatch-suppress: docs/cli.md:cli-docs:keep-sorted:35385fe8
```

- **Severity levels**: A violation that fails the run is an **error**. A violation [suppressed](suppressions.md) via commit messages or a pull request description is elevated to a **warning** (alongside a summary annotation naming the commit/PR directive). A violation suppressed only via `suppress` or `suppress_from` stays a **notice**, because it is already configured in the workflow or repository.
- **Directive annotations**: When `blockwatch-suppress:` lines appear in commit messages or the pull request description, the action emits an annotation per source even when no violation matched—as a **warning** when `suppress_from_commits` or (on a pull request) `suppress_from_description` is `"true"`, or as a **notice** when neither applies.
- **Suppression hints**: Named blocks carry their suppression address into the annotation, ready to copy into a commit message or the `suppress` input. When no `blockwatch-suppress:` line can reach the run, the annotation offers the address for the `suppress` input alone.
- **Run errors**: A run blockwatch cannot finish (a malformed tag, a missing config or `suppress_from` file, an `only_blocks` address that matches no block) checks nothing and reports no violation. Its error becomes one **error** annotation of its own, attached to no file.

### Annotation limits and inline rendering

- **Inline annotations appear only on changed lines**: GitHub renders an annotation in the *Files changed* tab only where the pull request touches that line. In the default mode (`only_changed: "false"`), the whole repository is checked, so a violation in an untouched file is reported in the check run and the job summary, but has no line in the diff to appear on.
- **Annotation cap**: GitHub renders a limited number of annotations per step and drops the rest without saying so, so the action emits at most `annotations_limit` (<!-- <block name="annotations-limit" same-as="action.yml:annotations-limit-default,scripts/action/config.py:config-annotations-limit-default,README.md:readme-annotations-limit" same-as-pattern="(?P<value>\d+)"> -->50<!-- </block> --> by default) and reports how many it left out.

## Job summary

When `summary: "true"` (the default), violations and any commit/PR-description suppression directives are written to the job summary as tables. GitHub does not cap the summary the way it caps annotations, so the table is where a long list stays complete—listing up to <!-- <block name="summary-row-limit" same-as="scripts/action/reporting.py:summary-row-limit" same-as-pattern="(?P<value>\d+)"> -->1000<!-- </block> --> rows.

## Sticky PR comment

When `comment: "true"` (the default) and `pull-requests: write` permission is granted, any violations suppressed via commit messages or the pull request description are listed in a sticky comment on the PR page, linking to the flagged code and commit diff, and removed again once none remain.

On workflows with `pull-requests: read` or `pull_request` runs from public forks (where GitHub restricts `GITHUB_TOKEN` to read-only), posting the comment is skipped cleanly with a log note while annotations, the job summary, and outputs still run. Set `comment: "false"` to turn off the built-in comment altogether.

## SARIF output

Set `format: "sarif"` to output a [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) log to stderr.

> **Note:** The action streams SARIF directly to the job log rather than saving it to a file, so it cannot be picked up by `github/codeql-action/upload-sarif`. If you need to upload a SARIF file to GitHub Code Scanning, invoke `blockwatch` directly in a custom step:
> ```bash
> blockwatch --format sarif 2> results.sarif
> ```
