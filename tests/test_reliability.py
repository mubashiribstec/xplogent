"""Regression tests for the correctness/reliability fixes (audit phase 2)."""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.config import load_config
from xplogent.core.context import ShortTermMemory
from xplogent.core.events import EventBus
from xplogent.interfaces.api.wschannel import WsChannel
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder
from xplogent.providers.anthropic import AnthropicProvider
from xplogent.providers.base import Message, Provider, Role, ToolCall
from xplogent.providers.gemini import GeminiProvider
from xplogent.safety.approval import ApprovalRequest, RiskLevel, SafetyManager
from xplogent.tools.registry import ToolRegistry

_AUTO = {"low": "auto", "medium": "auto", "high": "auto", "critical": "deny"}


# ── context trimming ──────────────────────────────────────────────────────────
def _assert_paired(msgs: list[Message]) -> None:
    open_ids: set[str] = set()
    for m in msgs:
        if m.role == Role.ASSISTANT:
            open_ids = {tc.id for tc in m.tool_calls}
        elif m.role == Role.TOOL:
            assert m.tool_call_id in open_ids, "tool result without its tool call"


def test_trim_never_orphans_tool_results():
    stm = ShortTermMemory(max_tokens=300)
    stm.add(Message(role=Role.USER, content="the original task " * 5))
    for i in range(30):
        stm.add(Message(role=Role.ASSISTANT, content="thinking " * 10,
                        tool_calls=[ToolCall(id=f"a{i}", name="x"), ToolCall(id=f"b{i}", name="y")]))
        stm.add(Message(role=Role.TOOL, content="out " * 40, tool_call_id=f"a{i}", name="x"))
        stm.add(Message(role=Role.TOOL, content="out " * 40, tool_call_id=f"b{i}", name="y"))
        stm.trim()
        rendered = stm.render()
        _assert_paired(rendered)
        assert all(m.role != Role.TOOL for m in rendered[:1])
    # The task itself survives trimming.
    assert any("the original task" in (m.content or "") for m in stm.render())


def test_huge_tool_output_is_capped():
    stm = ShortTermMemory(max_tokens=1000)
    stm.add(Message(role=Role.TOOL, content="A" * 50_000 + "TAIL", tool_call_id="1"))
    content = stm.messages[-1].content
    assert len(content) < 5000 and content.endswith("TAIL") and "truncated" in content


# ── provider wire formats ─────────────────────────────────────────────────────
def test_anthropic_convert_thinking_and_grouped_results():
    p = AnthropicProvider("claude-x", api_key="k")
    thinking = {"type": "thinking", "thinking": "hmm", "signature": "sig"}
    msgs = [
        Message(role=Role.SYSTEM, content="sys"),
        Message(role=Role.USER, content="hi"),
        Message(role=Role.ASSISTANT, content="", thinking_blocks=[thinking],
                tool_calls=[ToolCall(id="t1", name="a", arguments={}),
                            ToolCall(id="t2", name="b", arguments={})]),
        Message(role=Role.TOOL, content="r1", tool_call_id="t1"),
        Message(role=Role.TOOL, content="", tool_call_id="t2"),
        Message(role=Role.ASSISTANT, content=""),
        Message(role=Role.USER, content="again"),
    ]
    system, out = p._convert(msgs)
    assert system == "sys"
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant", "user"]
    assert out[1]["content"][0] == thinking                       # signed block first
    results = out[2]["content"]
    assert [b["tool_use_id"] for b in results] == ["t1", "t2"]    # one user turn
    assert results[1]["content"] == "(no output)"
    assert out[3]["content"] == [{"type": "text", "text": "(no reply)"}]  # never empty


@pytest.mark.asyncio
async def test_anthropic_stream_error_event_raises(monkeypatch):
    p = AnthropicProvider("claude-x", api_key="k")
    lines = ['data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}',
             'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}']

    class _Resp:
        def raise_for_status(self): ...
        async def aiter_lines(self):
            for ln in lines:
                yield ln

    class _Stream:
        async def __aenter__(self): return _Resp()
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(p._client, "stream", lambda *a, **k: _Stream())
    with pytest.raises(RuntimeError, match="overloaded"):
        await p.complete([Message(role=Role.USER, content="hi")])
    await p.aclose()


def test_gemini_groups_parallel_results_and_echoes_signature():
    p = GeminiProvider("gemini-x", api_key="k")
    msgs = [
        Message(role=Role.USER, content="hi"),
        Message(role=Role.ASSISTANT, content="",
                tool_calls=[ToolCall(id="1", name="a", arguments={"q": 1}),
                            ToolCall(id="2", name="b", arguments={})],
                thinking_blocks=[{"call": 0, "thoughtSignature": "SIG"}]),
        Message(role=Role.TOOL, content="ra", name="a", tool_call_id="1"),
        Message(role=Role.TOOL, content="rb", name="b", tool_call_id="2"),
    ]
    _system, contents = p._convert(msgs)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    model_parts = contents[1]["parts"]
    assert model_parts[0]["thoughtSignature"] == "SIG"
    assert "thoughtSignature" not in model_parts[1]
    assert [part["functionResponse"]["name"] for part in contents[2]["parts"]] == ["a", "b"]


# ── websocket channel ─────────────────────────────────────────────────────────
class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail = False

    async def send_json(self, payload):
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append(payload)


def _req() -> ApprovalRequest:
    return ApprovalRequest(tool="shell", arguments={"command": "ls"},
                           risk=RiskLevel.HIGH, reason="test")


