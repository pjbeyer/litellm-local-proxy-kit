# LiteLLM Compatibility Matrix

This matrix was run in disposable virtual environments. It did not use provider credentials, change a running proxy, modify a service, or send provider inference requests.

| Candidate | Installation source | Offline tests | Router preflight | Result |
| --- | --- | --- | --- | --- |
| `1.100.1` | PyPI | 36 passed; `pip check` passed | Failed: `classification_mode` and `enable_context_window_escalation` are not in the complexity-router schema | Not compatible with this source kit |
| `v1.102.0-rc.1` / `1.102.0` metadata | LiteLLM release-candidate commit `16f6d924530c1cf2dc94b3e83c86a4632aefb7c0` | 36 passed; `pip check` passed | Passed: callback API/config load; one localhost `429`; one ordered fallback; one deployment failure-hook event; expected wire output cap | RC-compatible candidate only |
| `1.101.0` | PyPI stable package | 37 passed; `pip check` passed | Passed: callback API/config load; one localhost `429`; one ordered fallback; one deployment failure-hook event; expected wire output cap; synthetic two-phase tool transport | Stable compatibility candidate; C10 remains blocked because explicit `stream: false` is still omitted downstream |

## Candidate pins

The first passing validation artifact was `v1.102.0-rc.1` commit `16f6d924530c1cf2dc94b3e83c86a4632aefb7c0`, retained in `requirements-candidate.txt` and `requirements-pilot.txt` as historic RC-validation material.

LiteLLM `1.101.0` is now the tested stable compatibility candidate. `requirements-stable-candidate.txt` records its disposable validation dependency set; see [stable candidate validation](stable-candidate-validation.md). It remains an isolated-validation artifact, not a production or migration recommendation.

## Promotion gate

Before using this configuration beyond isolated validation:

1. Re-run the full matrix against the exact stable candidate artifact whenever its dependency set changes.
2. Resolve the local function-tool contract before representing a local route as tool capable; the stable candidate still omits explicit downstream `stream: false`.
3. Verify real provider catalogs and authorized end-to-end requests in a separately approved environment.
4. Write and independently review a non-destructive deployment guide that preserves existing authentication and rollback.

## Upstream references

- Deployment-attempt failure hook: <https://github.com/BerriAI/litellm/pull/36657>
- User-turn classification: <https://github.com/BerriAI/litellm/pull/38773>
- Context-window escalation: <https://github.com/BerriAI/litellm/pull/38844>
- Candidate tag: <https://github.com/BerriAI/litellm/releases/tag/v1.102.0-rc.1>
