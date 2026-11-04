# Source Inventory and Import Decision

The initial source folder was an exploratory LiteLLM proxy test directory. This repository preserves the coherent, self-contained `auto-free-kit` implementation and excludes personal-machine operational artifacts.

## Imported and sanitized

- `README.md` — transformed from the source kit's handoff into a generic methodology. Personal absolute paths were replaced with `$HOME` paths.
- `proxy_config.yaml` — canonical ordered-routing configuration. The source's fake `sk-local` literal was replaced with `os.environ/LOCAL_OMLX_API_KEY`.
- `custom_callbacks.py` — per-deployment request policy and quota circuit breaker.
- `preflight.py` — installed-LiteLLM compatibility and localhost router checks.
- `proxy-env.sh` — environment-derived Cloudflare endpoint helper.
- `tests/test_callbacks.py` — 33 offline callback tests that stub LiteLLM.
- `tests/test_preflight.py` — offline regression tests for preflight command-mode behavior.
- `docs/test-results.md` — historical offline test evidence with an explicit non-live caveat.
- `SHA256SUMS.txt` — regenerated for the sanitized/imported repository files.

## Deliberately excluded

- `proxy.log` — runtime log; not appropriate for publication without a separately scoped scrub/review.
- `*.bak.*` and top-level duplicates — intermediate experiment history, superseded by the coherent kit.
- `proxy_config.json` — experimental alternate representation containing a secret-shaped placeholder.
- `hermes.config.yaml` — a personal Hermes integration artifact; extract a generic integration example later if warranted.
- `ai.litellm.proxy.plist` and `run-proxy-server.sh` — machine/service-specific artifacts. A portable macOS launch guide needs its own review and spec before inclusion.
- Source `SHA256SUMS.txt` — invalid after sanitization; regenerated checksums replace it.

## Publication gate

No direct source copy is automatically publication-ready. A future public release still requires a declared compatible LiteLLM version that passes the deployment-hook lifecycle test, current-provider verification, licensing, a generic quickstart, tests in a declared supported environment, and an independent review of all staged content. See [compatibility status](compatibility.md).
