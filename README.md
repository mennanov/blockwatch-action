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

## Running tests locally

```shell
act -W .github/workflows/local-test.yml
```
