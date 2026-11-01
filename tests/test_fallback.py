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
    fb = FallbackProvider(Scripted(fail_after=0), lambda: backup, backup_spec="gemini:gemini-3.6-flash")
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
    plain = registry.build_provider("openai:gpt-4o-mini")
    assert not isinstance(plain, FallbackProvider)
    assert registry.backup_spec() == ""                          # off and empty by default
    cfgmod.save_user_config({"fallback": {"enabled": True, "model": "gemini:gemini-3.6-flash"}})
    assert registry.backup_spec() == "gemini:gemini-3.6-flash"
    wrapped = registry.build_provider("openai:gpt-4o-mini")
    assert isinstance(wrapped, FallbackProvider) and wrapped.backup_spec == "gemini:gemini-3.6-flash"
    assert not isinstance(registry.build_provider("openai:gpt-4o-mini", fallback=False), FallbackProvider)
    # The backup itself is never wrapped in itself.
    assert not isinstance(registry.build_provider("gemini:gemini-3.6-flash"), FallbackProvider)


def test_nothing_points_at_a_local_ollama_any_more():
    """Ollama is no longer bundled: an older bare backup name means your own Ollama server."""
    assert "ollama-local" not in registry.available_providers()
    cfgmod.save_user_config({"fallback": {"enabled": True, "model": "llama3.2:3b"}})
    assert registry.backup_spec() == "ollama:llama3.2:3b"
    cfgmod.save_user_config({"fallback": {"server": "gpu"}})
    assert registry.backup_spec() == "ollama:llama3.2:3b@gpu"


async def test_an_empty_model_setting_explains_itself_and_switches_embeddings_off():
    from xplogent.memory.vector import Embedder

    none = registry.build_provider("", fallback=False)
    with pytest.raises(RuntimeError, match="Settings → Models"):
        await none.complete(_ask())
    emb = Embedder(none)
    assert await emb.embed(["a", "b"]) == [[], []] and emb.available is False


# ── routes ───────────────────────────────────────────────────────────────────
def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    return TestClient(create_app())


def test_backup_routes(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    c = _client()
    st = c.get("/providers/backup").json()
    assert st == {"enabled": False, "model": "", "server": "", "ready": False,
                  "problem": "choose a backup model first"}
    assert not c.post("/providers/backup", json={"enabled": True}).json()["ok"]      # nothing chosen
    assert not c.post("/providers/backup", json={"model": "llama3.2"}).json()["ok"]  # not provider:model
    assert c.post("/providers/backup", json={"enabled": True, "model": "gemini:gemini-3.6-flash"}).json()["ok"]
    st = c.get("/providers/backup").json()
    assert st["enabled"] and st["model"] == "gemini:gemini-3.6-flash"
    assert not st["ready"] and "GEMINI_API_KEY" in st["problem"]
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    assert c.get("/providers/backup").json()["ready"]
    assert c.post("/providers/backup/pull", json={}).status_code in (404, 405)   # nothing to download here

    made = []

    def fake_build(spec, *, fallback=True, **kw):
        made.append((spec, fallback))
        return Scripted(reply="Hello there!")

    monkeypatch.setattr(registry, "build_provider", fake_build)
    res = c.post("/providers/backup/test").json()
    assert res["ok"] and res["reply"] == "Hello there!"
    assert made == [("gemini:gemini-3.6-flash", False)]


async def test_openrouter_free_model_rate_limit_hands_the_turn_to_the_backup(monkeypatch):
    """A free OpenRouter model answering 429 is the common case: the backup must answer."""
    from xplogent.providers.openrouter import OpenRouterProvider

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(429, json={"error": {"message": "Provider returned error", "metadata": {
            "raw": "google/gemma-4-31b-it:free is temporarily rate-limited upstream."}}})

    primary = OpenRouterProvider("google/gemma-4-31b-it:free")
    primary._client = httpx.AsyncClient(base_url=primary.base_url, headers=primary._client.headers,
                                        transport=httpx.MockTransport(handler))
    backup = Scripted("b", reply="backup here")
    fb = FallbackProvider(primary, lambda: backup, backup_spec="ollama:qwen2.5:7b@gpu")
    text = "".join([ev.text or "" async for ev in fb.stream([Message(role=Role.USER, content="hi")])
                    if ev.kind == StreamKind.TOKEN])
    assert text == "backup here" and fb.used_backup
    assert seen == [("/api/v1/chat/completions", "Bearer sk-or-test")]

    with pytest.raises(httpx.HTTPStatusError, match="429 Too Many Requests: google/gemma-4-31b-it:free is temporarily"):
        async for _ in primary.stream([Message(role=Role.USER, content="hi")]):
            pass