@pytest.mark.asyncio
async def test_ws_close_denies_pending_approval_and_cancels_tasks():
    ws = _FakeWs()
    chan = WsChannel(ws)
    approval = asyncio.create_task(chan.approve(_req()))
    stopped = asyncio.Event()

    async def run_forever():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    chan.spawn(run_forever())
    await asyncio.sleep(0.01)
    assert ws.sent and ws.sent[0]["type"] == "approval_required"
    started = time.monotonic()
    await chan.close()
    assert await asyncio.wait_for(approval, 1) is False
    assert time.monotonic() - started < 1
    assert stopped.is_set()
    assert await chan.send({"type": "x"}) is False   # no-op after close


@pytest.mark.asyncio
async def test_ws_send_failure_marks_closed_and_denies():
    ws = _FakeWs()
    ws.fail = True
    chan = WsChannel(ws)
    assert await chan.approve(_req()) is False
    assert chan.closed


# ── embeddings ────────────────────────────────────────────────────────────────
class _CountingEmbedProvider(Provider):
    name = "emb"

    def __init__(self, fail_times: int) -> None:
        super().__init__("emb")
        self.fail_times = fail_times
        self.calls = 0

    async def stream(self, *a, **k):  # pragma: no cover - unused
        yield

    async def embed(self, texts):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("ollama still loading")
        return [[1.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_embedder_survives_a_single_failure():
    emb = Embedder(_CountingEmbedProvider(fail_times=1))
    assert await emb.embed_one("x") == []
    assert emb.available
    assert await emb.embed_one("x") == [1.0, 0.0]


@pytest.mark.asyncio
async def test_embedder_retries_after_cooldown(monkeypatch):
    prov = _CountingEmbedProvider(fail_times=Embedder.FAIL_LIMIT)
    emb = Embedder(prov)
    for _ in range(Embedder.FAIL_LIMIT):
        await emb.embed_one("x")
    assert not emb.available
    assert await emb.embed_one("x") == []           # cooling down: provider not called
    assert prov.calls == Embedder.FAIL_LIMIT
    monkeypatch.setattr(Embedder, "RETRY_AFTER", 0.0)
    emb._retry_at = time.monotonic() - 1
    assert await emb.embed_one("x") == [1.0, 0.0]
    assert emb.available


@pytest.mark.asyncio
async def test_rag_backfills_chunks_ingested_without_vectors():
    from xplogent.core.rag import hybrid_search, ingest_text

    prov = _CountingEmbedProvider(fail_times=1)
    emb = Embedder(prov)
    store = Store(":memory:")
    await ingest_text(store, emb, "the warranty lasts two years", title="faq")
    assert store.chunks_missing_embeddings()             # embed failed at ingest
    await hybrid_search(store, emb, "warranty")
    assert store.chunks_missing_embeddings() == []       # filled on the next search
    store.close()


# ── process cleanup ───────────────────────────────────────────────────────────
@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_shell_timeout_kills_child_processes(tmp_path):
    from xplogent.core.backends import LocalBackend

    pidfile = tmp_path / "pid"
    cmd = f"{sys.executable} -c 'import os,time; open(\"{pidfile}\",\"w\").write(str(os.getpid())); time.sleep(30)' & wait"
    started = time.monotonic()
    rc, _out, err = await LocalBackend().run(cmd, timeout=1)
    assert rc == 124 and "timed out" in err
    assert time.monotonic() - started < 10
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("grandchild survived the timeout")


# ── small fixes ───────────────────────────────────────────────────────────────


def test_install_root_ignores_unrelated_projects(tmp_path, monkeypatch):
    from xplogent.core import config

    proj = tmp_path / "someone-elses-project"
    pkg = proj / ".venv" / "lib" / "python3.11" / "site-packages" / "xplogent" / "core"
    pkg.mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname='other'\n")
    monkeypatch.setattr(config, "__file__", str(pkg / "config.py"))
    assert config.install_root() is None

    checkout = tmp_path / "xplogent-src"
    src = checkout / "src" / "xplogent" / "core"
    src.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("")
    monkeypatch.setattr(config, "__file__", str(src / "config.py"))
    assert config.install_root() == checkout.resolve()


def test_budget_midnight_is_start_of_today():
    from datetime import datetime

    from xplogent.core.budget import _local_midnight

    expected = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    assert _local_midnight() == pytest.approx(expected, abs=1)


@pytest.mark.asyncio
async def test_inject_does_not_unpause_and_bad_args_emit_result():
    from xplogent.core.agent import Agent
    from xplogent.core.events import EventType
    from xplogent.tools.base import Tool, ToolResult

    class _Echo(Tool):
        name = "echo"
        description = "echo"
        parameters = {"type": "object", "properties": {"x": {"type": "string"}}}
        risk = RiskLevel.LOW

        async def run(self, x: str) -> ToolResult:
            return ToolResult.success(x)

    bus = EventBus()
    tools = ToolRegistry()
    tools.register(_Echo())
    agent = Agent(load_config(), ScriptedProvider([]), tools,
                  SafetyManager(policy=dict(_AUTO)), bus=bus)
    agent.pause()
    agent.inject("try another way")
    assert not agent._pause.is_set()

    seen: list = []

    async def collect():
        async for ev in bus.stream():
            seen.append(ev)

    consumer = asyncio.create_task(collect())
    await asyncio.sleep(0.01)  # subscribe before the tool runs
    text = await agent._run_tool("echo", {"wrong": 1})
    await asyncio.sleep(0.01)
    await bus.close()
    await consumer
    assert text.startswith("ERROR: bad arguments")
    assert any(ev.type == EventType.TOOL_RESULT and not ev.data.get("ok") for ev in seen)
