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
          # Optional: pass extensions to blockwatch, comma-separated key=value
          extensions: "foo=bar,baz=qux"

          # Optional: control git diff pathspecs, e.g. exclude .github directory
          # Works like: git diff --patch ... -- ':(exclude).github/'
          # You can provide multiple space-separated pathspecs/options
          diff_pathspec: ":(exclude).github/ :(exclude)docs/"
```
