"""Scheduler and triggers (webhook + file)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from xplogent.core.config import load_config
from xplogent.interfaces.api import deps
from xplogent.interfaces.api.models import (
    ScheduleBody,
)
from xplogent.interfaces.api.security import token_matches
from xplogent.memory.store import Store

router = APIRouter()


# ── Scheduler (recurring / timed jobs) ───────────────────────────────────


@router.get("/schedules")
async def list_schedules() -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_schedules()
    return {"schedules": out}


@router.post("/schedules")
async def add_schedule(body: ScheduleBody) -> dict:
    from xplogent.core.scheduler import parse_schedule

    try:
        spec, next_run = parse_schedule(body.schedule, body.tz)
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with Store(load_config().db_path) as store:
        sid = store.add_schedule(body.name or body.prompt[:40], body.prompt,
                                 body.mode, spec, body.tz, next_run)
    return {"ok": True, "id": sid, "spec": spec, "next_run": next_run}


@router.post("/schedules/{schedule_id}/toggle")
async def toggle_schedule(schedule_id: int) -> dict:
    with Store(load_config().db_path) as store:
        job = store.get_schedule(schedule_id)
        if not job:
            store.close()
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="no such schedule")
        store.set_schedule_enabled(schedule_id, not job["enabled"])
    return {"ok": True, "enabled": not job["enabled"]}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_schedule(schedule_id)
    return {"ok": True}


# ── Proactive triggers (webhook + file-watch) ────────────────────────────


@router.get("/triggers")
async def list_triggers() -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_triggers()
    return {"triggers": out}


@router.post("/triggers")
async def create_trigger(body: dict) -> dict:
    ttype = str(body.get("type", "webhook"))
    spec = str(body.get("spec", ""))
    if ttype == "webhook":
        # Always a random, unguessable secret: the URL is the only credential.
        import secrets as _pysecrets

        spec = _pysecrets.token_hex(24)
    with Store(load_config().db_path) as store:
        tid = store.add_trigger(str(body.get("name", "trigger")), ttype, spec,
                                str(body.get("prompt", "")), str(body.get("mode", "agent")))
        out = store.get_trigger(tid)
    deps.audit_event("trigger_create", target=str(body.get("name", "")), risk="medium")
    return {"ok": True, "trigger": out}


@router.post("/triggers/{trigger_id}/toggle")
async def toggle_trigger(trigger_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.toggle_trigger(trigger_id)
    return {"ok": True}


@router.delete("/triggers/{trigger_id}")
async def delete_trigger(trigger_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_trigger(trigger_id)
    return {"ok": True}


@router.post("/triggers/webhook/{token}")
async def fire_webhook(token: str, request: Request) -> dict:
    cfg = load_config()
    with Store(cfg.db_path) as store:
        trig = store.get_trigger_by_token(token)
    if not trig or not trig.get("enabled") or not token_matches(token, str(trig.get("spec", ""))):
        return JSONResponse({"error": "unknown or disabled webhook"}, status_code=404)
    if int(request.headers.get("content-length") or 0) > deps.WEBHOOK_MAX_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    if trig["id"] in deps.webhook_running:
        # One run per trigger at a time: a flood of POSTs can't start (and pay for) many.
        return JSONResponse({"error": "this trigger is already running"}, status_code=429)
    try:
        body = (await request.body())[:deps.WEBHOOK_MAX_BYTES].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        body = ""
    from xplogent.core.triggers import run_trigger

    async def _go() -> None:
        deps.webhook_running.add(trig["id"])
        try:
            await run_trigger(trig, context=body, config=cfg)
        finally:
            deps.webhook_running.discard(trig["id"])

    # Fire-and-forget so the caller gets a fast reply; keep a reference so the task
    # can't be garbage-collected mid-run.
    task = asyncio.create_task(_go())
    deps.bg_tasks.add(task)
    task.add_done_callback(deps.bg_tasks.discard)
    return {"ok": True, "fired": trig["name"]}
