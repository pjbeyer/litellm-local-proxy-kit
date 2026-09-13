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

The isolated compatibility matrix identified `v1.102.0-rc.1` commit `16f6d924530c1cf2dc94b3e83c86a4632aefb7c0` as a passing release candidate. It is pinned for isolated validation in `requirements-candidate.txt`; see [compatibility matrix](compatibility-matrix.md). It is not yet a production recommendation.

Before publishing a runnable release, identify and pin a stable LiteLLM version that passes both of these gates:

1. `CustomLogger` exposes both the pre-deployment and per-deployment failure hooks used by `custom_callbacks.py`.
2. `preflight.py --router-test` proves one failed local mock deployment, one ordered fallback, one failure-hook event, and the expected rewritten output cap.
3. The local function-tool contract documented in [local-tool compatibility](local-tool-compatibility.md) passes through the exact candidate proxy and selected local endpoint.

Do not weaken the preflight guard merely to make a newer release load. Any callback adaptation needs the same lifecycle test and an independent review.
