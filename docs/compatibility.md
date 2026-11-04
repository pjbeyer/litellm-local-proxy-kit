# LiteLLM Compatibility Status

## Current state

The imported kit preserves the source experiment’s **deployment-attempt** failure-hook contract. Its quota circuit breaker must observe an individual failed provider deployment before ordered fallback proceeds.

## Verified incompatible environment

At import time, the locally installed LiteLLM release `1.99.0` was checked with `preflight.py` and is intentionally rejected:

- `CustomLogger` provides `async_pre_call_deployment_hook` but not `async_post_call_failure_deployment_hook`.
- It also rejects the source experiment’s `classification_mode` and `enable_context_window_escalation` complexity-router fields.
- Replacing the deployment hook with `async_post_call_failure_hook` was tested with two ephemeral localhost endpoints. The request-level hook did **not** run after the first failed deployment in ordered fallback, so it cannot safely implement the circuit breaker.

The preflight failure is expected and safe: **do not activate this kit with LiteLLM 1.99.0.**

## Publication gate

The isolated compatibility matrix now includes LiteLLM `1.101.0` as a passing stable compatibility candidate. In a disposable `1.101.0` environment, the imported callback/router contract, all 37 offline tests, and the localhost fallback lifecycle passed. A synthetic two-phase tool transport probe also passed, while confirming that the proxy still omits explicit downstream `stream: false`; see [stable candidate validation](stable-candidate-validation.md) and the [compatibility matrix](compatibility-matrix.md).

This is not a production recommendation and does not resolve the local function-tool gate. Before publishing a runnable release:

1. Preserve the exact stable validation environment in reproducible, declared install metadata and repeat the matrix when it changes.
2. `CustomLogger` must expose both the pre-deployment and per-deployment failure hooks used by `custom_callbacks.py`.
3. `preflight.py --router-test` must prove one failed local mock deployment, one ordered fallback, one failure-hook event, and the expected rewritten output cap.
4. The local function-tool contract documented in [local-tool compatibility](local-tool-compatibility.md) must pass through the exact candidate proxy and selected local endpoint.

Do not weaken the preflight or local-tool guards merely to make a newer release load. Any callback or proxy adaptation needs the same lifecycle test and an independent review.
