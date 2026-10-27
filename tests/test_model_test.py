"""The dashboard's model test: real provider errors become a reason + a fix."""

from __future__ import annotations

import json

import httpx
import pytest

from xplogent.providers.diagnose import diagnose
from xplogent.providers.openrouter import OpenRouterProvider, parse_key_info

GEMMA = "openrouter:google/gemma-4-31b-it:free"
UPSTREAM_429 = {"error": {"message": "Provider returned error", "code": 429, "metadata": {
    "raw": "google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortly.",
    "provider_name": "Google AI Studio", "limit_source": "upstream_provider_shared_pool"}}}
KEY_INFO = {"data": {"is_free_tier": True, "limit": 1, "limit_remaining": 1, "usage_daily": 0,
                     "free_model_daily_requests": {"used": 50, "limit": 50, "remaining": 0}}}


def provider_answering(status: int, body: dict, monkeypatch) -> OpenRouterProvider:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    p = OpenRouterProvider("google/gemma-4-31b-it:free")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json=KEY_INFO)
        return httpx.Response(status, json=body)

    p._client = httpx.AsyncClient(base_url=p.base_url, transport=httpx.MockTransport(handler))
    return p


async def failure(status, body, monkeypatch):
    p = provider_answering(status, body, monkeypatch)
    with pytest.raises(Exception) as info:
        async for _ in p.stream([]):
            pass
    return info.value


@pytest.mark.parametrize("status, body, code, words", [
    (429, UPSTREAM_429, "rate_limited_upstream", "Google AI Studio"),
    (401, {"error": {"message": "User not found.", "code": 401}}, "bad_key", "refused"),
    (402, {"error": {"message": "Key limit exceeded", "code": 402,
                     "metadata": {"limit_source": "openrouter_key_limit"}}}, "key_limit", "spending limit"),
    (402, {"error": {"message": "Insufficient credits", "code": 402}}, "credits", "credits"),
    (404, {"error": {"message": "No endpoints found for x/y.", "code": 404}}, "unknown_model", "not found"),
    (503, {"error": {"message": "No available provider", "code": 503}}, "provider_down", "down"),
])
async def test_real_openrouter_errors_are_explained(monkeypatch, status, body, code, words):
    exc = await failure(status, body, monkeypatch)
    assert isinstance(exc, httpx.HTTPStatusError)            # retry / backup still see the type
    d = diagnose(exc, GEMMA)
    assert d["code"] == code and words in d["reason"] and d["fix"]


async def test_free_daily_cap_wins_over_a_generic_429(monkeypatch):
    exc = await failure(429, {"error": {"message": "Rate limit exceeded: free-models-per-day", "code": 429}},
                        monkeypatch)
    d = diagnose(exc, GEMMA, quota=parse_key_info(KEY_INFO))
    assert d["code"] == "free_daily_cap" and "50" in d["reason"] and "00:00 UTC" in d["fix"]


async def test_mid_stream_error_after_200(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    p = OpenRouterProvider("m:free")
    sse = ('data: {"choices":[{"delta":{},"finish_reason":"error"}],'
           '"error":{"code":502,"message":"upstream died"}}\n\n')
    p._client = httpx.AsyncClient(base_url=p.base_url, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})))
    with pytest.raises(RuntimeError) as info:
        async for _ in p.stream([]):
            pass
    assert info.value.details["mid_stream"] and diagnose(info.value, "openrouter:m:free")["reason"]


def test_key_info_parse():
    q = parse_key_info(KEY_INFO)
    assert (q["free_used"], q["free_limit"], q["free_remaining"], q["is_free_tier"]) == (50, 50, 0, True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    return TestClient(create_app(), base_url="http://127.0.0.1")


def test_endpoint_without_key_says_where_to_put_it(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    out = client.post("/providers/test-model", json={"spec": GEMMA}).json()
    assert out["code"] == "no_key" and "OPENROUTER_API_KEY" in out["fix"]


def test_endpoint_reports_tool_call_timing_and_quota(client, monkeypatch):
    from xplogent.providers import openrouter

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {
            "name": "book_callback", "arguments": "{\"when\": \"tomorrow 10am\"}"}}]}}]}]) + "data: [DONE]\n\n"
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {**KEY_INFO["data"], "free_model_daily_requests":
                                                      {"used": 3, "limit": 50, "remaining": 47}}})
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "book_callback"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    real_init = openrouter.OpenRouterProvider.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=self._client.headers,
                                         transport=httpx.MockTransport(handler))

    monkeypatch.setattr(openrouter.OpenRouterProvider, "__init__", init)
    out = client.post("/providers/test-model", json={"spec": GEMMA}).json()
    assert out["ok"] and out["tool_call"] == {"name": "book_callback", "arguments": {"when": "tomorrow 10am"}}
    assert out["quota"]["free_remaining"] == 47 and out["ms_total"] >= 0
    assert seen == ["/api/v1/key", "/api/v1/chat/completions"]


