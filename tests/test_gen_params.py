"""Generation controls: effort / thinking / max_tokens mapping and threading."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from xplogent.core.agent import Agent
from xplogent.core.config import load_config
from xplogent.providers.base import (
    Message,
    Provider,
    Role,
    StreamEvent,
    StreamKind,
    extract_gen_params,
    is_reasoning_effort,
)
from xplogent.providers.openai import OpenAIProvider
from xplogent.safety.approval import SafetyManager
from xplogent.tools.registry import ToolRegistry


def test_extract_gen_params():
    g = extract_gen_params({"effort": "high", "thinking": True, "max_tokens": 500, "x": 1})
    assert g == {"effort": "high", "thinking": True, "max_tokens": 500, "off": False}
    off = extract_gen_params({"effort": "off"})
    assert off["effort"] is None and off["off"] is True
    assert is_reasoning_effort("high") and not is_reasoning_effort("off")


def test_openai_reasoning_model_detection():
    assert OpenAIProvider(model="o3-mini")._is_reasoning_model()
    assert OpenAIProvider(model="gpt-5")._is_reasoning_model()
    assert not OpenAIProvider(model="gpt-4o")._is_reasoning_model()


class _Capture(Provider):
    name = "capture"

    def __init__(self):
        super().__init__("capture")
        self.last: dict | None = None

    async def stream(self, messages, tools=None, *, temperature=0.7, **kwargs) -> AsyncIterator[StreamEvent]:
        self.last = {"temperature": temperature, **kwargs}
        yield StreamEvent(kind=StreamKind.DONE, message=Message(role=Role.ASSISTANT, content="ok"))


@pytest.mark.asyncio
async def test_agent_threads_gen_params_to_provider():
    provider = _Capture()
    agent = Agent(
        load_config(), provider, ToolRegistry.from_config([]),
        SafetyManager(),
        gen_params={"effort": "high", "thinking": True, "max_tokens": 256},
    )
    await agent.run("hi")
    assert provider.last["effort"] == "high"
    assert provider.last["thinking"] is True
    assert provider.last["max_tokens"] == 256


async def test_gemini_flash_thinks_not_on_calls():
    import json

    import httpx

    from xplogent.providers.gemini import GeminiProvider

    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n\n')

    for model in ("gemini-3.6-flash", "gemini-3.6-pro"):
        p = GeminiProvider(model=model, api_key="k")
        p._client = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))
        async for _ in p.stream([Message(role=Role.USER, content="hi")], effort="off"):
            pass
    assert sent[0]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert "thinkingConfig" not in sent[1]["generationConfig"]        # Pro can't switch it off
