# Source-Kit Offline Test Results

Imported as historical evidence only. These results cover offline custom-code tests with LiteLLM explicitly stubbed; they do not prove current SDK, provider, LaunchAgent, or macOS integration behavior.

```text
Offline custom-code unit tests; LiteLLM is explicitly stubbed.
Not a live SDK, provider, or Mac integration result.
YAML parsing, Python AST and shell syntax/env expansion also passed.

test_auth_scope_skips_all_gemini_models (test_callbacks.PolicyTests.test_auth_scope_skips_all_gemini_models) ... ok
test_chat_only_contract (test_callbacks.PolicyTests.test_chat_only_contract) ... ok
test_cloudflare_no_forced_reasoning_param (test_callbacks.PolicyTests.test_cloudflare_no_forced_reasoning_param) ... ok
test_complex_medium_effort (test_callbacks.PolicyTests.test_complex_medium_effort) ... ok
test_explicit_effort_wins (test_callbacks.PolicyTests.test_explicit_effort_wins) ... ok
test_failure_hook_records_first_failed_attempt (test_callbacks.PolicyTests.test_failure_hook_records_first_failed_attempt) ... auto-free cooldown bucket=openrouter seconds=120 reason=rate_limit
ok
test_global_normalization (test_callbacks.PolicyTests.test_global_normalization) ... ok
test_groq_large_request_skipped_not_truncated (test_callbacks.PolicyTests.test_groq_large_request_skipped_not_truncated) ... ok
test_groq_small_request_caps_output (test_callbacks.PolicyTests.test_groq_small_request_caps_output) ... ok
test_local_preserves_chat_template_options (test_callbacks.PolicyTests.test_local_preserves_chat_template_options) ... ok
test_local_removes_invented_thinking (test_callbacks.PolicyTests.test_local_removes_invented_thinking) ... ok
test_no_builtin_paid_tools (test_callbacks.PolicyTests.test_no_builtin_paid_tools) ... ok
test_no_client_route_override (test_callbacks.PolicyTests.test_no_client_route_override) ... ok
test_no_shared_mutation_or_history_edit (test_callbacks.PolicyTests.test_no_shared_mutation_or_history_edit) ... ok
test_openrouter_zero_price_policy (test_callbacks.PolicyTests.test_openrouter_zero_price_policy) ... ok
test_persisted_cooldown_skips_attempt (test_callbacks.PolicyTests.test_persisted_cooldown_skips_attempt) ... ok
test_reasoning_high_effort (test_callbacks.PolicyTests.test_reasoning_high_effort) ... ok
test_request_alias_only (test_callbacks.PolicyTests.test_request_alias_only) ... ok
test_small_explicit_output_respected (test_callbacks.PolicyTests.test_small_explicit_output_respected) ... ok
test_synthetic_skip_does_not_extend_cooldown (test_callbacks.PolicyTests.test_synthetic_skip_does_not_extend_cooldown) ... ok
test_unknown_deployment_rejected (test_callbacks.PolicyTests.test_unknown_deployment_rejected) ... ok
test_cloudflare_daily_reset_uses_utc (test_callbacks.StateTests.test_cloudflare_daily_reset_uses_utc) ... ok
test_dst_reset_is_not_fixed_24h (test_callbacks.StateTests.test_dst_reset_is_not_fixed_24h) ... ok
test_duration_units (test_callbacks.StateTests.test_duration_units) ... ok
test_google_daily_reset_uses_pacific (test_callbacks.StateTests.test_google_daily_reset_uses_pacific) ... ok
test_groq_minute_does_not_use_daily_header (test_callbacks.StateTests.test_groq_minute_does_not_use_daily_header) ... ok
test_millisecond_duration (test_callbacks.StateTests.test_millisecond_duration) ... ok
test_openrouter_unknown_daily_reset_reprobes (test_callbacks.StateTests.test_openrouter_unknown_daily_reset_reprobes) ... ok
test_ordinary_bad_request_not_global_cooldown (test_callbacks.StateTests.test_ordinary_bad_request_not_global_cooldown) ... ok
test_reject_zero_tokens (test_callbacks.StateTests.test_reject_zero_tokens) ... ok
test_retry_after_http_date (test_callbacks.StateTests.test_retry_after_http_date) ... ok
test_store_persists_without_secrets (test_callbacks.StateTests.test_store_persists_without_secrets) ... ok
test_stricter_output_limit_wins (test_callbacks.StateTests.test_stricter_output_limit_wins) ... ok

----------------------------------------------------------------------
Ran 33 tests in 0.058s

OK
```