def test_endpoint_explains_an_unknown_ollama_server(client):
    out = client.post("/providers/test-model", json={"spec": "ollama:qwen2.5:7b@gpu"}).json()
    assert not out["ok"] and out["code"] == "unknown_server" and "Settings → Ollama servers" in out["fix"]


def test_pasted_api_keys_are_trimmed(tmp_path, monkeypatch):
    import os

    from xplogent.core.config import save_env

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "")         # restored after the test
    save_env({"OPENROUTER_API_KEY": "  sk-or-v1-abc  \t"})
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-v1-abc"


GEMINI_404 = {"error": {"code": 404, "status": "NOT_FOUND", "message":
              "models/gemini-2.5-flash is not found for API version v1beta, or is not supported for "
              "generateContent. Call ListModels to see the list of available models."}}
GEMINI_BAD_KEY = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message":
                  "API key not valid. Please pass a valid API key.",
                  "details": [{"reason": "API_KEY_INVALID"}]}}


@pytest.mark.parametrize("status, body, code, words", [
    (404, GEMINI_404, "unknown_model", "gemini-3.6-flash"),
    (400, GEMINI_BAD_KEY, "bad_key", "Settings → API keys → GEMINI_API_KEY"),
])
async def test_gemini_errors_are_explained(monkeypatch, status, body, code, words):
    from xplogent.providers.gemini import GeminiProvider

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    p = GeminiProvider("gemini-2.5-flash")
    p._client = httpx.AsyncClient(base_url=p.base_url, transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json=body)))
    with pytest.raises(httpx.HTTPStatusError) as info:
        async for _ in p.stream([]):
            pass
    assert "not found for API version" in str(info.value) or "API key not valid" in str(info.value)
    d = diagnose(info.value, "gemini:gemini-2.5-flash")
    assert d["code"] == code and words in (d["reason"] + d["fix"])


def test_retired_gemini_models_fail_the_readiness_check():
    from xplogent.voip.readiness import checklist, retired_model

    assert "shut down Gemini 2.0" in retired_model("gemini:gemini-2.0-flash")
    assert retired_model("gemini:gemini-1.5-pro") and not retired_model("gemini:gemini-3.6-flash")
    items = {i["id"]: i for i in checklist({"model": "gemini:gemini-2.0-flash"}, "x",
                                           env={"GEMINI_API_KEY": "k"}, gpu=True)}
    assert not items["model"]["ok"] and "gemini-3.6-flash" in items["model"]["detail"]


def test_shipped_gemini_defaults_are_current():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for f in ("src/xplogent/config/default.yaml", "src/xplogent/interfaces/cli/app.py",
              "web/src/components/Onboarding.tsx", "web/src/Settings.tsx"):
        text = (root / f).read_text()
        assert "gemini-2.0" not in text and "gemini-1.5" not in text, f


async def test_chat_error_line_explains_a_gemini_404(tmp_path, monkeypatch):
    from xplogent.core.agent import Agent
    from xplogent.core.config import load_config
    from xplogent.core.events import EventType
    from xplogent.providers.gemini import GeminiProvider
    from xplogent.safety.approval import SafetyManager
    from xplogent.tools.registry import ToolRegistry

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    p = GeminiProvider("gemini-2.5-flash")
    p._client = httpx.AsyncClient(base_url=p.base_url, transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json=GEMINI_404)))
    agent = Agent(load_config(), p, ToolRegistry.from_config([]), SafetyManager())
    errors = []
    real_emit = agent._emit

    async def emit(kind, **data):
        if kind == EventType.ERROR:
            errors.append(data.get("message", ""))
        await real_emit(kind, **data)

    monkeypatch.setattr(agent, "_emit", emit)
    await agent.run("what is the weather like")
    assert errors and "doesn't serve gemini-2.5-flash" in errors[0] and "gemini-3.6-flash" in errors[0]
