"""History of multi-agent runs (started by schedules/triggers in team mode, the CLI or MCP)."""

from __future__ import annotations

from fastapi import APIRouter

from xplogent.core.config import load_config
from xplogent.memory.store import Store

router = APIRouter()


# ── Multi-agent run history ──────────────────────────────────────────────


@router.get("/runs")
async def runs() -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_runs()
    return {"runs": out, "active": []}


@router.get("/runs/{run_id}")
async def run_detail(run_id: str) -> dict:
    with Store(load_config().db_path) as store:
        info = store.get_run(run_id)
    return {"run": info, "metrics": []}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str) -> dict:
    with Store(load_config().db_path) as store:
        out = store.run_events(run_id)
    return {"events": out}


@router.get("/messages")
async def messages(run_id: str | None = None) -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_agent_messages(run_id)
    return {"messages": out}
