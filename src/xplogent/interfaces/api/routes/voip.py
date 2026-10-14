"""Phone calling agent: carrier webhook, media stream, calls, contacts, do-not-call."""

from __future__ import annotations

import asyncio

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


@router.get("/voip/readiness")
async def voip_readiness() -> dict:
    """Checklist of what's missing before Telnyx calls can work (Calls view)."""
    from xplogent.voip.readiness import checklist

    cfg = load_config()
    items = checklist(cfg.voip or {}, cfg.model)
    return {"ready": all(i["ok"] for i in items if i["required"]), "items": items}


@router.post("/voip/telnyx/test")
async def voip_telnyx_test() -> dict:
    """Validate the saved Telnyx key (and application ID, and its webhook URL)."""
    import httpx

    from xplogent.voip.telephony.telnyx import TelnyxClient

    v = load_config().voip or {}
    client = TelnyxClient(api_key_env=str(v.get("api_key_env", "TELNYX_API_KEY")))
    if not client.has_key:
        await client.aclose()
        return {"ok": False, "error": "no Telnyx API key saved yet"}
    expected = ""
    if v.get("public_url") and v.get("webhook_secret"):
        expected = f"{str(v['public_url']).rstrip('/')}/telephony/webhook/{v['webhook_secret']}"
    try:
        await client.balance()
        out: dict = {"ok": True, "key": "valid"}
        app_id = str(v.get("connection_id") or "").strip()
        if app_id:
            app = await client.get_application(app_id)
            hook = str(app.get("webhook_event_url") or "")
            out.update(application=app.get("application_name") or app_id, webhook_in_telnyx=hook,
                       webhook_matches=bool(expected) and hook == expected)
            if expected and hook != expected:
                out["warning"] = ("the application's webhook URL in Telnyx is different from "
                                  "the one shown here; paste this one into Telnyx")
        return out
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        msg = {401: "Telnyx rejected the API key", 403: "the API key lacks permission",
               404: "no Voice API application with that ID"}.get(code, f"Telnyx answered HTTP {code}")
        return {"ok": False, "error": msg}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"cannot reach Telnyx: {type(exc).__name__}"}
    finally:
        await client.aclose()


@router.post("/voip/speech/test")
async def voip_speech_test(body: dict | None = None) -> dict:
    """Speak a test sentence with the configured voice, then (optionally) transcribe it
    back with the configured speech-to-text: checks both directions end to end."""
    import time

    from xplogent.voip.audio import resample
    from xplogent.voip.providers import build_stt, build_tts

    v = load_config().voip or {}
    sentence = "Hello, this is a voice test from your phone agent."
    out: dict = {"ok": False, "sentence": sentence}
    tts = build_tts(v.get("tts", {}) or {})
    try:
        started = time.monotonic()
        chunks = [c async for c in tts.synthesize(sentence)]
        pcm = b"".join(chunks)
        out["tts_ms"] = round((time.monotonic() - started) * 1000)
        out["audio_seconds"] = round(len(pcm) / 2 / tts.sample_rate, 2)
        if not pcm:
            out["error"] = "text-to-speech returned no audio"
            return out
    except Exception as exc:  # noqa: BLE001 - report, don't 500
        out["error"] = f"text-to-speech failed: {exc}"
        return out
    finally:
        await tts.aclose()
    out["ok"] = True
    if (body or {}).get("stt", True):
        stt = build_stt(v.get("stt", {}) or {})
        try:
            started = time.monotonic()
            out["transcript"] = await asyncio.wait_for(
                stt.transcribe(resample(pcm, tts.sample_rate, 16000)), timeout=600)
            out["stt_ms"] = round((time.monotonic() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            out["ok"] = False
            out["error"] = f"speech-to-text failed: {exc or type(exc).__name__}"
        finally:
            await stt.aclose()
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
