"""Gemini + Claude-CLI providers and the registry wiring."""

from __future__ import annotations

import pytest

from xplogent.providers.base import Message, Role
from xplogent.providers.registry import available_providers, build_provider


def test_registry_includes_new_providers():
    avail = available_providers()
    assert "gemini" in avail
    assert build_provider("gemini:gemini-1.5-pro").name == "gemini"


def test_gemini_convert_roles_and_system():
    from xplogent.providers.gemini import GeminiProvider

    prov = GeminiProvider("gemini-1.5-pro", api_key="x")
    system, contents = prov._convert([
        Message(role=Role.SYSTEM, content="be terse"),
        Message(role=Role.USER, content="hi"),
        Message(role=Role.ASSISTANT, content="hello"),
        Message(role=Role.TOOL, content="42", name="calc"),
    ])
    assert system["parts"][0]["text"] == "be terse"
    assert contents[0] == {"role": "user", "parts": [{"text": "hi"}]}
    assert contents[1]["role"] == "model"
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "calc"


@pytest.mark.asyncio
async def test_gemini_stream_parses_text_and_usage(monkeypatch):
    from xplogent.providers.gemini import GeminiProvider

    sse = [
        'data: {"candidates":[{"content":{"parts":[{"text":"Hel"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":"lo"}]}}]}',
        'data: {"usageMetadata":{"promptTokenCount":11,"candidatesTokenCount":2}}',
    ]

    class _Resp:
        def raise_for_status(self): ...
        async def aiter_lines(self):
            for line in sse:
                yield line

    class _Stream:
        async def __aenter__(self): return _Resp()
        async def __aexit__(self, *a): return False

    prov = GeminiProvider("gemini-1.5-pro", api_key="x")
    monkeypatch.setattr(prov._client, "stream", lambda *a, **k: _Stream())
    final = await prov.complete([Message(role=Role.USER, content="hi")])
    assert final.content == "Hello"
    assert final.usage == {"input_tokens": 11, "output_tokens": 2}
    await prov.aclose()
