"""Validate using YOUR installed LiteLLM. Default: no provider calls.
--router-test: real LiteLLM against two localhost mock HTTP endpoints (no quota).
--catalogs: optional authenticated read-only model-list calls (no inference).
"""
import argparse
import asyncio
import copy
import importlib.metadata
import inspect
import json
import os
import re
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def validate():
    import yaml
    import litellm
    from litellm import Router
    from litellm.integrations.custom_logger import CustomLogger
    from litellm.router_strategy.complexity_router.config import ComplexityRouterConfig
    print("Installed LiteLLM:", importlib.metadata.version("litellm"))
    for name in ("async_pre_call_deployment_hook", "async_post_call_failure_deployment_hook"):
        if not hasattr(CustomLogger, name):
            raise RuntimeError("Installed LiteLLM lacks " + name + "; update in your existing venv, then retest")
    config = yaml.safe_load((ROOT / "proxy_config.yaml").read_text())
    auto = next(x for x in config["model_list"] if x["model_name"] == "auto-free")
    cfg = auto["litellm_params"]["complexity_router_config"]
    unknown = set(cfg) - set(ComplexityRouterConfig.model_fields)
    if unknown:
        raise RuntimeError("Installed complexity router does not recognize: " + ", ".join(sorted(unknown)))
    ComplexityRouterConfig.model_validate(cfg)
    if len({m["model_info"]["id"] for m in config["model_list"] if "model_info" in m}) != sum("model_info" in m for m in config["model_list"]):
        raise RuntimeError("Deployment IDs must be unique")
    orders = [m["litellm_params"]["order"] for m in config["model_list"] if m["model_name"] == "cloud-free"]
    if not orders or len(set(orders)) != len(orders) or any(x < 1 for x in orders):
        raise RuntimeError("Cloud orders must be unique positive integers")
    # Constructor/schema check with dummy values. No inference, no real secrets.
    models = copy.deepcopy(config["model_list"])
    for entry in models:
        params = entry["litellm_params"]
        for key, value in list(params.items()):
            if isinstance(value,str) and value.startswith("os.environ/"):
                params[key] = "https://api.cloudflare.com/client/v4/accounts/" + "0"*32 + "/ai/v1" if key == "api_base" else "<redacted>"
    Router(model_list=models, **config["router_settings"])
    import custom_callbacks
    assert isinstance(custom_callbacks.proxy_modifier, CustomLogger)
    print("PASS: callback API, complexity settings, and Router configuration load")
    # Reveal only readiness, never secret/reference values.
    missing = []
    for name in ("GEMINI_API_KEY","GROQ_API_KEY","OPENROUTER_API_KEY","CLOUDFLARE_API_KEY"):
        value = os.environ.get(name, "")
        ok = bool(value) and not value.startswith("op://")
        print(name + ": " + ("resolved" if ok else "NOT RESOLVED in this shell"))
        if not ok: missing.append(name)
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID","")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", account):
        missing.append("CLOUDFLARE_ACCOUNT_ID")
    expected_base = "https://api.cloudflare.com/client/v4/accounts/" + account + "/ai/v1"
    if os.environ.get("CLOUDFLARE_API_BASE") != expected_base:
        missing.append("CLOUDFLARE_API_BASE (source proxy-env.sh)")
    if missing:
        print("ENVIRONMENT NOT READY: " + ", ".join(missing))
        print("launchd/op run environment is separate from your interactive terminal.")
    return not missing


