"""Per-deployment request policy and persistent quota circuit breaker.

No credentials, prompts, responses, or exception bodies are stored/logged here.
SQLite stores only a provider/model bucket, resume timestamp, and reason code.
Provider responses remain authoritative: this is NOT an accounting/billing system.
Requires LiteLLM's CURRENT per-attempt deployment hooks. See preflight.py.
"""
from __future__ import annotations

import asyncio
import copy
import email.utils
import json
import logging
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import litellm
from litellm.integrations.custom_logger import CustomLogger

for _hook in ("async_pre_call_deployment_hook", "async_post_call_failure_deployment_hook"):
    if not hasattr(CustomLogger, _hook):
        raise RuntimeError("Installed LiteLLM lacks " + _hook + "; run preflight.py before activating this kit")

LOG = logging.getLogger("auto_free")
LOCAL_MODEL = "gemma-4-12b-coder-fable5-composer2.5-4bit"
GEMINI_MODELS = frozenset(f"gemini-3.{n}-flash" for n in (5, 6, 7, 8))
REASONING_KEYS = ("reasoning", "reasoning_effort", "thinking", "thinking_budget", "thinking_config", "thinking_level", "include_thoughts")


def identify(data):
    """Accept raw and provider-prefixed forms; never identify by client alias."""
    params = data.get("litellm_params") or {}
    m = str(data.get("model") or params.get("model") or "")
    p = str(data.get("custom_llm_provider") or params.get("custom_llm_provider") or "")
    base = str(data.get("api_base") or params.get("api_base") or "")
    if m in (LOCAL_MODEL, "openai/" + LOCAL_MODEL):
        return "local", LOCAL_MODEL
    if m in ("openrouter/openrouter/free", "openrouter/free"):
        return "openrouter", "openrouter/free"
    if m in ("openai/@cf/openai/gpt-oss-120b", "@cf/openai/gpt-oss-120b"):
        return "cloudflare", "@cf/openai/gpt-oss-120b"
    if m == "groq/openai/gpt-oss-120b" or (m == "openai/gpt-oss-120b" and (p == "groq" or "api.groq.com" in base)):
        return "groq", "openai/gpt-oss-120b"
    raw = m.removeprefix("gemini/")
    if raw in GEMINI_MODELS and (m.startswith("gemini/") or p == "gemini" or "generativelanguage.googleapis.com" in base):
        return "gemini", raw
    return None, m


def bucket(provider, model):
    # All OpenRouter free models share an allowance; all Workers AI share Neurons.
    return provider if provider in ("openrouter", "cloudflare") else provider + ":" + model


def metadata_dicts(data):
    params = data.get("litellm_params") or {}
    return [x for x in (data, data.get("metadata"), data.get("litellm_metadata"), params.get("metadata"), params.get("litellm_metadata")) if isinstance(x, dict)]


def effort_for(data):
    # A deliberate client preference wins. Use canonical values across providers.
    effort = data.get("reasoning_effort")
    if isinstance(effort, str):
        return {"none": "low", "minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}.get(effort, "high")
    for md in metadata_dicts(data):
        decision = md.get("routing_decision") or {}
        tier = decision.get("tier") if isinstance(decision, dict) else None
        tier = tier or md.get("complexity_tier")
        if tier == "REASONING":
            return "high"
        if tier == "COMPLEX":
            return "medium"
    # Router metadata shape varies by version. Never silently disable reasoning.
    return "high"


def output_limit(data, cap):
    values = [data[k] for k in ("max_tokens", "max_completion_tokens", "max_output_tokens") if data.get(k) is not None]
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("Output token limits must be positive integers")
    return min([cap] + values)


def synthetic_limit(message, provider, model, wait=60):
    # A real LiteLLM exception, so the Router can advance to the next order.
    error = litellm.RateLimitError(message=message, llm_provider=provider, model=model)
    error._auto_free_local_rejection = True
    response = getattr(error, "response", None)
    if response is not None:
        response.headers["retry-after"] = str(max(1, math.ceil(wait)))
    return error


class BlockStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("AUTO_FREE_STATE_DB", str(Path.home() / ".litellm/auto-free-state.sqlite3"))).expanduser()

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=2)
        db.execute("CREATE TABLE IF NOT EXISTS blocks (bucket TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT NOT NULL)")
        os.chmod(self.path, 0o600)
        return db

    def remaining(self, keys, now):
        db = self.connect()
        try:
            db.execute("DELETE FROM blocks WHERE until <= ?", (now,))
            rows = [db.execute("SELECT until FROM blocks WHERE bucket=?", (key,)).fetchone() for key in keys]
            db.commit()
        finally:
            db.close()
        return max([0.0] + [row[0] - now for row in rows if row])

    def block(self, key, until, reason):
        db = self.connect()
        try:
            db.execute("INSERT INTO blocks VALUES (?, ?, ?) ON CONFLICT(bucket) DO UPDATE SET until=MAX(blocks.until, excluded.until), reason=excluded.reason", (key, until, reason))
            db.commit()
        finally:
            db.close()


