"""Gemini + Claude-CLI providers and the registry wiring."""

from __future__ import annotations

import asyncio

import pytest

from xplogent.providers.base import Message, Role
from xplogent.providers.registry import available_providers, build_provider


def test_registry_includes_new_providers():
    avail = available_providers()
    assert "gemini" in avail
    assert "claude-cli" in avail
    assert build_provider("gemini:gemini-1.5-pro").name == "gemini"
    assert build_provider("claude-cli:sonnet").model == "sonnet"


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


class _FakeStdout:
    def __init__(self, lines): self._lines = [ln.encode() + b"\n" for ln in lines]
    def __aiter__(self): return self
    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)
    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeStdin:
    def write(self, _b): ...
    async def drain(self): ...
    def close(self): ...


class _FakeProc:
    returncode = 0
    def __init__(self, lines):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.stderr = self
    async def read(self): return b""
    async def wait(self): return 0


@pytest.mark.asyncio
async def test_claude_cli_stream_parses_assistant_and_usage(monkeypatch):
    from xplogent.providers import claude_cli

    lines = [
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Hi from claude"}]}}',
        '{"type":"result","usage":{"input_tokens":50,"output_tokens":4},"result":"Hi from claude"}',
    ]
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _x: "/usr/bin/claude")

    async def fake_exec(*a, **k):
        return _FakeProc(lines)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    prov = claude_cli.ClaudeCLIProvider("sonnet")
    final = await prov.complete([Message(role=Role.USER, content="hello")])
    assert "Hi from claude" in final.content
    assert final.usage == {"input_tokens": 50, "output_tokens": 4}


class _HangingProc:
    """A CLI that never writes anything: must be killed on timeout / cancel."""

    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = self
        self.stderr = self
        self.returncode = None
        self.killed = False
        self._exit = asyncio.Event()

    async def readline(self):
        await self._exit.wait()
        return b""

    async def read(self):
        await self._exit.wait()
        return b""

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._exit.set()

    async def wait(self):
        await self._exit.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_claude_cli_timeout_kills_process(monkeypatch):
    from xplogent.providers import claude_cli

    proc = _HangingProc()
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _x: "/usr/bin/claude")

    async def fake_exec(*a, **k):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    prov = claude_cli.ClaudeCLIProvider("sonnet", timeout=0.05)
    with pytest.raises(TimeoutError):
        await prov.complete([Message(role=Role.USER, content="hello")])
    assert proc.killed


@pytest.mark.asyncio
async def test_claude_cli_cancel_kills_process(monkeypatch):
    from xplogent.providers import claude_cli

    proc = _HangingProc()
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _x: "/usr/bin/claude")

    async def fake_exec(*a, **k):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    prov = claude_cli.ClaudeCLIProvider("sonnet")
    task = asyncio.create_task(prov.complete([Message(role=Role.USER, content="hello")]))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed


@pytest.mark.asyncio
async def test_claude_cli_missing_binary(monkeypatch):
    from xplogent.providers import claude_cli

    monkeypatch.setattr(claude_cli.shutil, "which", lambda _x: None)
    prov = claude_cli.ClaudeCLIProvider("sonnet")
    final = await prov.complete([Message(role=Role.USER, content="hello")])
    assert "claude" in final.content.lower()
