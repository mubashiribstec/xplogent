"""Chat sessions (persistent history)."""

from __future__ import annotations

from fastapi import APIRouter

from xplogent.core.config import load_config
from xplogent.interfaces.api.models import (
    RenameBody,
)
from xplogent.memory.store import Store

router = APIRouter()


# ── Chat sessions (persistent history) ───────────────────────────────────


@router.get("/sessions")
async def list_sessions() -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_sessions()
    return {"sessions": out}


@router.post("/sessions")
async def new_session() -> dict:
    with Store(load_config().db_path) as store:
        sid = store.create_session(title="chat")
    return {"id": sid}


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: int) -> dict:
    with Store(load_config().db_path) as store:
        out = store.session_messages(session_id)
    return {"messages": out}


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: int, body: RenameBody) -> dict:
    with Store(load_config().db_path) as store:
        store.rename_session(session_id, body.title)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_session(session_id)
    return {"ok": True}


@router.post("/sessions/{session_id}/undo")
async def undo_session(session_id: int, n: int = 1) -> dict:
    """Roll back the last n user→assistant exchanges in a session."""
    with Store(load_config().db_path) as store:
        removed = store.delete_last_turns(session_id, n)
    return {"ok": True, "removed": removed}
