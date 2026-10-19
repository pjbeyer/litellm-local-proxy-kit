# Stable LiteLLM Candidate Validation — C10

**Date:** 2026-09-15  
**Candidate:** LiteLLM `1.101.0` from PyPI  
**Decision:** **stable compatibility candidate only; C10 remains blocked.**

## Why this candidate was tested

LiteLLM `1.101.0` was the current stable PyPI release when this validation ran.[1] Its tagged source contains the imported kit's required per-deployment failure hook, `classification_mode`, and `enable_context_window_escalation` compatibility fields, which derive from the corresponding upstream changes.[4][5][6]

It is not a descendant of the previously tested RC commit `16f6d924530c1cf2dc94b3e83c86a4632aefb7c0`: GitHub reports the two histories as diverged. The stable tag is therefore an independently tested artifact, not a release-date-based promotion of the RC result.[2][3]

## Scope and containment

- Created a disposable virtual environment and installed `litellm[proxy]==1.101.0`, `prisma==0.15.0`, and PyYAML.
- Used only ephemeral loopback servers and disposable authentication values.
- Did not request oMLX inference, change oMLX model residency, contact any provider, inspect/use provider credentials, alter a running proxy, or change services or persistent configuration.
- The synthetic upstream retained only allowlisted request-shape facts. It did not retain request bodies, prompts, tool arguments, response bodies, credentials, or logs.

## Results

### Imported-kit compatibility matrix — passed

- `pip check` passed.
- All 37 offline tests passed.
- `preflight.py --router-test` passed against two loopback endpoints: one synthetic `429`, one ordered fallback, one deployment-failure-hook event, and the expected output-cap rewrite.
- The exact `1.101.0` environment accepted the callback API, complexity-router schema, and imported Router configuration.

### Synthetic two-phase tool transport — passed, with the existing limitation

A disposable authenticated proxy and synthetic OpenAI-compatible upstream completed the two-phase client-side tool pattern:

1. phase A preserved the declared function tool and returned one synthetic tool call;
2. phase B preserved the assistant tool-call history and client tool result and returned the synthetic final text.

The upstream shape probe recorded `stream_present=false` for **both** proxy requests, despite each client request explicitly supplying `stream: false`. The stable candidate therefore retains the C10 transport limitation observed in the RC candidate. An upstream report separately demonstrates that false-stream request conversion/forwarding is an active proxy concern, but it is not evidence that this omission causes the gpt-oss phase-B behavior.[7]

## Determination

`1.101.0` is the first validated **stable** candidate for the imported kit's callback/router lifecycle. It is not a tool-capable local-route recommendation:

- the proxy still drops explicit false-valued `stream` on the tested OpenAI-compatible path;
- generic synthetic phase-B success does not prove compatibility with the local gpt-oss endpoint;
- the configured local Gemma deployment remains unvalidated for the full client-executed round trip;
- the local function-tool rejection guard remains required.

Do not replace a running proxy, change `requirements-pilot.txt`, remove the guard, or describe a local route as tool-capable based on this result. A future candidate must first change the loopback request-shape result, then pass the exact two-phase contract against the selected local endpoint under a separately approved live-test gate.

## Sources

[1] https://pypi.org/project/litellm/1.101.0 — LiteLLM 1.101.0 on PyPI
[2] https://github.com/BerriAI/litellm/releases/tag/v1.101.0 — LiteLLM v1.101.0 release
[3] https://github.com/BerriAI/litellm/compare/16f6d924530c1cf2dc94b3e83c86a4632aefb7c0...v1.101.0 — Comparison of tested RC commit and v1.101.0
[4] https://github.com/BerriAI/litellm/pull/36657 — Per-deployment failure hook pull request
[5] https://github.com/BerriAI/litellm/pull/38773 — user_turn complexity routing pull request
[6] https://github.com/BerriAI/litellm/pull/38844 — Context-window escalation pull request
[7] https://github.com/BerriAI/litellm/issues/40068 — Proxy false-stream conversion bug report
