# LiteLLM Local Proxy Kit

An experimental, OSS-oriented configuration and methodology kit for operating a local LiteLLM proxy on Apple Silicon.

It routes SIMPLE/MEDIUM work to a local OpenAI-compatible model endpoint and allows COMPLEX/REASONING work to use an ordered, explicitly configured cloud-free group. It includes request-policy callbacks, a persistent quota circuit breaker, offline tests, and a preflight that validates the installed LiteLLM API before activation.

## Scope and safety

- This is an **experiment-derived reference**, not a claim that every listed provider/model or free-tier allocation is currently available.
- Provider plans, model IDs, limits, retention terms, and LiteLLM hooks can change. Run `python preflight.py --router-test` with the exact Python environment that launches LiteLLM before activation.
- Credentials are only environment references; do not put resolved keys in `proxy_config.yaml`, logs, or commits.
- The proxy should bind to loopback and retain authentication. Do not expose an unauthenticated proxy to a LAN.
- The original test-source logs, backup snapshots, personal Hermes configuration, and LaunchAgent plist are intentionally excluded from this repository.

---

Prepared from an Apple Silicon local oMLX + free-cloud experiment, 2026-09-07.

## Behavior

- `auto-free` is a real complexity-router deployment, not an alias.
- SIMPLE / MEDIUM -> the existing local oMLX model.
- COMPLEX / REASONING -> one ordered `cloud-free` model group:
  1. Gemini 3.8 Flash
  2. Gemini 3.7 Flash
  3. Gemini 3.6 Flash
  4. Gemini 3.5 Flash
  5. OpenRouter `openrouter/free`
  6. Cloudflare GPT-OSS 120B
  7. Groq GPT-OSS 120B, for sufficiently small prompts
- Lower `order` wins. On exhaustion/failure, current LiteLLM tries the next order. This is NOT weighted random routing across providers. A higher-priority model becomes eligible again when its cooldown expires; this is a priority policy, not a permanently moving round-robin pointer.
- No local-to-cloud fallback and no cloud-to-local downgrade. Cloud capacity exhaustion is an error; the agent should pause and retry later.
- No additional classifier, embeddings, automatic web-search, or other paid hosted tools.
- **Scope: OpenAI Chat Completions (`/v1/chat/completions`) with full conversation history.** The early hook intentionally rejects native Responses requests. Configure agents for Chat Completions, or separately validate a Responses/Anthropic translation layer. Do not assume changing the URL makes native Codex/Claude protocols interchangeable.

## What changed from the supplied files

1. Provider rewrites moved to `async_pre_call_deployment_hook`: the old `async_pre_call_hook` runs BEFORE deployment selection.
2. `max_tokens` is an OUTPUT allowance, not the context window. Local output is capped at 8,192; normal cloud output at 16,384; Groq at 4,096. Smaller explicit client caps are honored. Larger client caps are intentionally clamped. Reasoning can consume output allowance; increase caps only after testing quota and context fit.
3. Removed the invented `thinking_budget` injection. This kit makes no claim about the private local model's thinking abilities; use only parameters your oMLX model actually supports.
4. Removed broad SIMPLE/MEDIUM keyword overrides: "explain" or "hi" must not force a difficult task into a cheap tier. Retained selected high-tier escalation terms.
5. Disabled session/deployment affinity. Classify user turns rather than pinning the entire session to its first-turn model; the current router carries decisions through continuation turns.
6. Removed cache-control injection, global silent parameter dropping, and forced cloud sampling values. Tools and full messages are preserved where the selected endpoint honors the forwarded contract; the current RC/oMLX local route has a documented forced-tool incompatibility in [docs/local-tool-compatibility.md](docs/local-tool-compatibility.md). Provider incompatibilities produce visible errors, not silent removal of tools.
7. Replaced the class-shadowing callback object with `custom_callbacks.proxy_modifier`.
8. Used Cloudflare's OpenAI-compatible `/ai/v1` endpoint. Your supplied plist lacks the Cloudflare ACCOUNT ID; the API key alone is insufficient.

## Free-tier assumptions and limits

In the source experiment, the Gemini dashboard displayed each of 3.8, 3.7, 3.6 and 3.5 Flash with limits of 5 RPM, 250K input TPM and 20 RPD. Treat those values as historical observations, not current entitlement or availability. Dashboard usage figures can describe historical peaks rather than remaining quota. Exact API model IDs must still be checked with `preflight.py --catalogs`.

This kit intentionally excludes Flash-Lite / Gemma cloud entries (they overlap the local medium tier), all zero-quota Pro/agent/image/audio entries, NVIDIA trial-only endpoints, and billing-dependent providers. It does not promise all displayed model quotas are additive: shared project/provider constraints and upstream responses still control access.

OpenRouter documents 50 free requests/day before purchasing at least $10 in credits and 1,000/day afterward, with 20 RPM in either case. No config change is needed for the credit purchase: OpenRouter enforces the current entitlement. Calls remain on `openrouter/free`; the callback also constrains provider prompt/completion/request prices to zero. No fallback names paid models. The random free router can select weaker models: reasoning/tool-parameter requirements reduce incompatibility but do not guarantee model quality.

Groq's 8K TPM is a major constraint for coding agents. Its conservative 3,500-token input cap and callback input-plus-output check intentionally skip large histories. It is NOT a free 131K-prompt service. Token estimation is approximate. The kit never truncates history to squeeze it into Groq.

Cloudflare has a SHARED 10,000-Neuron/day allowance, not 10K tokens/day or a per-model allowance. On a Free plan it blocks exhausted usage. The configuration does not pretend to meter Neurons locally.