def seconds_to_midnight(zone, now):
    current = datetime.fromtimestamp(now, ZoneInfo(zone))
    tomorrow = (current + timedelta(days=1)).date()
    reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=ZoneInfo(zone))
    return reset.timestamp() - now + 5


def duration(value, now):
    if value is None:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    # Groq's reset headers may be durations, e.g. 2m59.56s.
    if re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|h|m|s))+", value):
        return sum(float(n) * {"h": 3600, "m": 60, "s": 1, "ms": .001}[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", value))
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


def failure_block(provider, model, exception, now):
    """Return (scope, wait_seconds, reason), or None. Never log exception text."""
    if getattr(exception, "_auto_free_local_rejection", False):
        return None
    response = getattr(exception, "response", None)
    status = getattr(exception, "status_code", None) or getattr(response, "status_code", None)
    headers = {str(k).lower(): v for k, v in (getattr(response, "headers", {}) or {}).items()}
    body = getattr(exception, "body", None)
    if body is None and response is not None:
        try:
            body = response.json()
        except Exception:
            pass
    # Inspect in-memory only; provider errors can contain excerpts of prompts.
    text = (json.dumps(body, default=str) if body is not None else str(exception)).lower()
    daily = any(s in text for s in ("requestsperday", "tokensperday", "requests_per_day", "tokens_per_day", "free-models-per-day", "daily quota", "daily limit", "daily token limit", "daily free allocation", "daily neuron"))
    is_limit = status == 429 or "ratelimit" in type(exception).__name__.lower() or (status in (400, 402) and daily)
    if is_limit:
        waits = [duration(headers.get("retry-after"), now)]
        waits += [duration(v, now) for v in re.findall(r'"retrydelay"\s*:\s*"([^\"]+)"', text)]
        if provider == "groq":
            # Request header is RPD; token header is TPM. Do not mix the two!
            header = "x-ratelimit-reset-requests" if daily else "x-ratelimit-reset-tokens"
            waits.append(duration(headers.get(header), now))
        if daily and provider in ("gemini", "cloudflare"):
            waits.append(seconds_to_midnight("America/Los_Angeles" if provider == "gemini" else "UTC", now))
        # Unknown daily reset: periodically probe, rather than inventing a reset.
        default = 900 if daily else 60
        wait = max([1.0] + [x for x in waits if x is not None]) if any(x is not None for x in waits) else default
        return bucket(provider, model), wait, "daily_quota" if daily else "rate_limit"
    if status == 401:
        return provider + ":auth", 3600, "authentication"
    if status in (402, 403, 404):
        return bucket(provider, model), 3600, "access_or_model"
    if status and int(status) >= 500:
        return bucket(provider, model), 15, "upstream_unavailable"
    return None


class ProxyModifierHook(CustomLogger):
    def __init__(self, store=None):
        super().__init__()
        self.store = store or BlockStore()

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # THIS hook runs before routing. Only generic policy belongs here.
        data = dict(data)
        if data.get("model") not in ("auto-free", "local", "cloud-free"):
            raise litellm.BadRequestError(message="Use auto-free, local, or cloud-free; direct provider routing is disabled", model=str(data.get("model")), llm_provider="auto-free")
        # Scope this kit to stateless Chat Completions. Native Responses sessions
        # and Anthropic tool/thinking blocks need their own tested translation.
        if "messages" not in data:
            raise litellm.BadRequestError(message="This configuration expects OpenAI Chat Completions with full messages history", model=str(data.get("model")), llm_provider="auto-free")
        extra = data.get("extra_body") or {}
        forbidden = {"model", "models", "route", "api_base", "api_key", "api_version", "custom_llm_provider", "fallbacks", "default_fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "plugins", "web_search_options", "guardrails"}
        if any(k in data for k in forbidden - {"model"}) or (isinstance(extra, dict) and forbidden.intersection(extra)):
            raise litellm.BadRequestError(message="Per-request routing overrides / hosted paid tools are disabled", model=str(data.get("model")), llm_provider="auto-free")
        if data.get("n", 1) != 1:
            raise litellm.BadRequestError(message="Only n=1 is allowed", model=str(data.get("model")), llm_provider="auto-free")
        for tool in data.get("tools") or []:
            if tool.get("type") != "function":
                raise litellm.BadRequestError(message="Only client-executed function tools are enabled", model=str(data.get("model")), llm_provider="auto-free")
        # Normalize before context/token prechecks. Never change the messages.
        data["max_tokens"] = output_limit(data, 16384)
        data.pop("max_completion_tokens", None)
        data.pop("max_output_tokens", None)
        data["num_retries"] = 0
        return data

    async def async_pre_call_deployment_hook(self, kwargs, call_type=None):
        data = dict(kwargs)
        provider, model = identify(data)
        if provider is None:
            if model in ("auto_router/complexity_router", "complexity_router"):
                return data
            raise litellm.BadRequestError(message="Unrecognized deployment; free-only policy refused the request", model=model, llm_provider="auto-free")
        if provider != "local":
            wait = await asyncio.to_thread(self.store.remaining, (bucket(provider, model), provider + ":auth"), time.time())
            if wait > 0:
                raise synthetic_limit("auto-free: quota/access cooldown; trying next deployment", provider, model, wait)
        effort = effort_for(data)
        extra = copy.deepcopy(data.get("extra_body") or {})
        if not isinstance(extra, dict):
            raise ValueError("extra_body must be an object")
        for key in REASONING_KEYS:
            data.pop(key, None)
            extra.pop(key, None)
        # Eliminate policy/selection overrides from inherited provider bodies.
        for key in ("model", "models", "route", "provider", "plugins", "web_search_options", "cache_control", "generation_config", "generationConfig"):
            extra.pop(key, None)
            if key != "model":
                data.pop(key, None)
        cap = 8192 if provider == "local" else 4096 if provider == "groq" else 16384
        data["max_tokens"] = output_limit(data, cap)
        data.pop("max_completion_tokens", None)
        data.pop("max_output_tokens", None)
        data["num_retries"] = 0
        if provider == "local" and data.get("tools"):
            raise litellm.BadRequestError(message="Local function tools are disabled until the RC/oMLX tool contract is verified", model=model, llm_provider="auto-free")
        if provider == "local":
            # max_tokens is an output cap. No invented thinking_budget argument.
            data.setdefault("temperature", .7)
            data.setdefault("top_p", .95)
        else:
            extra.pop("chat_template_kwargs", None)
            # Use provider defaults for sampling; current thinking models differ.
            for key in ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty"):
                data.pop(key, None)
                extra.pop(key, None)
            if provider == "openrouter":
                extra["reasoning"] = {"effort": effort}
                extra["provider"] = {"require_parameters": True, "max_price": {"prompt": 0, "completion": 0, "request": 0}}
            elif provider in ("gemini", "groq"):
                data["reasoning_effort"] = effort
            # Cloudflare: leave provider-native reasoning defaults in place;
            # do not assume every OpenAI reasoning parameter is supported.
            if provider == "groq":
                try:
                    tokens = litellm.token_counter(model="groq/openai/gpt-oss-120b", messages=data.get("messages") or [], tools=data.get("tools"))
                except Exception:
                    raise synthetic_limit("auto-free: cannot estimate Groq token budget; skipping", provider, model, 1)
                if tokens + data["max_tokens"] > 7800:
                    raise synthetic_limit("auto-free: request exceeds conservative Groq free TPM envelope; skipping without truncation", provider, model, 1)
        data["extra_body"] = extra
        return data

    async def async_post_call_failure_deployment_hook(self, request_data, exception, call_type=None, fallback_depth=None):
        provider, model = identify(request_data)
        if provider is None or provider == "local":
            return None
        now = time.time()
        block = failure_block(provider, model, exception, now)
        if block is not None:
            key, wait, reason = block
            await asyncio.to_thread(self.store.block, key, now + wait, reason)
            LOG.warning("auto-free cooldown bucket=%s seconds=%d reason=%s", key, math.ceil(wait), reason)
        return None


# Do not shadow the class with an instance. Config references this object.
proxy_modifier = ProxyModifierHook()
