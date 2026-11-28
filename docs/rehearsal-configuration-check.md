# Offline configuration check (experiment)

`./rehearsal-check` and `make -s -j1 rehearse-check` share the same checker. A plain checkout fails closed with `failed/render_failed` for a valid digest argument: it does not install the trusted bootstrap. Do not use the test fixture or direct Python invocation as an operational bootstrap.

To exercise both entry points with synthetic, credential-free bundles and an isolated test fixture, run this from a dedicated kit worktree with the test dependencies already cached:

```sh
uv run --offline --no-project --with pytest --with pyyaml python -B -m pytest -q tests/test_rehearsal_check.py tests/test_owner_maintenance.py
```

The fixture covers pipe admission, digest binding, and in-memory rendering. It does not establish that verified interpreter and dependency objects were the ones loaded at startup. The worker's deadline begins inside Python; the qualified bootstrap still needs to cover startup within the same operation budget.

See the [configuration-check contract](../specs/004-isolated-proxy-rehearsal/contracts/configuration-check.md) for the pipe, independently recorded digest, and exit/result rules. Even a `checked` result is advisory: it neither exercises the proxy runtime nor authorizes staging, restart, or qualification.
