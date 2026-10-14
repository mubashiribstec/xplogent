"""Token usage capture, context-window limits, and the USAGE event."""

from __future__ import annotations

import pytest

from xplogent.core.agent import Agent
from xplogent.core.config import load_config
from xplogent.core.events import EventBus, EventType
from xplogent.core.limits import context_window, estimate_cost
from xplogent.providers.base import Message, Role
from xplogent.safety.approval import SafetyManager
from xplogent.tools.registry import ToolRegistry


def test_context_window_lookup_and_override():
    assert context_window("anthropic:claude-sonnet-4-6") == 200000
    assert context_window("openai:gpt-4o") == 128000
    assert context_window("ollama:llama3.1") == 131072
    assert context_window("unknown:thing") == 8192  # default
    assert context_window("openai:gpt-4o", {"openai:gpt-4o": 999}) == 999
    assert context_window("x:my-model", {"my-model": 4242}) == 4242


def test_estimate_cost():
    assert estimate_cost("ollama:llama3.1", 1000, 1000) == 0.0
    # 1M in + 1M out of gpt-4o = 2.5 + 10 = 12.5
    assert estimate_cost("openai:gpt-4o", 1_000_000, 1_000_000) == pytest.approx(12.5)
    assert estimate_cost("unknown:model", 1000, 1000) == 0.0


@pytest.mark.asyncio
async def test_agent_emits_usage_event():
    from tests.conftest import ScriptedProvider

    reply = Message(role=Role.ASSISTANT, content="hi there",
                    usage={"input_tokens": 120, "output_tokens": 8})
    bus = EventBus()
    queue = bus.subscribe()
    agent = Agent(load_config(), ScriptedProvider([reply]),
                  ToolRegistry.from_config([]), SafetyManager(), bus=bus)
    await agent.run("hello")
    await bus.close()

    seen = []
    while True:
        ev = await queue.get()
        if ev is None:
            break
        if ev.type == EventType.USAGE:
            seen.append(ev.data)

    assert seen, "no USAGE event emitted"
    u = seen[0]
    assert u["input_tokens"] == 120
    assert u["output_tokens"] == 8
    assert u["context_limit"] > 0
    assert u["context_used"] > 0
    assert agent.session_tokens == {"input": 120, "output": 8}


@pytest.mark.asyncio
async def test_openai_provider_parses_usage(monkeypatch):
    from xplogent.providers.openai import OpenAIProvider

    sse = [
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        'data: {"choices":[{"delta":{"content":"!"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":3}}',
        "data: [DONE]",
    ]

    class _Resp:
        def raise_for_status(self): ...
        async def aiter_lines(self):
            for line in sse:
                yield line

    class _Stream:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return _Resp()
        async def __aexit__(self, *a): return False

    prov = OpenAIProvider(model="gpt-4o", api_key="x")
    monkeypatch.setattr(prov._client, "stream", lambda *a, **k: _Stream())
    final = await prov.complete([Message(role=Role.USER, content="hi")])
    assert final.content == "Hi!"
    assert final.usage == {"input_tokens": 42, "output_tokens": 3}
    await prov.aclose()
