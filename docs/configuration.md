# Configuration, Diff Scope, and Block Selection

## Diff checking and scope

On `push` and `pull_request` events, the action generates a git diff and feeds it to blockwatch:

- **Default (`only_changed: "false"`)**: Scans all blocks across the repository, using the diff to flag modified blocks. This allows cross-block validators like `affects` to verify that related blocks stay in sync.
- **`only_changed: "true"`**: Scans only the blocks touched by the diff.

`globs` and `ignore` filter which files are scanned. Each entry in `globs` has to select at least one file to check, or the step fails; with `only_changed: "true"`, the file need not be one the diff touches. Hidden directories (like `.github/`) are scanned; only VCS directories (`.git`, `.hg`, `.jj`, `.svn`) and paths ignored by `.gitignore` or `ignore` are skipped.

`diff_pathspec` only filters the diff itself—it does **not** exclude files from scanning. In the default mode (`only_changed: "false"`), use `ignore` to exclude files from being scanned.

### Which diff is used

- **Pull request**: Everything the pull request changes, against the point where it left its base branch.
- **Push**: Everything since the branch's previous tip.
- **Push that creates a branch, or force-push**: Only the head commit, against its first parent. GitHub provides no previous base commit for a newly pushed branch, and a force-push replaces the previous tip, which then exists on no branch and is not fetched. Earlier commits in a new branch are checked once a pull request is opened.
- **Events without a diff (`workflow_dispatch`, `schedule`)**: Scan the full repository without diff context, and `only_changed` is ignored. Validators that depend on knowing what changed (such as `affects`) cannot check for simultaneous updates on these events.

A diff that comes out empty (empty commits, changes that cancel out, or a `diff_pathspec` that excludes everything) is not an error: it means no block changed. Every block is still checked, or none with `only_changed: "true"`. A diff git fails to produce fails the step with git's own error.

## Config file

blockwatch reads the settings a project always uses from `blockwatch.toml` at the repository root, so the action, a pre-commit hook, and local runs share one copy. The action picks it up with no input. Set `config` to read another file instead, relative to the repository root. See the [blockwatch documentation](https://github.com/mennanov/blockwatch/blob/main/docs/cli.md#config-file) for the keys.

The inputs combine with the file the way blockwatch's flags do:

| Input | With the file's key |
|---|---|
| `ignore` | Both lists apply |
| `extensions` | Both apply; the input wins for the same extension |
| `enable`, `disable` | The input replaces the file's selection |
| `only_blocks`, `skip_blocks` | The input replaces the file's `only-blocks` or `skip-blocks` |

So `disable: "line-count"` turns a validator the file disables back on. List it in the input too to keep it off.

The file can also declare blocks around values in files that cannot hold a comment, such as a version in `package.json`. Their violations are reported, and annotated, on that file.

## Selecting blocks

`only_blocks` checks only the blocks it lists, and `skip_blocks` checks every block but those. Each entry is the address of one named block, `FILE:BLOCK_NAME`: the start of the suppression address an [annotation](reporting.md#annotations) shows. An unnamed block has no address, so to leave out a whole file use `globs` or `ignore`.

- An address that matches no block fails the step, so a renamed block can't quietly turn its checks on or off. A block that `globs`, `ignore`, or `only_changed` leaves out of the run still counts as a match.
- A skipped block still exists: `affects` and `same-as` in other blocks can still refer to it.
- `only_blocks` and `skip_blocks` can't be combined. Either one replaces the `only-blocks` or `skip-blocks` list of `blockwatch.toml`.

For example, a block that needs the network can be skipped for every normal run in `blockwatch.toml`:

```toml
skip-blocks = ['src/models.rs:latest-model']
```

and checked on its own by a scheduled workflow:

<!-- <block name="scheduled-example" same-as="README.md:quick-start" same-as-pattern="uses:\s+mennanov/blockwatch-action@(?P<value>\S+)" line-pattern-forbidden="uses:\s+mennanov/blockwatch-action@v\d+(\s|$)"> -->
```yaml
on:
  schedule:
    - cron: "0 6 * * 1"

jobs:
  blockwatch:
    runs-on: ubuntu-latest
    steps:
      - uses: mennanov/blockwatch-action@v1.2.1
        with:
          only_blocks: "src/models.rs:latest-model"
```
<!-- </block> -->
