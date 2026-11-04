# Isolated RC Pilot Result — Blocked

**Date:** 2026-09-13

**Candidate:** LiteLLM `v1.102.0-rc.1`, immutable commit `16f6d924530c1cf2dc94b3e83c86a4632aefb7c0`

**Decision:** **blocked** for a complete pilot and any promotion. The local-only portions passed; provider validation and a client-executed function-tool round trip remain incomplete.

## Scope and boundaries honored

- Used a disposable Python environment and a disposable proxy configuration/state/log root.
- Bound the test proxy to `127.0.0.1` only on a temporary high port.
- Used a synthetic, disposable loopback authentication value and a synthetic local-backend value; no provider credential was read or written.
- Did not alter Hermes provider configuration, an existing LiteLLM configuration, credentials, services, LaunchAgents, login items, firewall rules, or the existing local-model process.
- Did not contact a provider API or send any cloud inference request.
- Used only short synthetic local prompts. Raw configuration, logs, request bodies, and response bodies were removed during teardown.

## Results

### Environment, binding, and authentication — passed after dependency correction

- The candidate remained LiteLLM `1.102.0`; `pip check` passed.
- The proxy launched only after installing its `proxy` extra. The original candidate manifest installs the Router-validation dependency set only and is not sufficient to start `litellm proxy`.
- An unauthenticated request initially failed as HTTP `500`, not the required HTTP `401`: the RC proxy authentication error path imports `prisma`, which was not installed by `litellm[proxy]` in this pilot.
- After installing `prisma==0.15.0` in the disposable environment, the unauthenticated request correctly returned HTTP `401`; an authenticated model-list request returned HTTP `200`.
- A fresh disposable environment installed `requirements-pilot.txt`, imported the proxy runtime and Prisma, and passed `pip check` with LiteLLM `1.102.0`.
- Loopback-only binding and liveness were confirmed.
- The subsequent local function-tool investigation determined why the forced tool request emitted no call through the proxy; see `docs/local-tool-compatibility.md`.

### Local proxy behavior — passed except tools

- One bounded local completion succeeded through the proxy with a stable `local` alias and non-empty final content.
- One bounded local streamed completion succeeded with valid SSE framing and a completion marker.
- A forced, synthetic function-tool request returned HTTP `200` but emitted no tool call. Therefore a client-executed tool round trip was not validated for this local model/route.

### Fallback/cooldown lifecycle — passed without provider traffic

- `preflight.py --router-test` passed in the exact candidate environment: synthetic localhost `429`, ordered fallback, deployment-failure hook event, and output-cap rewrite.

### Provider validation — not run

The public kit deliberately contains no 1Password references or provider-account mapping. No unrelated workstation configuration was searched for credentials. Consequently, read-only provider catalog checks and all cloud request classes remain unvalidated.

## Teardown verification

- Stopped only the disposable pilot proxy and verified its temporary port was closed.
- Confirmed the pre-existing local-model endpoint still answered its read-only model-list probe as before.
- Removed raw pilot logs, configuration, request/response bodies, and transient state.

## Required follow-up

1. Supply explicit, command-scoped 1Password references or a dedicated pilot credential mapping for the intended providers; do not copy keys from an existing Hermes/service process.
2. Decide whether this local model should support function tools. If yes, validate tool-capable model/template settings in a separate isolated test.
3. Repeat the authenticated catalog and approved minimal cloud-request gates after the credential mapping is available.
4. Do not promote this RC or start a persistent local proxy. It remains a validation candidate only.
