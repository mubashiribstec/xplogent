"""Shared test fixtures and fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from xplogent.providers.base import (
    Message,
    Provider,
    Role,
    StreamEvent,
    StreamKind,
    ToolSpec,
)


@pytest.fixture(autouse=True)
def _isolate_data(tmp_path_factory, monkeypatch):
    """Point every test at a throwaway data dir so nothing touches the real
    install/home (SOUL.md, MEMORY.md, the DB) or triggers legacy migration.

    Tests that set XPLOGENT_HOME or delenv it still override this (later wins)."""
    home = tmp_path_factory.mktemp("xph")
    monkeypatch.setenv("XPLOGENT_HOME", str(home))
    monkeypatch.setattr("xplogent.core.config._migrated", True, raising=False)
    # FastAPI's TestClient sends `Host: testserver`; the API's DNS-rebinding guard only
    # accepts localhost names unless told otherwise.
    monkeypatch.setenv("XPLOGENT_ALLOWED_HOSTS", "testserver")


class ScriptedProvider(Provider):
    """A provider that replays a predetermined list of assistant messages."""

    name = "scripted"

    def __init__(self, replies: list[Message], embeddings: dict[str, list[float]] | None = None):
        super().__init__(model="scripted")
        self._replies = list(replies)
        self._embeddings = embeddings or {}

    async def stream(  # type: ignore[override]
        self, messages: list[Message], tools: list[ToolSpec] | None = None, **kwargs
    ) -> AsyncIterator[StreamEvent]:
        reply = self._replies.pop(0) if self._replies else Message(role=Role.ASSISTANT, content="done")
        if reply.content:
            yield StreamEvent(kind=StreamKind.TOKEN, text=reply.content)
        yield StreamEvent(kind=StreamKind.DONE, message=reply)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            # deterministic tiny embedding based on word overlap buckets
            vec = [0.0] * 8
            for _i, ch in enumerate(t.lower()):
                vec[ord(ch) % 8] += 1.0
            out.append(vec)
        return out
