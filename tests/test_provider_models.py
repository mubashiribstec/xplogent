"""Live model discovery: provider list_models() parsing + the API endpoint."""

from __future__ import annotations

import pytest


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_openai_list_models_parses_ids(monkeypatch):
    from xplogent.providers.openai import OpenAIProvider

    prov = OpenAIProvider("gpt-4o", api_key="x")

    async def fake_get(path, **kw):
        assert path == "/models"
        return _FakeResp({"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"nope": 1}]})

    monkeypatch.setattr(prov._client, "get", fake_get)
    assert await prov.list_models() == ["gpt-4o", "gpt-4o-mini"]
    await prov.aclose()


@pytest.mark.asyncio
async def test_openrouter_inherits_list_models(monkeypatch):
    from xplogent.providers.openrouter import OpenRouterProvider

    prov = OpenRouterProvider("anthropic/claude-3.5-sonnet", api_key="x")

    async def fake_get(path, **kw):
        return _FakeResp({"data": [{"id": "anthropic/claude-3.5-sonnet"}, {"id": "meta-llama/llama-3.1-70b"}]})

    monkeypatch.setattr(prov._client, "get", fake_get)
    models = await prov.list_models()
    assert "anthropic/claude-3.5-sonnet" in models
    await prov.aclose()


@pytest.mark.asyncio
async def test_ollama_list_models_from_tags(monkeypatch):
    from xplogent.providers.ollama import OllamaProvider

    prov = OllamaProvider("llama3.1")

    async def fake_get(path, **kw):
        assert path == "/api/tags"
        return _FakeResp({"models": [{"name": "llama3.1:latest"}, {"name": "llava:latest"}]})

    monkeypatch.setattr(prov._client, "get", fake_get)
    assert await prov.list_models() == ["llama3.1:latest", "llava:latest"]
    await prov.aclose()


@pytest.mark.asyncio
async def test_gemini_filters_to_generatecontent(monkeypatch):
    from xplogent.providers.gemini import GeminiProvider

    prov = GeminiProvider("gemini-1.5-pro", api_key="x")

    async def fake_get(path, **kw):
        return _FakeResp({"models": [
            {"name": "models/gemini-1.5-pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
        ]})

    monkeypatch.setattr(prov._client, "get", fake_get)
    assert await prov.list_models() == ["gemini-1.5-pro"]
    await prov.aclose()


def test_provider_models_endpoint(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))

    import xplogent.interfaces.api.deps as deps
    import xplogent.interfaces.api.server as srv

    class _FakeProv:
        async def list_models(self):
            return ["b-model", "a-model"]

        async def aclose(self):
            return None

    monkeypatch.setattr(deps, "build_provider", lambda spec, **k: _FakeProv())
    c = TestClient(srv.create_app())

    out = c.get("/providers/openrouter/models").json()
    assert out["models"] == ["a-model", "b-model"]  # sorted
    assert out["error"] == ""

    assert c.get("/providers/bogus/models").json()["error"].startswith("unknown provider")
