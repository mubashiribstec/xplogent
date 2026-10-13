"""Scheduler, triggers (webhook + file), visual workflows, evals."""

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


# ── Visual workflows ─────────────────────────────────────────────────────


@router.get("/workflows")
async def list_workflows() -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_workflows()
    return {"workflows": out}


@router.post("/workflows")
async def save_workflow(body: dict) -> dict:
    with Store(load_config().db_path) as store:
        wid = store.save_workflow(str(body.get("name", "workflow")),
                                  body.get("graph", {}), body.get("id"))
        out = store.get_workflow(wid)
    return {"ok": True, "workflow": out}


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_workflow(workflow_id)
    return {"ok": True}


@router.post("/workflows/{workflow_id}/run")
async def run_workflow_ep(workflow_id: int) -> dict:
    from xplogent.core.workflow import run_workflow

    with Store(load_config().db_path) as store:
        wf = store.get_workflow(workflow_id)
    if not wf:
        return {"ok": False, "error": "workflow not found"}
    deps.audit_event("workflow_run", target=wf.get("name", str(workflow_id)))
    return await run_workflow(wf["graph"])


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


# ── Evals ────────────────────────────────────────────────────────────────


@router.get("/evals")
async def list_evals() -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_evals()
    return {"evals": out}


@router.post("/evals")
async def save_eval(body: dict) -> dict:
    with Store(load_config().db_path) as store:
        eid = store.upsert_eval(str(body.get("name", "suite")), str(body.get("description", "")))
        if isinstance(body.get("cases"), list):
            store.set_eval_cases(eid, body["cases"])
        out = next((e for e in store.list_evals() if e["id"] == eid), None)
    return {"ok": True, "eval": out}


@router.delete("/evals/{eval_id}")
async def delete_eval(eval_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_eval(eval_id)
    return {"ok": True}


@router.post("/evals/{eval_id}/run")
async def run_eval(eval_id: int) -> dict:
    from xplogent.core.evals import run_suite
    try:
        return await run_suite(eval_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.post("/evals/{eval_id}/ab")
async def run_eval_ab(eval_id: int, body: dict) -> dict:
    """A/B-test a suite across prompt/model variants and return a winner."""
    from xplogent.core.evals import run_ab
    variants = body.get("variants") or []
    if len(variants) < 2:
        return {"ok": False, "error": "provide at least two variants"}
    try:
        res = await run_ab(eval_id, variants)
        return {"ok": True, **res}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.post("/evals/promote")
async def promote_eval(body: dict) -> dict:
    """Promote a winning variant's prompt/model to the live config."""
    from xplogent.core.evals import promote_variant
    out = promote_variant(str(body.get("system_prompt", "")), str(body.get("model", "")))
    deps.audit_event("eval_promote", target=",".join(out.get("promoted", [])), risk="medium")
    return out
