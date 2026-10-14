"""Backup model: a local Ollama answers when the main model can't."""

from __future__ import annotations

import httpx
import pytest

from xplogent.core import config as cfgmod
from xplogent.providers import registry
from xplogent.providers.base import Message, Provider, Role, StreamEvent, StreamKind
from xplogent.providers.fallback import FallbackProvider


class Scripted(Provider):
    name = "scripted"

    def __init__(self, model="m", *, fail_after: int | None = None, reply="hi", **kw):
        super().__init__(model, **kw)
        self.fail_after = fail_after
        self.reply = reply
        self.calls = 0
        self.closed = False

    async def stream(self, messages, tools=None, **kwargs):
        self.calls += 1
        for i, ch in enumerate(self.reply):
            if self.fail_after is not None and i >= self.fail_after:
                raise httpx.ConnectError("down")
            yield StreamEvent(StreamKind.TOKEN, text=ch)
        if self.fail_after is not None and self.fail_after >= len(self.reply):
            raise httpx.ConnectError("down")
        yield StreamEvent(StreamKind.DONE, message=Message(role=Role.ASSISTANT, content=self.reply))

    async def embed(self, texts):
        return [[float(len(self.reply))] for _ in texts]

    async def aclose(self):
        self.closed = True


def _ask():
    return [Message(role=Role.USER, content="hello")]


async def test_backup_answers_when_primary_fails_before_output():
    backup = Scripted(reply="backup")
    fb = FallbackProvider(Scripted(fail_after=0), lambda: backup, backup_spec="ollama-local:x")
    msg = await fb.complete(_ask())
    assert msg.content == "backup" and fb.used_backup and backup.calls == 1


async def test_no_backup_once_the_primary_started_answering():
    backup = Scripted(reply="backup")
    fb = FallbackProvider(Scripted(reply="primary", fail_after=3), lambda: backup)
    with pytest.raises(httpx.ConnectError):
        await fb.complete(_ask())
    assert backup.calls == 0


async def test_primary_ok_never_builds_backup_and_embeddings_stay_on_primary():
    built = []
    primary = Scripted(reply="primary")
    fb = FallbackProvider(primary, lambda: built.append(1) or Scripted())
    assert (await fb.complete(_ask())).content == "primary"
    assert not fb.used_backup and built == []
    assert await fb.embed(["a"]) == [[7.0]]
    await fb.aclose()
    assert primary.closed


def test_build_provider_wraps_only_when_enabled():
    plain = registry.build_provider("ollama:llama3.1")
    assert not isinstance(plain, FallbackProvider)
    cfgmod.save_user_config({"fallback": {"enabled": True, "model": "llama3.2:3b"}})
    assert registry.backup_spec() == "ollama-local:llama3.2:3b"
    wrapped = registry.build_provider("openai:gpt-4o-mini")
    assert isinstance(wrapped, FallbackProvider) and wrapped.backup_spec == "ollama-local:llama3.2:3b"
    assert not isinstance(registry.build_provider("openai:gpt-4o-mini", fallback=False), FallbackProvider)
    # The backup itself is never wrapped in itself.
    assert not isinstance(registry.build_provider("ollama-local:llama3.2:3b"), FallbackProvider)
    cfgmod.save_user_config({"fallback": {"model": "openai:gpt-4o-mini"}})
    assert registry.backup_spec() == "openai:gpt-4o-mini"      # any provider can be the backup


def test_local_provider_uses_the_docker_host_without_token(monkeypatch):
    from xplogent.providers.ollama import LocalOllamaProvider

    monkeypatch.setenv("OLLAMA_API_KEY", "remote-secret")
    p = LocalOllamaProvider("llama3.2:3b")
    assert p.host == "http://ollama:11434"
    assert "authorization" not in p._client.headers
    cfgmod.save_user_config({"fallback": {"host": "http://10.0.0.5:11434"}})
    assert LocalOllamaProvider("x").host == "http://10.0.0.5:11434"


# ── routes ───────────────────────────────────────────────────────────────────
def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    return TestClient(create_app())


@pytest.fixture
def local_ollama(monkeypatch):
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def handler(request):
        seen.append(request)
        if request.url.host != "ollama":
            return httpx.Response(404)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.2:3b"}]})
        if request.url.path == "/api/pull":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(404)

    class _Client(real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


def test_backup_routes(local_ollama, monkeypatch):
    c = _client()
    st = c.get("/providers/backup").json()
    assert st["reachable"] and st["installed"] and st["enabled"] is False
    assert c.post("/providers/backup", json={"enabled": True, "model": "llama3.2:3b"}).json()["ok"]
    assert c.get("/providers/backup").json()["enabled"] is True
    assert c.post("/providers/backup", json={"host": "ftp://x"}).json()["ok"] is False
    assert c.post("/providers/backup/pull", json={}).json() == {"ok": True, "model": "llama3.2:3b"}
    assert any(r.url.path == "/api/pull" for r in local_ollama)

    made = []

    def fake_build(spec, *, fallback=True, **kw):
        made.append((spec, fallback))
        return Scripted(reply="Hello there!")

    monkeypatch.setattr(registry, "build_provider", fake_build)
    res = c.post("/providers/backup/test").json()
    assert res["ok"] and res["reply"] == "Hello there!"
    assert made == [("ollama-local:llama3.2:3b", False)]
