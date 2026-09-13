"""Offline unit tests. Explicit LiteLLM stub: NOT a live SDK integration test."""
import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

stub = types.ModuleType("litellm")
class FakeError(Exception):
    def __init__(self, message="", llm_provider=None, model=None):
        super().__init__(message)
        self.message = message
        self.response = types.SimpleNamespace(headers={})
        self.status_code = 429
class FakeBadRequest(FakeError):
    pass
stub.RateLimitError = FakeError
stub.BadRequestError = FakeBadRequest
stub.token_counter = lambda **kwargs: 100
custom = types.ModuleType("litellm.integrations.custom_logger")
class CustomLogger:
    async def async_pre_call_deployment_hook(self, kwargs, call_type): pass
    async def async_post_call_failure_deployment_hook(self, request_data, exception, call_type, fallback_depth=None): pass
custom.CustomLogger = CustomLogger
sys.modules["litellm"] = stub
sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
sys.modules["litellm.integrations.custom_logger"] = custom
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import custom_callbacks as cb

NOW = datetime(2026, 9, 7, 21, 18, tzinfo=timezone.utc).timestamp()
def upstream(status, body, headers=None):
    error = Exception("upstream failure")
    error.status_code = status
    error.body = body
    error.response = types.SimpleNamespace(status_code=status, headers=headers or {})
    return error

class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = cb.BlockStore(Path(self.tmp.name) / "state.sqlite3")
        self.hook = cb.ProxyModifierHook(self.store)
        stub.token_counter = lambda **kwargs: 100
    def tearDown(self): self.tmp.cleanup()
    async def call(self, model, **kwargs):
        return await self.hook.async_pre_call_deployment_hook({"model":model, "messages":[{"role":"user", "content":"task"}], **kwargs})
    async def test_local_removes_invented_thinking(self):
        r = await self.call("openai/" + cb.LOCAL_MODEL, max_tokens=131072, reasoning_effort="high", extra_body={"thinking_budget":5000})
        self.assertEqual(r["max_tokens"], 8192)
        self.assertNotIn("reasoning_effort", r)
        self.assertNotIn("thinking_budget", r["extra_body"])
    async def test_local_preserves_chat_template_options(self):
        r = await self.call(cb.LOCAL_MODEL, extra_body={"chat_template_kwargs":{"enable_thinking":False}})
        self.assertFalse(r["extra_body"]["chat_template_kwargs"]["enable_thinking"])
    async def test_local_function_tools_rejected_until_contract_is_verified(self):
        with self.assertRaises(FakeBadRequest) as ctx:
            await self.call(cb.LOCAL_MODEL, tools=[{"type":"function","function":{"name":"pilot_echo"}}])
        self.assertIn("function tools are disabled", str(ctx.exception))
    async def test_openrouter_zero_price_policy(self):
        r = await self.call("openrouter/openrouter/free", temperature=.3, extra_body={"provider":{"max_price":{"prompt":10}}, "thinking_budget":55})
        self.assertEqual(r["extra_body"]["provider"]["max_price"], {"prompt":0,"completion":0,"request":0})
        self.assertEqual(r["extra_body"]["reasoning"], {"effort":"high"})
        self.assertNotIn("temperature", r)
    async def test_complex_medium_effort(self):
        r = await self.call("gemini/gemini-3.8-flash", metadata={"routing_decision":{"tier":"COMPLEX"}})
        self.assertEqual(r["reasoning_effort"], "medium")
    async def test_reasoning_high_effort(self):
        r = await self.call("gemini/gemini-3.8-flash", litellm_metadata={"routing_decision":{"tier":"REASONING"}})
        self.assertEqual(r["reasoning_effort"], "high")
    async def test_explicit_effort_wins(self):
        r = await self.call("gemini/gemini-3.8-flash", reasoning_effort="low")
        self.assertEqual(r["reasoning_effort"], "low")
    async def test_no_shared_mutation_or_history_edit(self):
        data = {"model":"openrouter/openrouter/free", "messages":[{"role":"assistant", "content":"x", "tool_calls":[{"id":"call1"}]}], "extra_body":{"thinking_budget":20}}
        before = copy.deepcopy(data)
        await self.hook.async_pre_call_deployment_hook(data)
        self.assertEqual(data, before)
    async def test_groq_small_request_caps_output(self):
        r = await self.call("groq/openai/gpt-oss-120b", max_tokens=16384)
        self.assertEqual(r["max_tokens"], 4096)
    async def test_groq_large_request_skipped_not_truncated(self):
        stub.token_counter = lambda **kwargs: 7000
        with self.assertRaises(FakeError) as ctx:
            await self.call("groq/openai/gpt-oss-120b")
        self.assertTrue(ctx.exception._auto_free_local_rejection)
    async def test_cloudflare_no_forced_reasoning_param(self):
        r = await self.call("openai/@cf/openai/gpt-oss-120b", reasoning_effort="high")
        self.assertNotIn("reasoning_effort",r)
    async def test_unknown_deployment_rejected(self):
        with self.assertRaises(FakeBadRequest): await self.call("openai/paid-model")
    async def test_request_alias_only(self):
        with self.assertRaises(FakeBadRequest):
            await self.hook.async_pre_call_hook(None,None,{"model":"openai/paid-model", "messages":[]},"completion")
    async def test_no_client_route_override(self):
        with self.assertRaises(FakeBadRequest):
            await self.hook.async_pre_call_hook(None,None,{"model":"auto-free", "messages":[], "extra_body":{"models":["paid"]}},"completion")
    async def test_no_builtin_paid_tools(self):
        with self.assertRaises(FakeBadRequest):
            await self.hook.async_pre_call_hook(None,None,{"model":"auto-free","messages":[],"tools":[{"type":"web_search"}]},"completion")
    async def test_chat_only_contract(self):
        with self.assertRaises(FakeBadRequest):
            await self.hook.async_pre_call_hook(None,None,{"model":"auto-free","input":"hello"},"responses")
    async def test_small_explicit_output_respected(self):
        r = await self.call("gemini/gemini-3.8-flash",max_completion_tokens=2048)
        self.assertEqual(r["max_tokens"],2048)
    async def test_global_normalization(self):
        r = await self.hook.async_pre_call_hook(None,None,{"model":"auto-free","messages":[],"max_completion_tokens":90000},"completion")
        self.assertEqual(r["max_tokens"],16384)
        self.assertEqual(r["num_retries"],0)
    async def test_persisted_cooldown_skips_attempt(self):
        self.store.block("gemini:gemini-3.8-flash", cb.time.time()+300,"rate_limit")
        with self.assertRaises(FakeError): await self.call("gemini/gemini-3.8-flash")
        # Another model can still run.
        await self.call("gemini/gemini-3.7-flash")
    async def test_auth_scope_skips_all_gemini_models(self):
        self.store.block("gemini:auth",cb.time.time()+300,"authentication")
        with self.assertRaises(FakeError): await self.call("gemini/gemini-3.7-flash")
    async def test_failure_hook_records_first_failed_attempt(self):
        await self.hook.async_post_call_failure_deployment_hook({"model":"openrouter/openrouter/free"}, upstream(429,{"error":"Too many requests"},{"Retry-After":"120"}))
        self.assertGreater(self.store.remaining(["openrouter"],cb.time.time()),110)
    async def test_synthetic_skip_does_not_extend_cooldown(self):
        error = cb.synthetic_limit("skip","openrouter","openrouter/free")
        await self.hook.async_post_call_failure_deployment_hook({"model":"openrouter/openrouter/free"},error)
        self.assertEqual(self.store.remaining(["openrouter"],cb.time.time()),0)

