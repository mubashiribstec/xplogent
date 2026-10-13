"""Multi-agent orchestration and live monitoring."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from xplogent.core.config import load_config
from xplogent.core.events import EventBus
from xplogent.core.orchestrator import AgentSpec
from xplogent.interfaces.api import deps
from xplogent.interfaces.api.models import (
    OrchestrateRequest,
)
from xplogent.memory.store import Store
from xplogent.monitor.recorder import TraceRecorder

router = APIRouter()


# ── Multi-agent orchestration + deep monitoring ──────────────────────────


@router.post("/orchestrate")
async def orchestrate(req: OrchestrateRequest) -> dict:
    """Launch a multi-agent run in the background; returns its run_id.

    Connect to ``/ws/monitor?run_id=<id>`` to watch it live, or poll
    ``/runs/{id}`` and ``/agents``.
    """
    mbus = EventBus()

    # Without a human at the keyboard, confirm-tier tools would be blocked.
    # Auto-approve up to high risk so agents can actually run shell/python/web;
    # critical ops stay denied by the role policy + deny-list.
    approve = None
    if req.auto_approve:
        from xplogent.safety.approval import RiskLevel

        async def approve(reqo) -> bool:  # noqa: F811
            return reqo.risk != RiskLevel.CRITICAL

    runtime = deps.build_orchestrator(bus=mbus, approve=approve)
    orch = runtime.orchestrator
    recorder = TraceRecorder(mbus, runtime.store, orch.run_id)
    recorder.start()

    async def go() -> None:
        try:
            if req.specs:
                specs = [AgentSpec(**s.model_dump()) for s in req.specs]
                await orch.run_team(specs, max_concurrent=req.max_concurrent)
            else:
                await orch.run_goal(req.goal or "", max_concurrent=req.max_concurrent,
                                    mode=req.mode)
        finally:
            await mbus.close()
            await recorder.stop()
            await runtime.aclose()
            deps.active_runs.pop(orch.run_id, None)

    task = asyncio.create_task(go())
    deps.active_runs[orch.run_id] = {
        "orchestrator": orch, "recorder": recorder, "bus": mbus, "task": task,
    }
    return {"run_id": orch.run_id, "mode": "manual" if req.specs else req.mode}


@router.get("/runs")
async def runs() -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_runs()
    return {"runs": out, "active": list(deps.active_runs)}


@router.get("/runs/{run_id}")
async def run_detail(run_id: str) -> dict:
    with Store(load_config().db_path) as store:
        info = store.get_run(run_id)
    live = deps.active_runs.get(run_id)
    metrics = live["recorder"].snapshot() if live else []
    return {"run": info, "metrics": metrics}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str) -> dict:
    with Store(load_config().db_path) as store:
        out = store.run_events(run_id)
    return {"events": out}


@router.get("/agents")
async def agents() -> dict:
    live = []
    for rid, info in deps.active_runs.items():
        for a in info["orchestrator"].live_agents():
            live.append({"run_id": rid, **a})
    return {"agents": live}


@router.get("/messages")
async def messages(run_id: str | None = None) -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_agent_messages(run_id)
    return {"messages": out}


@router.post("/agents/{agent_id}/{action}")
async def control_agent(agent_id: str, action: str) -> dict:
    if action not in {"pause", "resume", "cancel"}:
        return {"ok": False, "error": "unknown action"}
    for info in deps.active_runs.values():
        if info["orchestrator"].control(agent_id, action):
            return {"ok": True}
    return {"ok": False, "error": "agent not found"}


@router.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket) -> None:
    if not deps.ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    run_id = websocket.query_params.get("run_id")
    info = deps.active_runs.get(run_id) if run_id else (
        next(iter(deps.active_runs.values()), None)
    )
    if not info:
        await websocket.send_json({"type": "error", "message": "no such run"})
        await websocket.close()
        return
    queue = info["bus"].subscribe()
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            await websocket.send_json({"type": event.type.value, **event.data})
    except WebSocketDisconnect:
        pass
    finally:
        info["bus"].unsubscribe(queue)