**Important: Gemini, Groq, and Cloudflare model IDs also work on PAID accounts. No YAML can make those IDs intrinsically free.** This kit relies on your stated unbilled/free account status. If you later enable billing or attach another project/account, stop and revise the policy; do not assume these callback caps are a dollar budget. Separate free-only accounts/keys and provider-side restrictions are stronger controls than a local convention. OpenRouter price limits are defense in depth, not protection against fees for features outside this chat/text scope.

## Quota handling

- Native LiteLLM RPM/TPM/context checks and provider responses are authoritative; zero automatic same-deployment retries.
- Per-attempt failure hooks remember 429 cooldowns in `~/.litellm/auto-free-state.sqlite3` and immediately make subsequent attempts skip that provider/model.
- Honors `Retry-After` (seconds or HTTP date), Google's structured retry delay, and Groq token-reset headers where available.
- Explicit Gemini daily exhaustion -> next midnight America/Los_Angeles, including DST.
- Explicit Cloudflare daily exhaustion -> next midnight UTC.
- Unknown daily resets, including unqualified OpenRouter/Groq daily errors -> 15-minute recheck, unless the response supplies a reset duration. Unknown ordinary 429 -> 60-second recheck. It does not invent a universal midnight reset.
- 401 -> provider-wide one-hour authentication cooldown. 402/403/404 -> one-hour model/access cooldown. Other bad requests are not treated as quota exhaustion.
- This is a reactive circuit breaker, not an exact persistent RPD/token counter. Parallel in-flight requests can still receive 429s. Other applications' usage is not visible until the provider reports limits. SQLite stores only bucket, resume time and reason code; this callback does not log prompts, responses, keys or error bodies.
- Once streaming output has reached the client, do NOT assume transparent cross-provider retry is possible. Surface interruption; avoid blindly rerunning tool actions. This is not an exactly-once execution system.

## Validation boundary

The original imported routing experiment is **not an activation or migration procedure**. The separately specified shared service core (`core/`, `launchd/`, spec 015) now has a [managed macOS service guide](docs/macos-managed-service.md), covering manifest installation, runtime environment parity, operator reload, real authentication/inference gates and rollback. Workstation credentials and resolved plists remain excluded; deployment source is separate.

The isolation and offline-test instructions below apply to the original routing experiment, not a production takeover.

1. Work only in an isolated copy of this repository; keep any running proxy configuration, authentication settings, startup scripts, and credentials unchanged.
2. Run the complete offline suite from the repository root using an isolated
   test environment (includes the shared installer and installed-layout tests):

```sh
uv venv .venv-tests
uv pip install --python .venv-tests/bin/python pytest pyyaml
.venv-tests/bin/python -m pytest -q tests
```

`unittest discover` alone does not collect the pytest-style shared-core and
installer regressions; it is not the complete suite.

3. Use the same Python environment that starts LiteLLM to run the non-provider compatibility and localhost routing check:

```sh
python preflight.py --router-test
```

This starts only ephemeral localhost test endpoints; it does not require provider credentials or consume provider quota. A non-zero exit is a stop signal—do not activate this kit. The currently observed LiteLLM compatibility status is documented in [docs/compatibility.md](docs/compatibility.md).

Any future deployment guide must be separately specified and reviewed. It must merge (never overwrite) existing proxy authentication, resolve credentials at runtime, use a real user-supplied path where launchd requires one, retain loopback/authentication protections, and demonstrate rollback before service activation.

## Recovery / rollback

This import does not activate or modify a proxy. Discard the isolated working copy rather than changing a running proxy. A future deployment guide must preserve a tested rollback configuration and must not change authentication, binding, or credential injection without a separately reviewed recovery procedure.

Free-provider terms and retention policies can be unsuitable for confidential code. This kit does not make third-party providers private; use only data you are authorized to send to every eligible fallback.

## Validation performed during preparation

- Python compilation; YAML parsing and structural consistency.
- 34 offline callback unit tests (33 source-derived plus one local tool-contract regression) plus 3 preflight command-mode regression tests for rewriting, output caps, fail-closed policy, quota persistence, retry headers, day resets/DST, no input mutation, and credential-free router-test mode.
- The unit tests deliberately stub LiteLLM: they validate this code's behavior, not the installed Router.
- The initial RC validation established the callback/router contract. A later disposable LiteLLM `1.101.0` stable-candidate environment passed all 37 offline tests, `pip check`, the localhost Router lifecycle, and a synthetic two-phase tool transport probe. It still omitted explicit downstream `stream: false`, so the local function-tool guard remains in place; see [stable candidate validation](docs/stable-candidate-validation.md) and [local-tool compatibility](docs/local-tool-compatibility.md).
- Live provider inference and authenticated catalog checks were not run. No provider credentials, account configuration, existing proxy configuration, startup service, or running proxy was modified.

## Sources

- LiteLLM auto routing: https://docs.litellm.ai/docs/proxy/auto_routing
- Deployment hooks: https://docs.litellm.ai/docs/observability/custom_callback
- Ordered deployments / retries: https://docs.litellm.ai/docs/routing
- Gemini adapter: https://docs.litellm.ai/docs/providers/gemini
- OpenRouter free limits: https://openrouter.ai/docs/api/reference/limits
- OpenRouter zero-price ceilings: https://openrouter.ai/blog/tutorials/how-to-get-the-lowest-cost-llm-inference-on-openrouter/
- OpenRouter reasoning: https://openrouter.ai/docs/guides/reasoning-tokens
- Gemini resets: https://ai.google.dev/gemini-api/docs/rate-limits.md.txt
- Cloudflare compatibility: https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/
- Cloudflare model formats: https://developers.cloudflare.com/ai/models/%40cf/openai/gpt-oss-120b
- Cloudflare allowance: https://developers.cloudflare.com/workers-ai/platform/pricing/
- Groq limits: https://console.groq.com/docs/rate-limits
