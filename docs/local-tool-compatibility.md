# Local Function-Tool Compatibility — RC Pilot Finding

## Status

**Blocked for tool-capable proxy use.** The isolated LiteLLM RC candidate can transport OpenAI-compatible tool declarations and tool-call responses, but the tested local oMLX/Gemma route does not honor a forced tool choice when the downstream request omits an explicit `stream: false` field. The current LiteLLM proxy path omits that false-valued field.

This is a local route compatibility finding, not evidence that LiteLLM generally discards tools.

## Isolated evidence

All tests used loopback-only endpoints, synthetic prompts/tools, a disposable proxy authentication value, and no provider credentials. No raw payloads, responses, logs, configuration, or state were retained.

| Path | Result |
| --- | --- |
| Direct oMLX `gemma-4`, explicit `stream: false`, forced `pilot_echo` | Returned exactly one tool call. |
| Direct oMLX, client-executed synthetic tool result | Follow-up returned no final text; full two-phase round trip did not pass. |
| RC proxy to oMLX, same forced tool request | Returned ordinary text with no tool call. |
| RC proxy to a loopback synthetic backend | Forwarded tools and the forced tool choice; preserved the backend tool-call response. |
| Direct oMLX, only `stream` omitted | Returned ordinary text with no tool call, matching the proxy behavior. |
| Direct vs. proxied request-shape comparison using the synthetic backend | Model, message roles/count, tool declaration, and forced choice matched. The proxy omitted `stream: false`. |

## Determination

The actual local server’s forced-tool behavior is sensitive to whether false-valued `stream` is explicit. LiteLLM's current OpenAI-compatible proxy path did not forward that explicit false value. Because the resulting phase-A behavior differs, this kit must fail closed: `custom_callbacks.py` rejects function-tool requests after local deployment selection, protecting both direct `local` calls and `auto-free` calls classified local. Do not represent the local route as compatible with forced function tools and do not activate a tool-capable local proxy from this candidate.

The direct phase-B failure is a separate local-model output behavior. It does not weaken the phase-A transport finding, but it also prevents claiming a complete client-executed tool round trip.

## Required resolution before tool use

1. Identify a stable LiteLLM/oMLX combination that preserves the required explicit control, or an endpoint-compatible configuration that proves the same phase-A behavior.
2. Repeat the two-phase synthetic contract through the proxy: exactly one forced zero-argument call in phase A, one client-side synthetic result, and non-empty tool-free final text in phase B.
3. Add an automated loopback regression before any deployment or public runnable-release claim.
4. Treat provider/tool routing as unsupported until all of these gates pass.
