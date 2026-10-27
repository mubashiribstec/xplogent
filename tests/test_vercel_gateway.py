"""Vercel AI Gateway: an OpenAI-compatible endpoint for many providers' models."""

from __future__ import annotations

import json

import httpx
import pytest

from xplogent.providers.registry import build_provider
from xplogent.providers.vercel import VercelGatewayProvider, pick_quick_model


def test_spec_base_url_and_key(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck_test")
    p = build_provider("vercel:anthropic/claude-opus-5", fallback=False)
    assert isinstance(p, VercelGatewayProvider) and p.model == "anthropic/claude-opus-5"
    assert str(p._client.base_url).rstrip("/") == "https://ai-gateway.vercel.sh/v1"
    assert p._client.headers["Authorization"] == "Bearer vck_test"


def test_pick_quick_model():
    ids = ["openai/text-embedding-3-small", "anthropic/claude-opus-5", "google/gemini-3.6-flash",
           "google/gemini-3.5-flash-lite", "openai/gpt-image-2"]
    assert pick_quick_model(ids) == "google/gemini-3.5-flash-lite"
    assert pick_quick_model(["anthropic/claude-opus-5"]) == "anthropic/claude-opus-5"
    assert pick_quick_model(["openai/text-embedding-3-small"]) == ""


def test_the_key_is_a_known_secret():
    from xplogent.core.config import allowed_env_key, secret_names

    assert allowed_env_key("AI_GATEWAY_API_KEY") and "AI_GATEWAY_API_KEY" in secret_names()


def test_test_key_picks_a_quick_model_and_reports_the_tool_call(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app
    from xplogent.providers import vercel

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck_test")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "anthropic/claude-opus-5", "type": "language", "tags": ["tool-use"],
                 "pricing": {"input": "0.000005", "output": "0.000025"}},
                {"id": "google/gemini-3.6-flash", "type": "language", "tags": ["tool-use"],
                 "pricing": {"input": "0.0000003", "output": "0.0000025"}},
                {"id": "stealth/free-thing", "type": "language", "tags": ["tool-use"],
                 "pricing": {"input": "0", "output": "0"}},
                {"id": "openai/text-embedding-3-small", "type": "embedding",
                 "pricing": {"input": "0.00000002"}}]})
        body = json.loads(request.content)
        assert body["model"] == "google/gemini-3.6-flash"
        sse = "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {
            "name": "book_callback", "arguments": "{\"when\": \"tomorrow 10am\"}"}}]}}]}) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    real_init = vercel.VercelGatewayProvider.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=self._client.headers,
                                         transport=httpx.MockTransport(handler))

    monkeypatch.setattr(vercel.VercelGatewayProvider, "__init__", init)
    out = TestClient(create_app(), base_url="http://127.0.0.1").post(
        "/providers/test-model", json={"spec": "vercel:auto"}).json()
    assert out["ok"] and out["spec"] == "vercel:google/gemini-3.6-flash"
    assert out["tool_call"]["name"] == "book_callback"
    assert seen == ["/v1/models", "/v1/chat/completions"]


def test_a_refused_gateway_key_is_explained(monkeypatch):
    from xplogent.providers.diagnose import diagnose

    req = httpx.Request("GET", "https://ai-gateway.vercel.sh/v1/models")
    exc = httpx.HTTPStatusError("401", request=req, response=httpx.Response(401, request=req))
    d = diagnose(exc, "vercel:auto")
    assert d["code"] == "bad_key" and "AI_GATEWAY_API_KEY" in d["fix"]