async def router_test():
    """Actual Router/HTTP/callback lifecycle test, not the unit-test stub."""
    import litellm
    from litellm import Router
    from litellm.integrations.custom_logger import CustomLogger
    from custom_callbacks import LOCAL_MODEL, proxy_modifier
    hits = {"first":0,"second":0}
    sent = []
    events = []
    def handler(which):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))))
                hits[which]+=1
                sent.append(body)
                if which == "first":
                    status=429
                    response={"error":{"message":"synthetic upstream RPM limit","type":"rate_limit_error","code":"rate_limit_exceeded"}}
                else:
                    status=200
                    response={"id":"chatcmpl-test","object":"chat.completion","created":1,"model":LOCAL_MODEL,"choices":[{"index":0,"message":{"role":"assistant","content":"ORDERED_FAILOVER_OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}
                encoded=json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(encoded)))
                self.send_header("Retry-After","1")
                self.end_headers()
                self.wfile.write(encoded)
        return Handler
    servers=[ThreadingHTTPServer(("127.0.0.1",0),handler(w)) for w in ("first","second")]
    for server in servers:
        threading.Thread(target=server.serve_forever,daemon=True).start()
    class Recorder(CustomLogger):
        async def async_pre_call_deployment_hook(self,kwargs,call_type=None):
            events.append("pre")
            return kwargs
        async def async_post_call_failure_deployment_hook(self,request_data,exception,call_type=None,fallback_depth=None):
            events.append("failure")
    previous=list(litellm.callbacks)
    litellm.callbacks=[proxy_modifier,Recorder()]
    try:
        models=[{"model_name":"order-test","litellm_params":{"model":"openai/"+LOCAL_MODEL,"api_base":f"http://127.0.0.1:{server.server_port}/v1","api_key":"sk-local-test","order":i+1,"num_retries":0},"model_info":{"id":f"test-{i}"}} for i,server in enumerate(servers)]
        router=Router(model_list=models,num_retries=0,max_fallbacks=12,cooldown_time=1)
        result=await asyncio.wait_for(router.acompletion(model="order-test",messages=[{"role":"user","content":"test"}],max_tokens=131072),timeout=20)
        assert result.choices[0].message.content == "ORDERED_FAILOVER_OK", "Wrong response"
        assert hits == {"first":1,"second":1}, f"Unexpected retry/failover counts: {hits}"
        assert events.count("pre") == 2 and events.count("failure") == 1, f"Hook lifecycle mismatch: {events}"
        assert all(body["max_tokens"] == 8192 for body in sent), "Deployment rewrite did not reach the wire"
        print("PASS: real LiteLLM ordered failover, one 429, one fallback, deployment hooks, wire output cap")
    finally:
        litellm.callbacks=previous
        for server in servers:
            server.shutdown()
            server.server_close()


def catalogs():
    # Headers only, no keys in URLs. No inference requests. No error-body dumps.
    def get(url,headers):
        request=urllib.request.Request(url,headers=headers)
        with urllib.request.urlopen(request,timeout=15) as response:
            return json.load(response)
    key=os.environ.get("GEMINI_API_KEY","")
    if not key or key.startswith("op://"): raise RuntimeError("Resolve GEMINI_API_KEY with op run first")
    names=set()
    page=""
    while True:
        from urllib.parse import urlencode
        data=get("https://generativelanguage.googleapis.com/v1beta/models?"+urlencode({"pageSize":1000,**({"pageToken":page} if page else {})}),{"x-goog-api-key":key})
        names.update(m["name"].removeprefix("models/") for m in data.get("models",[]))
        page=data.get("nextPageToken")
        if not page: break
    from custom_callbacks import GEMINI_MODELS
    absent=GEMINI_MODELS-names
    for model in sorted(GEMINI_MODELS): print(model+": "+("listed" if model in names else "NOT LISTED; remove this deployment or correct its exact ID"))
    if absent: raise RuntimeError("Resolve unlisted Gemini model IDs before activating")
    print("Catalog presence is NOT proof of free eligibility or remaining quota; your AI Studio account page is authoritative.")


def requires_resolved_environment(router_test, catalogs):
    """Only provider catalog reads require resolved provider credentials."""
    return not router_test or catalogs


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router-test",action="store_true")
    parser.add_argument("--catalogs",action="store_true")
    args=parser.parse_args()
    try:
        ready=validate()
        if args.router_test: asyncio.run(router_test())
        if args.catalogs: catalogs()
        if not ready and requires_resolved_environment(args.router_test, args.catalogs): sys.exit(2)
    except Exception as error:
        # Avoid dumping provider payloads or resolved config containing keys.
        print("PRECHECK FAILED:",type(error).__name__)
        if isinstance(error,(RuntimeError,AssertionError,ModuleNotFoundError)): print(str(error))
        else: print("Check installed LiteLLM compatibility / configuration. No credentials were printed.")
        sys.exit(1)
