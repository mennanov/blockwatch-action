# Suppressing Violations and Trust Controls

Suppressed violations are still reported in the output, but will not fail the step.

Addresses follow the syntax:

```text
FILE[:BLOCK_NAME[:VALIDATOR[:HASH]]]
```

Shorter addresses match more broadly. For example, `path/to/file.md` suppresses all violations in that file. You can find exact violation addresses in previous run logs or annotations. See the [blockwatch documentation](https://github.com/mennanov/blockwatch/blob/main/docs/cli.md#suppressing-a-violation) for syntax details.

Suppressions can come from three sources, which all combine:

## 1. Action input (`suppress`)

```yaml
suppress: |
  docs/cli.md:cli-docs:keep-sorted
  legacy/generated.py
```

## 2. Commit messages or PR descriptions

Add a line to a commit message or pull request description:

```text
blockwatch-suppress: docs/cli.md:cli-docs:keep-sorted
```

The action inspects commits in the push or PR (and the PR body) for lines starting with `blockwatch-suppress:`. Any detected directives are printed in the job log, written to the job summary, exported via [outputs](../README.md#outputs), and surfaced as GitHub [annotations](reporting.md#annotations). When `comment: "true"` and `pull-requests: write` is granted, any actually suppressed violations are also listed in a [sticky PR comment](reporting.md#sticky-pr-comment).

The PR description is read from the GitHub API when the run starts, not from the event that triggered it. The two differ after an edit: a re-run replays the original event, so a description-based suppression would otherwise not take effect until the next push. Reading it live means **edit the description, press Re-run, and the violation stops blocking**. The job log names which source was used; without a usable `GITHUB_TOKEN` the action falls back to the event payload.

> **Caveats:**
> - On a newly pushed branch or force-push, only the head commit's message is read for suppressions; earlier commits in that push are checked once a PR is opened.
> - A line starting with `blockwatch-suppress:` with invalid address syntax, or with no address at all (such as a placeholder left in a pull request template), fails the step unless its source is turned off.

## 3. External files (`suppress_from`)

<!-- <block name="suppress-from-example" same-as="README.md:quick-start" same-as-pattern="uses:\s+mennanov/blockwatch-action@(?P<value>\S+)" line-pattern-forbidden="uses:\s+mennanov/blockwatch-action@v\d+(\s|$)"> -->
```yaml
- uses: mennanov/blockwatch-action@v1.2.1
  with:
    suppress_from: "ci/known-violations.txt"
```
<!-- </block> -->

Files use the same format: lines starting with `blockwatch-suppress: ADDRESS` are parsed, and all other lines are ignored.

## Choosing who can suppress

By default, whoever writes a commit or opens/edits a pull request description can suppress a violation, including one their own change introduces. Because the PR description is read live, an edit takes effect on the next run or re-run without a new commit—so a suppression can appear after a review, leaving the approval intact. The job log lists every address that was applied and where the description came from.

Where that is too much trust, such as for outside contributors or automated agents, turn those sources off:

<!-- <block name="disable-commit-pr-suppress" same-as="README.md:quick-start" same-as-pattern="uses:\s+mennanov/blockwatch-action@(?P<value>\S+)" line-pattern-forbidden="uses:\s+mennanov/blockwatch-action@v\d+(\s|$)"> -->
```yaml
- uses: mennanov/blockwatch-action@v1.2.1
  with:
    suppress_from_commits: "false"
    suppress_from_description: "false"
```
<!-- </block> -->

Each input works alone. `suppress_from_description: "false"` is the narrower choice: it stops a suppression arriving after a review without a new commit, and leaves commit messages working. With both off, a `blockwatch-suppress:` line can't change the result or fail the step: ignored lines in commit messages or the event payload's PR body are listed in the job log and job summary, emitted as `::notice` annotations (or `::warning` if the other source is enabled on a pull request), and annotations point to the `suppress` input.

To trust branches in your own repository but not forks:

```yaml
suppress_from_commits: ${{ github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository }}
suppress_from_description: ${{ github.event.pull_request.head.repo.full_name == github.repository }}
```

This does not separate people from agents that push to a branch in the repository; those need the inputs set to `"false"`.

These inputs move a suppression into the diff, where a reviewer sees it. They do not stop a pull request from changing what is checked: it can still edit the workflow, `blockwatch.toml`, a `suppress_from` file, or the block tags themselves. To keep those under control, require review from a code owner on them.
