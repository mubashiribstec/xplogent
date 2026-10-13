"""Chat reliability: always a final answer, never a stuck run, mid-run steering."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.agent import Agent
from xplogent.core.config import load_config
from xplogent.core.events import EventBus, EventType
from xplogent.providers.base import Message, Provider, Role, StreamEvent, StreamKind
from xplogent.safety.approval import SafetyManager
from xplogent.tools.registry import ToolRegistry


def _agent(provider, **kw):
    return Agent(load_config(**kw), provider, ToolRegistry.from_config([]), SafetyManager())


@pytest.mark.asyncio
async def test_empty_reply_is_nudged_then_falls_back():
    # Model returns empty twice → nudge once, then a clear fallback (never blank).
    prov = ScriptedProvider([
        Message(role=Role.ASSISTANT, content=""),
        Message(role=Role.ASSISTANT, content=""),
    ])
    answer = await _agent(prov).run("hello?")
    assert answer.strip()
    assert "empty response" in answer


@pytest.mark.asyncio
async def test_empty_then_real_answer():
    prov = ScriptedProvider([
        Message(role=Role.ASSISTANT, content=""),            # empty → nudge
        Message(role=Role.ASSISTANT, content="here is the answer"),
    ])
    answer = await _agent(prov).run("hello?")
    assert answer == "here is the answer"


class _BoomProvider(Provider):
    name = "boom"

    def __init__(self):
        super().__init__(model="boom")

    async def stream(self, messages, tools=None, **kwargs) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("kaboom")
        yield StreamEvent(kind=StreamKind.DONE)  # pragma: no cover


@pytest.mark.asyncio
async def test_provider_crash_still_returns_and_emits_run_end():
    bus = EventBus()
    queue = bus.subscribe()
    agent = Agent(load_config(), _BoomProvider(), ToolRegistry.from_config([]),
                  SafetyManager(), bus=bus)
    answer = await agent.run("do it")           # must not raise
    await bus.close()
    assert answer  # a non-empty answer (provider error is caught in _stream_assistant)
    types = []
    while True:
        ev = await queue.get()
        if ev is None:
            break
        types.append(ev.type)
    assert EventType.RUN_END in types           # the chat is told the turn ended


@pytest.mark.asyncio
async def test_inject_is_seen_on_next_step():
    # First reply asks to continue (a tool-less but non-empty message ends the run),
    # so instead drive two steps: reply empty-with-no-tools would end; use injection
    # before the run by pre-queuing, then a normal answer.
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="ok, using your hint")])
    agent = _agent(prov)
    agent.inject("try the staging server")
    transcript: list[str] = []
    agent._drain_injections(transcript)
    assert any("staging server" in line for line in transcript)
    contents = [m.content for m in agent.stm.messages]
    assert "try the staging server" in contents


@pytest.mark.asyncio
async def test_handle_task_always_sends_done_on_error(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))

    # Force run-building to blow up so handle_task hits its error path.
    import xplogent.interfaces.api.deps as deps
    monkeypatch.setattr(deps, "build_runtime",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("build failed")))

    c = TestClient(create_app())
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "task", "task": "hi"})
        kinds = [ws.receive_json()["type"] for _ in range(2)]
        assert "error" in kinds and "done" in kinds   # never leaves the UI stuck
