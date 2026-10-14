"""Everything the portal configures: remote Ollama + token, Telnyx, speech, capabilities."""

from __future__ import annotations

import httpx
import pytest

from xplogent.core import config as cfgmod
from xplogent.voip.readiness import checklist, public_url_problem


def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    return TestClient(create_app())


@pytest.fixture
def fake_http(monkeypatch):
    """Route every httpx.AsyncClient request to a handler (records requests)."""
    seen: list[httpx.Request] = []
    routes: dict = {}
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for (host_part, path), fn in routes.items():
            if host_part in str(request.url) and request.url.path == path:
                return fn(request)
        return httpx.Response(404, json={"error": "no route"})

    class _Client(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return routes, seen


# ── dashboard settings win over the Docker environment ───────────────────────
def test_dashboard_model_beats_env_default(monkeypatch):
    monkeypatch.setenv("XPLOGENT_MODEL", "openai:gpt-4o-mini")
    assert cfgmod.load_config().model == "openai:gpt-4o-mini"          # env as default
    cfgmod.save_user_config({"model": "ollama:qwen2.5"})
    assert cfgmod.load_config().model == "ollama:qwen2.5"              # dashboard wins


def test_empty_env_var_does_not_hide_a_saved_value(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "")                               # `OLLAMA_HOST=` in Docker .env
    cfgmod.env_path().write_text("OLLAMA_HOST=http://saved:11434\n")
    cfgmod.load_dotenv()
    import os

    assert os.environ["OLLAMA_HOST"] == "http://saved:11434"


# ── remote Ollama with a bearer token ────────────────────────────────────────
def test_ollama_uses_dashboard_host_and_bearer_token(monkeypatch):
    from xplogent.providers.ollama import OllamaProvider, ollama_settings

    monkeypatch.setenv("OLLAMA_HOST", "")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert ollama_settings()[0] == "http://localhost:11434"            # never an empty address
    cfgmod.save_user_config({"ollama": {"host": "https://ai.example.com/"}})
    monkeypatch.setenv("OLLAMA_API_KEY", "tok-123")
    prov = OllamaProvider("llama3.1")
    assert prov.host == "https://ai.example.com"
    assert prov._client.headers["authorization"] == "Bearer tok-123"


def test_ollama_test_route_reports_ok_and_bad_token(fake_http):
    routes, seen = fake_http

    def tags(request):
        if request.headers.get("authorization") != "Bearer good":
            return httpx.Response(401)
        return httpx.Response(200, json={"models": [{"name": "llama3.1:latest"}, {"name": "nomic-embed-text"}]})

    routes[("ai.example.com", "/api/tags")] = tags
    c = _client()
    ok = c.post("/providers/ollama/test", json={"host": "https://ai.example.com", "token": "good"}).json()
    assert ok == {"ok": True, "models": ["llama3.1:latest", "nomic-embed-text"]}
    bad = c.post("/providers/ollama/test", json={"host": "https://ai.example.com", "token": "nope"}).json()
    assert bad["ok"] is False and "token" in bad["error"]
    assert c.post("/providers/ollama/test", json={"host": "ftp://x"}).json()["ok"] is False


def test_ollama_settings_saved_encrypted_and_used(fake_http):
    routes, _ = fake_http
    routes[("ai.example.com", "/api/tags")] = lambda r: httpx.Response(
        200 if r.headers.get("authorization") == "Bearer s3cret" else 401, json={"models": []})
    c = _client()
    assert c.post("/providers/ollama/settings",
                  json={"host": "https://ai.example.com", "token": "s3cret"}).json()["ok"]
    full = c.get("/config/full").json()
    assert full["ollama"]["host"] == "https://ai.example.com"
    assert full["secrets"]["OLLAMA_API_KEY"] is True
    # The test button works with the saved token without sending it again.
    assert c.post("/providers/ollama/test", json={}).json()["ok"] is True
    st = c.get("/status").json()
    assert st["ollama"]["reachable"] is True and st["ollama"]["auth"] is True


def test_ollama_pull_goes_through_the_server_api(fake_http):
    routes, seen = fake_http
    cfgmod.save_user_config({"ollama": {"host": "https://ai.example.com"}})
    routes[("ai.example.com", "/api/pull")] = lambda r: httpx.Response(200, json={"status": "success"})
    res = _client().post("/providers/ollama/pull", json={"model": "llama3.1"}).json()
    assert res["ok"] is True
    assert any(r.url.path == "/api/pull" for r in seen)


# ── Telnyx readiness + test ───────────────────────────────────────────────────
@pytest.mark.parametrize("url, ok", [
    ("", False), ("http://agent.example.com", False), ("https://10.44.1.27", False),
    ("https://localhost", False), ("https://agent.example.com", True),
])
def test_public_url_rules(url, ok):
    assert (public_url_problem(url) == "") is ok


def test_checklist_explains_what_is_missing():
    v = {"public_url": "http://10.44.1.27:8765", "tts": {"base_url": "https://api.openai.com/v1"}}
    items = {i["id"]: i for i in checklist(v, "openai:gpt-4o-mini", env={})}
    assert not items["public_url"]["ok"] and "https" in items["public_url"]["detail"]
    assert not items["telnyx_key"]["ok"]
    assert not items["model"]["ok"] and "OPENAI_API_KEY" in items["model"]["detail"]
    assert not items["speech"]["ok"] and "Local on this server" in items["speech"]["detail"]
    assert items["connection_id"]["required"] is False

    good = {"public_url": "https://agent.example.com", "webhook_secret": "x", "enabled": True,
            "connection_id": "123", "model": "ollama:llama3.1",
            "stt": {"provider": "whisper-local"}, "tts": {"base_url": "http://kokoro:8880/v1"}}
    assert all(i["ok"] for i in checklist(good, "openai:x", env={"TELNYX_API_KEY": "k"}))


def test_telnyx_test_route(monkeypatch):
    from xplogent.voip.telephony import telnyx

    monkeypatch.setenv("TELNYX_API_KEY", "KEY123")
    cfgmod.save_user_config({"voip": {"connection_id": "app-1", "public_url": "https://a.example.com",
                                      "webhook_secret": "sec"}})

    async def balance(self):
        return {"balance": "10.00"}

    async def get_app(self, app_id):
        return {"application_name": "AI line", "webhook_event_url": "https://other.example.com/hook"}

    monkeypatch.setattr(telnyx.TelnyxClient, "balance", balance)
    monkeypatch.setattr(telnyx.TelnyxClient, "get_application", get_app)
    res = _client().post("/voip/telnyx/test").json()
    assert res["ok"] and res["application"] == "AI line"
    assert res["webhook_matches"] is False and "webhook" in res["warning"]

    async def bad_key(self):
        raise httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://t"),
                                    response=httpx.Response(401))

    monkeypatch.setattr(telnyx.TelnyxClient, "balance", bad_key)
    assert _client().post("/voip/telnyx/test").json() == {"ok": False, "error": "Telnyx rejected the API key"}