class StateTests(unittest.TestCase):
    def test_google_daily_reset_uses_pacific(self):
        b = cb.failure_block("gemini","gemini-3.8-flash",upstream(429,{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier","retryDelay":"10s"}),NOW)
        reset = datetime.fromtimestamp(NOW+b[1], timezone.utc)
        self.assertEqual((reset.day,reset.hour,reset.minute),(8,7,0))
    def test_cloudflare_daily_reset_uses_utc(self):
        b=cb.failure_block("cloudflare","m",upstream(400,{"error":"daily neuron limit"}),NOW)
        self.assertEqual(datetime.fromtimestamp(NOW+b[1],timezone.utc).hour,0)
    def test_openrouter_unknown_daily_reset_reprobes(self):
        b=cb.failure_block("openrouter","m",upstream(429,{"error":"free-models-per-day"}),NOW)
        self.assertEqual(b[1],900)
    def test_retry_after_http_date(self):
        self.assertAlmostEqual(cb.duration("Mon, 07 Sep 2026 21:20:00 GMT",NOW),120)
    def test_duration_units(self): self.assertAlmostEqual(cb.duration("2m59.56s",NOW),179.56)
    def test_millisecond_duration(self): self.assertEqual(cb.duration("500ms",NOW),.5)
    def test_ordinary_bad_request_not_global_cooldown(self):
        self.assertIsNone(cb.failure_block("gemini","m",upstream(400,{"error":"Invalid tool schema"}),NOW))
    def test_groq_minute_does_not_use_daily_header(self):
        b=cb.failure_block("groq","m",upstream(429,{}, {"x-ratelimit-reset-requests":"23h", "x-ratelimit-reset-tokens":"4s"}),NOW)
        self.assertEqual(b[1],4)
    def test_store_persists_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"state.sqlite3"
            cb.BlockStore(p).block("openrouter",NOW+90,"rate_limit")
            self.assertEqual(cb.BlockStore(p).remaining(["openrouter"],NOW),90)
            self.assertEqual(cb.BlockStore(p).remaining(["openrouter"],NOW+91),0)
    def test_dst_reset_is_not_fixed_24h(self):
        midnight = datetime(2026,11,1,0,0,tzinfo=cb.ZoneInfo("America/Los_Angeles")).timestamp()
        self.assertEqual(cb.seconds_to_midnight("America/Los_Angeles",midnight),25*3600+5)
    def test_stricter_output_limit_wins(self):
        self.assertEqual(cb.output_limit({"max_tokens":8192,"max_completion_tokens":2048},16384),2048)
    def test_reject_zero_tokens(self):
        with self.assertRaises(ValueError): cb.output_limit({"max_tokens":0},8192)

if __name__ == "__main__": unittest.main(verbosity=2)
