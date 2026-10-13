"""Phone calling agent: carrier webhook, media stream, calls, contacts, do-not-call."""

from __future__ import annotations

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse

from xplogent.core.config import load_config
from xplogent.interfaces.api import deps
from xplogent.interfaces.api.security import token_matches
from xplogent.memory.store import Store

router = APIRouter()


# ── Phone calling agent (VoIP) ───────────────────────────────────────────


@router.post("/telephony/webhook/{secret}")
async def telephony_webhook(secret: str, request: Request):

    from xplogent.voip.service import get_service

    expected = str((load_config().voip or {}).get("webhook_secret", ""))
    if not expected or not token_matches(secret, expected):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "bad json"}, status_code=400)
    return await get_service().handle_webhook(payload)


@router.websocket("/ws/telephony/{token}")
async def ws_telephony(websocket: WebSocket, token: str) -> None:
    # The carrier can't send our dashboard token; the one-time stream token
    # (issued only after a verified webhook) authorises this socket instead.
    from xplogent.voip.service import get_service

    await get_service().run_media_socket(websocket, token)


@router.get("/voip/status")
async def voip_status() -> dict:
    from xplogent.voip.service import get_service

    cfg = load_config()
    v = cfg.voip or {}
    with Store(cfg.db_path) as store:
        out = {"enabled": bool(v.get("enabled")), "public_url": v.get("public_url", ""),
               "has_secret": bool(v.get("webhook_secret")),
               "webhook_path": "/telephony/webhook/<your webhook_secret>",
               "contacts": store.count_contacts(), "calls": len(store.list_calls(1000)),
               "active": [{"call_id": k, **val} for k, val in get_service().active.items()]}
    return out


@router.get("/voip/calls")
async def voip_calls(limit: int = 100) -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_calls(limit)
    return {"calls": out}


@router.get("/voip/calls/{call_id}")
async def voip_call(call_id: int) -> dict:
    with Store(load_config().db_path) as store:
        out = store.get_call(call_id)
    return {"call": out}


@router.get("/voip/contacts")
async def voip_contacts(q: str = "", limit: int = 500) -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_contacts(limit, q)
        total = store.count_contacts()
    return {"contacts": out, "total": total}


@router.post("/voip/contacts/import")
async def voip_import(body: dict) -> dict:
    from xplogent.voip.contacts import import_csv

    with Store(load_config().db_path) as store:
        res = import_csv(store, str(body.get("csv", "")), source=str(body.get("source", "csv")))
    deps.audit_event("contacts_import", target=f"{res.get('imported', 0)} contacts", risk="medium")
    return res


@router.delete("/voip/contacts/{contact_id}")
async def voip_delete_contact(contact_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_contact(contact_id)
    return {"ok": True}


@router.post("/voip/dnc")
async def voip_add_dnc(body: dict) -> dict:
    with Store(load_config().db_path) as store:
        store.add_dnc(str(body.get("phone", "")), str(body.get("reason", "manual")))
    return {"ok": True}
