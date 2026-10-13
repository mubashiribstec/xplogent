"""Deep monitoring: trace persistence and per-agent metrics."""

from __future__ import annotations

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.config import load_config
from xplogent.core.events import EventBus
from xplogent.core.orchestrator import AgentSpec, Orchestrator
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder
from xplogent.monitor.recorder import TraceRecorder
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.safety.approval import SafetyManager
from xplogent.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_recorder_persists_events_and_metrics(monkeypatch):
    cfg = load_config()
    bus = EventBus()
    store = Store(":memory:")
    import xplogent.core.orchestrator as orch_mod

    def provider(_spec, **_kw):
        return ScriptedProvider([
            Message(role=Role.ASSISTANT, content="",
                    tool_calls=[ToolCall(id="c1", name="broadcast",
                                         arguments={"content": "working"})]),
            Message(role=Role.ASSISTANT, content="done"),
        ])

    monkeypatch.setattr(orch_mod, "build_provider", provider)
    orch = Orchestrator(
        cfg, bus=bus, store=store, embedder=Embedder(ScriptedProvider([])),
        base_tools=ToolRegistry.from_config(["filesystem"]),
        base_safety=SafetyManager(policy={"low": "auto", "medium": "auto",
                                          "high": "auto", "critical": "deny"}),
    )
    recorder = TraceRecorder(bus, store, orch.run_id)
    recorder.start()

    await orch.run_team([AgentSpec(name="alice", role="operator", task="go")])

    await bus.close()
    await recorder._task   # drain remaining events until the close sentinel

    # events persisted under this run
    events = store.run_events(orch.run_id)
    assert any(e["type"].endswith("tool_call") for e in events)
    # per-agent metrics captured
    snap = recorder.snapshot()
    assert snap and snap[0]["name"] == "alice"
    assert snap[0]["tool_calls"] >= 1
    assert snap[0]["status"] == "done"
    await orch.aclose()


def test_api_app_builds_with_monitoring_routes():
    from xplogent.interfaces.api.routes import ALL_ROUTERS
    from xplogent.interfaces.api.server import create_app

    app = create_app()
    # HTTP routes via the public OpenAPI schema; WebSocket routes aren't in it.
    paths = set(app.openapi()["paths"])
    paths |= {r.path for router in ALL_ROUTERS for r in router.routes if hasattr(r, "path")}
    assert "/orchestrate" in paths
    assert "/runs/{run_id}/events" in paths
    assert "/ws/monitor" in paths
