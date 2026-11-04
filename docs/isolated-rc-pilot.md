# Isolated LiteLLM Release-Candidate Pilot

## Purpose

Validate the compatible LiteLLM release-candidate build in an isolated, loopback-only pilot before considering any change to a running proxy or Hermes provider path.

**Candidate under test:**

- LiteLLM release-candidate commit `16f6d924530c1cf2dc94b3e83c86a4632aefb7c0`
- Router-only compatibility input: `requirements-candidate.txt`
- Proxy-pilot runtime input: `requirements-pilot.txt`

The compatibility matrix already proved that this candidate passes the repository's 36 offline tests and the no-provider localhost Router lifecycle test. This protocol covers the later, separately authorized credentialed validation.

## Hard boundaries

This pilot must not:

- modify the primary Hermes provider or its configuration;
- overwrite an existing LiteLLM configuration, callback, credential source, proxy database, or state file;
- change a LaunchAgent, service, login item, shell startup file, firewall, or network binding;
- bind beyond `127.0.0.1`;
- print, write, commit, or retain resolved credentials;
- use production-like traffic or confidential prompts;
- make provider inference requests until the explicit inference gate is approved.

The pilot has no automatic promotion path. Production deployment, public publication, service activation, or any credentialed provider call remains human-gated.

## Pilot environment contract

Use a disposable directory and a dedicated Python virtual environment. Keep its configuration, state, and logs under that disposable root. Delete the root after recording a sanitized outcome summary.

- Bind any test proxy only to `127.0.0.1` on an unused high port.
- Use a fresh local state database under the disposable root.
- Install the candidate's proxy runtime from `requirements-pilot.txt`; it is a disposable pilot manifest, not a production lockfile.
- Supply credentials only through the established command-scoped secret resolver.
- Use distinct, explicitly named pilot credentials/accounts where available; never copy values from another process into a file.
- Keep request logging at a level that excludes prompts, responses, and credential values.

## Gates

### Gate 0 — existing offline evidence

**Status:** passed in the compatibility matrix.

- [x] Candidate installs from the immutable commit.
- [x] `pip check` passes.
- [x] 36 offline tests pass.
- [x] `preflight.py --router-test` proves one localhost 429, one ordered fallback, one deployment-failure-hook event, and output-cap rewriting.

### Gate 1 — isolated environment readiness

**Mechanical checks:**

- [ ] Dedicated virtual environment uses the candidate commit.
- [ ] Candidate version and commit are recorded without credentials.
- [ ] Test configuration resolves only paths under the disposable root.
- [ ] Any listener is confirmed as `127.0.0.1` only.
- [ ] Existing Hermes and LiteLLM processes/configuration are read back unchanged.
- [ ] Secret and private-path scans pass on any retained evidence.

**Human gate:** approve before starting a test proxy or resolving any credential.

### Gate 2 — credential and authentication boundary

**Mechanical checks:**

- [ ] Credential resolver succeeds without printing values.
- [ ] Unresolved secret references fail closed.
- [ ] A request without the pilot's required local authentication is rejected.
- [ ] The pilot does not expose a listener beyond loopback.
- [ ] No resolved credentials appear in logs, shell history, configuration, or retained evidence.

**Human gate:** approve before contacting any provider API.

### Gate 3 — read-only provider catalog validation

Use only small, read-only model/catalog requests against explicitly approved provider accounts.

- [ ] Each configured provider/model identifier is listed or rejected with a sanitized explanation.
- [ ] Catalog results are recorded as availability only, not as quota/price guarantees.
- [ ] No inference request is issued at this gate.

**Human gate:** approve before each class of inference request.

### Gate 4 — controlled inference and client behavior

Use synthetic, non-confidential prompts with minimal output limits.

- [ ] One local-model request succeeds.
- [ ] One authorized cloud request succeeds.
- [ ] A streamed response works.
- [ ] A client-executed function-tool round trip works.
- [ ] The configured alias remains stable across a normal request.
- [ ] The evidence contains no prompt, response, key, or private path.

### Gate 5 — controlled fallback and cooldown behavior

Do not manufacture upstream failures against a real provider. Prefer the existing localhost fake-endpoint router test. If a provider-limit test is later approved, make it bounded and record only sanitized status/route evidence.

- [ ] Localhost lifecycle test remains green in the exact candidate environment.
- [ ] Any approved real-provider fallback test shows the selected deployment, fallback outcome, and cooldown bucket without provider payloads or credentials.
- [ ] A subsequent request skips a cooling-down deployment when the evidence proves a cooldown was recorded.

### Gate 6 — rollback and teardown

- [ ] Stop only the disposable pilot process.
- [ ] Confirm the primary Hermes provider and existing LiteLLM processes/configuration remain unchanged.
- [ ] Remove the disposable environment, state, and raw logs.
- [ ] Retain only a sanitized result summary and exact candidate identifier.

## Evidence and decision record

Record a compact sanitized result under `docs/` or `reviews/` only after review. Do not retain raw command transcripts, environment dumps, request logs, prompts, responses, provider request IDs, or credentials.

The result must state one of:

- **validated for continued isolated testing** — all approved gates passed; not a production approval;
- **blocked** — name the failed gate and sanitized error class;
- **reverted** — pilot torn down and the primary path confirmed unchanged.

## Promotion criteria

A future stable release may replace this candidate only after it passes the complete matrix and this pilot. A production/local-service proposal additionally requires a separate reviewed design covering authentication preservation, loopback binding, credential injection, monitoring, rollback, and release pin maintenance.
