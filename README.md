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
```
