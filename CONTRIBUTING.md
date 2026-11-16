# Contributing

## Running tests locally

Run the workflow locally with [`act`](https://github.com/nektos/act) and run the Python unit test suite:

```shell
act workflow_dispatch -W .github/workflows/local-test.yml
python3 -m unittest discover -s scripts -v
```

Note that the unit tests invoke the real `blockwatch` binary at the exact version pinned in `action.yml`.

If you use `pre-commit`, install the hooks once per clone (`pre-commit install`) so formatting (`black`), type checking (`pyrefly`), workflow auditing (`zizmor`), and unit tests run before committing.