def test_voip_settings_are_returned_to_the_calls_view():
    cfgmod.save_user_config({"voip": {"company": "Xplosole", "connection_id": "app-9"}})
    full = _client().get("/config/full").json()
    assert full["voip"]["company"] == "Xplosole" and full["voip"]["connection_id"] == "app-9"
    assert _client().get("/voip/readiness").json()["ready"] is False


def test_speech_test_round_trip(monkeypatch):
    from xplogent.voip import providers

    class FakeTTS:
        sample_rate = 24000

        async def synthesize(self, text):
            yield b"\x01\x00" * 24000            # one second

        async def aclose(self):
            pass

    class FakeSTT:
        async def transcribe(self, pcm):
            assert len(pcm) == 32000              # resampled to 16 kHz
            return "hello this is a voice test"

        async def aclose(self):
            pass

    monkeypatch.setattr(providers, "build_tts", lambda cfg: FakeTTS())
    monkeypatch.setattr(providers, "build_stt", lambda cfg: FakeSTT())
    res = _client().post("/voip/speech/test", json={}).json()
    assert res["ok"] and res["audio_seconds"] == 1.0 and "voice test" in res["transcript"]


# ── capabilities for "show disabled" ─────────────────────────────────────────
def test_status_reports_capabilities(monkeypatch):
    monkeypatch.setenv("XPLOGENT_IN_DOCKER", "1")
    caps = _client().get("/status").json()["capabilities"]
    assert caps["docker"] is True and caps["git"] is False
    assert isinstance(caps["display"], bool)
