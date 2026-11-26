# Original routing experiment

This is historical context for the imported callback and preflight, not a managed deployment recipe or a list of current provider entitlements. See [the managed service guide](macos-managed-service.md) for installation and [compatibility evidence](compatibility.md) for version-specific observations.

- The experiment routed SIMPLE/MEDIUM to a local endpoint and COMPLEX/REASONING to an ordered `cloud-free` group. Lower order won; cooldown made unavailable deployments temporarily ineligible. There was no local-to-cloud fallback or silent cloud-to-local downgrade.
- Provider adjustments ran after deployment selection. Output caps were not context-size limits; the callback preserved smaller explicit client caps. Tool and full-message transport depended on the selected endpoint. See [local tool compatibility](local-tool-compatibility.md) before assuming forced tool calls work.
- The imported route was scoped to OpenAI Chat Completions. Native Responses and Anthropic protocol support need separate evidence. A changed base URL is not protocol translation.
- The persistent quota circuit breaker remembered provider cooldowns, honored available retry hints, and did not claim exact daily budget accounting. It did not make streamed retries transparent once output reached the caller.
- Model IDs, quotas, billing, and retention terms can change. Verify them with the provider before using the route. Zero-price routing controls do not make providers private or guarantee a monetary ceiling. Send only data authorized for every eligible provider.

With the declared LiteLLM runtime, `python preflight.py --router-test` exercises localhost routing without provider credentials. Run it in an isolated environment. It is neither managed candidate rehearsal nor live provider acceptance.
