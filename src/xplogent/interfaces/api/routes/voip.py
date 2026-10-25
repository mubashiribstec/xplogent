"""Phone calling agent: carrier webhook, media stream, calls, contacts, do-not-call."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

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

    import json

    from xplogent.voip.service import get_service
    from xplogent.voip.telephony.telnyx import verify_signature

    v = load_config().voip or {}
    expected = str(v.get("webhook_secret", ""))
    if not expected or not token_matches(secret, expected):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    body = await request.body()
    public_key = str(v.get("telnyx_public_key") or "").strip()
    if public_key:   # with the account key set, the path secret alone is not enough
        problem = verify_signature(public_key, request.headers.get("telnyx-signature-ed25519", ""),
                                   request.headers.get("telnyx-timestamp", ""), body)
        if problem:
            deps.audit_event("voip_webhook_rejected", target=problem, risk="high", allowed=False)
            return JSONResponse({"error": "bad signature"}, status_code=403)
    try:
        payload = json.loads(body)
    except ValueError:
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
               "active": [c.summary() for c in get_service().active.values()]}
    from xplogent.voip.telephony.sip import current_line

    out["capacity"] = {line: {k: t.status()[k] for k in ("calls", "max_calls", "busy_24h", "last_busy_at")}
                       for line in ("trunk", "extension") if (t := current_line(line)) is not None}
    return out


@router.post("/voip/sip/busy/clear")
async def voip_sip_busy_clear(body: dict | None = None) -> dict:
    """Acknowledge the "callers got busy" warning for a line (or both)."""
    from xplogent.voip.telephony.sip import current_line

    lines = [str((body or {}).get("line"))] if (body or {}).get("line") else ["trunk", "extension"]
    for line in lines:
        if (t := current_line(line)) is not None:
            t.clear_busy()
    return {"ok": True}


@router.get("/voip/readiness")
async def voip_readiness() -> dict:
    """Checklist of what's missing before Telnyx calls can work (Calls view)."""
    from xplogent.voip.readiness import checklist
    from xplogent.voip.telephony.sip import current_line, current_trunk

    cfg = load_config()
    trunk = current_trunk()
    ext = current_line("extension")
    items = checklist(cfg.voip or {}, cfg.model, sip_status=trunk.status() if trunk else None,
                      sip_extension_status=ext.status() if ext else None)
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


@router.get("/voip/speech/voices")
async def voip_speech_voices(provider: str = "") -> dict:
    """Voices for the Speech card's picker (the configured TTS, or ``provider``)."""
    from xplogent.voip.providers import list_voices

    tts = dict((load_config().voip or {}).get("tts") or {})
    if provider:
        tts["provider"] = provider
    try:
        return {"ok": True, "voices": await asyncio.wait_for(list_voices(tts), timeout=15)}
    except Exception as exc:  # noqa: BLE001 - shown in the card
        return {"ok": False, "voices": [], "error": f"{type(exc).__name__}: {exc}"[:300]}


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

    def run() -> dict:            # big files: keep the event loop (and live calls) responsive
        with Store(load_config().db_path) as store:
            return import_csv(store, str(body.get("csv", "")), source=str(body.get("source", "csv")))

    res = await asyncio.to_thread(run)
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


# ── Training material (recordings, scripts, Q&A) + playbook ─────────────────
def _kick_training() -> None:
    from xplogent.voip import training

    training.worker.process = lambda item_id, cfg=None: training.process_item(
        item_id, cfg=cfg, make_provider=deps.build_provider)
    training.worker.kick()


@router.get("/voip/training")
async def voip_training() -> dict:
    from xplogent.voip import training

    with Store(load_config().db_path) as store:
        items = store.list_training_items()
    if any(i["status"] in ("queued", "processing") for i in items):
        _kick_training()            # resumes work cut off by a restart
    return {"items": items, "busy": bool(training.worker.task and not training.worker.task.done())}


@router.post("/voip/training/upload")
async def voip_training_upload(request: Request) -> JSONResponse:
    import secrets

    from xplogent.core.rag import is_sensitive
    from xplogent.voip import training

    form = await request.form(max_files=50, max_part_size=training.MAX_UPLOAD_BYTES)
    layout = str(form.get("layout") or "agent-left")
    if layout not in training.LAYOUTS:
        layout = "agent-left"
    added, rejected = [], []
    with Store(load_config().db_path) as store:
        for upload in form.getlist("files"):
            if isinstance(upload, str):
                continue
            name = training.safe_name(upload.filename or "upload")
            kind = training.classify(name)
            if not kind or is_sensitive(Path(name)):
                rejected.append({"file": name, "error": "unsupported file type"})
                continue
            dest = training.training_dir() / f"{secrets.token_hex(4)}_{name}"
            size = 0
            too_big = False
            with dest.open("wb") as fh:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > training.MAX_UPLOAD_BYTES:
                        too_big = True
                        break
                    fh.write(chunk)
            await upload.close()
            if too_big or size == 0:
                dest.unlink(missing_ok=True)
                rejected.append({"file": name, "error": "larger than 200 MB" if too_big else "empty file"})
                continue
            item_id = store.add_training_item(kind, name, str(dest), size,
                                              {"layout": layout} if kind == "recording" else {})
            added.append({"id": item_id, "file": name, "kind": kind})
    await form.close()
    if added:
        deps.audit_event("voip_training_upload", target=", ".join(a["file"] for a in added)[:300],
                         risk="low")
        _kick_training()
    return JSONResponse({"ok": bool(added), "added": added, "rejected": rejected},
                        status_code=200 if added or not rejected else 400)


@router.get("/voip/training/{item_id}")
async def voip_training_item(item_id: int) -> dict:
    with Store(load_config().db_path) as store:
        return {"item": store.get_training_item(item_id)}


@router.post("/voip/training/{item_id}/retry")
async def voip_training_retry(item_id: int, body: dict | None = None) -> dict:
    from xplogent.voip import training

    with Store(load_config().db_path) as store:
        item = store.get_training_item(item_id)
        if not item:
            return {"ok": False, "error": "no such item"}
        if item.get("doc_id"):                 # re-processing replaces the old knowledge
            store.delete_document(int(item["doc_id"]))
        opts = dict(item["options"] or {})
        if (body or {}).get("layout") in training.LAYOUTS:
            opts["layout"] = body["layout"]
        store.update_training_item(item_id, status="queued", progress=0, error="", doc_id=None,
                                   chunks=0, transcript="", options=opts)
    _kick_training()
    return {"ok": True}


@router.delete("/voip/training/{item_id}")
async def voip_training_delete(item_id: int) -> dict:
    from xplogent.voip import training

    with Store(load_config().db_path) as store:
        item = store.get_training_item(item_id)
        if item and item["status"] == "processing":
            return {"ok": False, "error": "still processing; delete it when it's done"}
        ok = training.delete_item(store, item_id)
    return {"ok": ok}


@router.get("/voip/playbook")
async def voip_playbook() -> dict:
    from xplogent.voip import training
    from xplogent.voip.service import load_playbook

    return {"content": load_playbook(), "job": training.playbook_job.snapshot()}


@router.put("/voip/playbook")
async def voip_playbook_save(body: dict) -> dict:
    from xplogent.voip import training

    text = str(body.get("content") or "")
    if len(text) > 60_000:
        return {"ok": False, "error": "the playbook is too long (max 60,000 characters)"}
    training.save_playbook(text)
    deps.audit_event("voip_playbook_saved", target=f"{len(text)} chars", risk="low")
    return {"ok": True}


@router.get("/voip/agent")
async def voip_agent() -> dict:
    """Call-agent settings at a glance: template, CRM delivery, knowledge gaps."""
    from xplogent.voip import crm
    from xplogent.voip.service import top_knowledge_gaps
    from xplogent.voip.templates import TEMPLATES

    cfg = load_config()
    c = crm.crm_config(cfg)
    return {
        "templates": [{"name": t["name"], "direction": t["direction"],
                       "fields": [f["name"] for f in t["fields"]]} for t in TEMPLATES.values()],
        "template": str((cfg.voip or {}).get("template") or ""),
        "crm": {"enabled": bool(c.get("enabled")), "base_url": str(c.get("base_url") or ""),
                "token_env": crm.token_env(cfg), "token_set": bool(os.environ.get(crm.token_env(cfg))),
                "deliveries": crm.recent_deliveries(20)},
        "knowledge_gaps": top_knowledge_gaps(20),
    }


@router.post("/voip/crm/test")
async def voip_crm_test() -> dict:
    """Send one signed sample summary to the CRM now (marked test, call_id 0)."""
    from xplogent.voip import crm

    cfg = load_config()
    if not crm.crm_enabled(cfg):
        return {"ok": False, "error": "switch CRM delivery on and set the CRM address first"}
    out = await crm.deliver(cfg, {"event": "call.test", "call_id": 0, "test": True,
                                  "summary": "Xplogent test delivery", "transcript": []},
                            attempts=1)
    deps.audit_event("voip_crm_test", target=out["status"], risk="low")
    return {"ok": out["status"] == "sent", "status": out["status"], "detail": out["detail"]}


@router.post("/voip/playbook/build")
async def voip_playbook_build() -> dict:
    from xplogent.voip import training

    job = training.playbook_job
    if job.task is not None and not job.task.done():
        return {"ok": True, "job": job.snapshot()}
    cfg = load_config()
    spec = (cfg.voip or {}).get("model") or cfg.model

    async def run() -> None:
        provider = deps.build_provider(spec)
        try:
            job.draft = await training.build_playbook(cfg, provider, job=job)
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            job.status, job.error = "error", str(exc)[:500]
        finally:
            await provider.aclose()

    job.reset()
    job.status = "running"
    job.task = asyncio.create_task(run())
    return {"ok": True, "job": job.snapshot()}


# ── Live call control, manual calls, test calls ──────────────────────────────
def _live(call_id: int):
    from xplogent.voip.service import get_service

    return get_service().active.get(call_id)


def _gone() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "that call has ended"}, status_code=404)


@router.post("/voip/calls/{call_id}/hangup")
async def voip_call_hangup(call_id: int):
    live = _live(call_id)
    if live is None:
        return _gone()
    await live.hangup()
    deps.audit_event("voip_call_hangup", target=str(call_id), risk="low")
    return {"ok": True}


@router.post("/voip/calls/{call_id}/transfer")
async def voip_call_transfer(call_id: int, body: dict | None = None):
    from xplogent.voip.control import resolve_transfer_number

    live = _live(call_id)
    if live is None:
        return _gone()
    number = resolve_transfer_number(load_config().voip or {}, str((body or {}).get("to") or ""),
                                     transport=live.transport)
    if not number:
        return {"ok": False, "error": "no valid transfer number (set the hand-off number or enter one)"}
    try:
        await live.transfer(number)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}
    deps.audit_event("voip_call_transfer", target=f"{call_id} -> {number}", risk="medium")
    return {"ok": True, "to": number}


@router.post("/voip/calls/{call_id}/say")
async def voip_call_say(call_id: int, body: dict):
    live = _live(call_id)
    if live is None:
        return _gone()
    text = str(body.get("text") or "").strip()[:1000]
    if not text:
        return {"ok": False, "error": "nothing to say"}
    await live.say(text)
    deps.audit_event("voip_call_say", target=f"{call_id}: {text[:80]}", risk="low")
    return {"ok": True}


@router.post("/voip/calls/{call_id}/instruct")
async def voip_call_instruct(call_id: int, body: dict):
    live = _live(call_id)
    if live is None:
        return _gone()
    text = str(body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty instruction"}
    live.instruct(text)
    return {"ok": True, "notes": list(live.brain.notes)}


@router.post("/voip/calls/{call_id}/pause-ai")
async def voip_call_pause(call_id: int, body: dict | None = None):
    live = _live(call_id)
    if live is None:
        return _gone()
    paused = bool((body or {}).get("paused", not live.brain.paused))
    live.pause(paused)
    return {"ok": True, "paused": paused}


@router.post("/voip/dial")
async def voip_dial(body: dict):
    """Manual call (or "call me" test call) to a number or a contact."""
    from xplogent.voip.service import get_service

    default_via = "sip" if (load_config().voip or {}).get("telephony") == "sip" else "telnyx"
    via = str(body.get("via") or default_via)
    if via not in ("telnyx", "sip", "extension"):
        return {"ok": False, "error": "via must be telnyx, sip or extension"}
    contact_id = body.get("contact_id")
    if contact_id not in (None, ""):
        try:
            contact_id = int(contact_id)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "contact_id must be a number"}, status_code=400)
    number = str(body.get("number") or "")
    if contact_id and not number:
        with Store(load_config().db_path) as store:
            contact = store.get_contact(int(contact_id))
        number = (contact or {}).get("phone", "")
    res = await get_service().dial(
        number, contact_id=int(contact_id) if contact_id else None, test=bool(body.get("test")),
        via=via, ignore_hours=bool(body.get("ignore_hours")))
    deps.audit_event("voip_dial", target=f"{number} via {via}{' (test)' if body.get('test') else ''}",
                     risk="medium", allowed=bool(res.get("ok")), detail=str(res.get("error", ""))[:200])
    return res


@router.websocket("/ws/voip/test")
async def ws_voip_test(websocket: WebSocket) -> None:
    from xplogent.voip.testcall import run_test_call

    if not deps.ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    await run_test_call(websocket, provider=_test_provider(), retrieve=None)


def _test_provider():
    """The call model, through deps so tests can script it (None: build_session decides)."""
    cfg = load_config()
    return deps.build_provider((cfg.voip or {}).get("model") or cfg.model)


# ── SIP trunk (built-in client) ──────────────────────────────────────────────
_SIP_FIELDS = {"server": str, "port": int, "username": str, "auth_username": str, "domain": str,
               "caller_id": str, "public_ip": str, "local_port": int, "rtp_start": int, "rtp_end": int,
               "register": bool, "expires": int, "max_calls": int, "strict_source": bool,
               "ring_timeout": int}


def _extension_status() -> dict:
    from xplogent.voip.telephony.sip import current_line

    v = load_config().voip or {}
    ext = v.get("sip_extension") or {}
    trunk = current_line("extension")
    base = {"enabled": bool(ext.get("enabled")),
            "configured": bool(ext.get("server") and ext.get("username")),
            "password_set": bool(os.environ.get("SIP_EXTENSION_PASSWORD"))}
    if trunk is None:
        return {**base, "running": False, "registered": False,
                "error": "" if base["configured"] else "not set up"}
    return {**base, **trunk.status()}


def _sip_status() -> dict:
    from xplogent.voip.telephony.sip import current_trunk, wants_sip

    v = load_config().voip or {}
    trunk = current_trunk()
    base = {"telephony": v.get("telephony") or "telnyx", "configured": wants_sip(v),
            "password_set": bool(os.environ.get("SIP_PASSWORD"))}
    if trunk is None:
        out = {**base, "running": False, "registered": False,
               "error": "" if base["configured"] else "not set up"}
    else:
        out = {**base, **trunk.status()}
    out["extension"] = _extension_status()
    return out


@router.get("/voip/sip/status")
async def voip_sip_status() -> dict:
    return _sip_status()


def _sip_updates(body: dict, fields: dict) -> tuple[dict, str]:
    """Validate a SIP settings body (shared by the trunk and extension cards).
    Returns (updates, error): error is "" when the updates are usable."""
    updates: dict = {}
    for key, kind in fields.items():
        if key not in body:
            continue
        try:
            updates[key] = kind(body[key]) if kind is not bool else bool(body[key])
        except (TypeError, ValueError):
            return {}, f"{key} must be a {kind.__name__}"
        if kind is str:
            updates[key] = updates[key].strip()
    for key in ("allow_from", "queue_numbers"):
        if key in body:
            raw = body[key]
            updates[key] = [str(a).strip() for a in (raw if isinstance(raw, list) else str(raw).split(","))
                            if str(a).strip()]
    if any(not re.fullmatch(r"\+?[0-9*#]{1,20}", q) for q in updates.get("queue_numbers", [])):
        return {}, "queue numbers must be extension/queue numbers like 600"
    host = updates.get("server", "")
    if host and not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
        return {}, "server must be a host name or IP (no sip:, no port)"
    for key in ("port", "local_port", "rtp_start", "rtp_end"):
        if key in updates and not 1 <= updates[key] <= 65535:
            return {}, f"{key} must be a port number"
    if updates.get("rtp_start", 0) and updates.get("rtp_end", 0) and updates["rtp_end"] <= updates["rtp_start"]:
        return {}, "the RTP range end must be above its start"
    return updates, ""


@router.post("/voip/sip/settings")
async def voip_sip_settings(body: dict) -> dict:
    """Save the SIP account (password goes to the encrypted secrets) and restart the trunk."""
    from xplogent.core.config import save_env, save_user_config
    from xplogent.voip.telephony import sip as sip_mod

    updates, error = _sip_updates(body, _SIP_FIELDS)
    if error:
        return {"ok": False, "error": error}
    patch: dict = {"sip": updates}
    if body.get("telephony") in ("telnyx", "sip", "both"):
        patch["telephony"] = body["telephony"]
    save_user_config({"voip": patch})
    if body.get("password"):
        save_env({"SIP_PASSWORD": str(body["password"])})
    deps.audit_event("voip_sip_settings", target=f"{updates.get('username', '')}@{updates.get('server', '')}",
                     risk="medium")
    await sip_mod.apply_settings()
    return {"ok": True, "status": _sip_status()}


@router.post("/voip/sip/extension/settings")
async def voip_sip_extension_settings(body: dict) -> dict:
    """Save the PBX extension login (its own secret) and rebuild ONLY that line."""
    from xplogent.core.config import save_env, save_user_config
    from xplogent.voip.telephony import sip as sip_mod

    updates, error = _sip_updates(body, {**_SIP_FIELDS, "enabled": bool})
    if error:
        return {"ok": False, "error": error}
    save_user_config({"voip": {"sip_extension": updates}})
    if body.get("password"):
        save_env({"SIP_EXTENSION_PASSWORD": str(body["password"])})
    deps.audit_event("voip_sip_extension_settings",
                     target=f"{updates.get('username', '')}@{updates.get('server', '')}", risk="medium")
    await sip_mod.apply_settings(line="extension")
    return {"ok": True, "status": _sip_status()["extension"]}


@router.post("/voip/sip/extension/test")
async def voip_sip_extension_test() -> dict:
    """Restart the extension line and wait for the registration result."""
    from xplogent.voip.telephony import sip as sip_mod

    trunk = await sip_mod.apply_settings(line="extension")
    if trunk is None:
        return {"ok": False, "error": "switch the extension line on and enter the PBX address "
                                      "and extension number first",
                "status": _sip_status()["extension"]}
    if not trunk.started:
        return {"ok": False, "error": trunk.reg_error, "status": _sip_status()["extension"]}
    if not trunk.s.register:
        return {"ok": True, "status": _sip_status()["extension"],
                "note": "registration is off: calls are accepted from the server"}
    ok = await trunk.wait_registered(10.0)
    st = _sip_status()["extension"]
    return {"ok": ok, "status": st, "error": "" if ok else (st.get("error") or "no answer from the server")}


@router.post("/voip/sip/test")
async def voip_sip_test() -> dict:
    """Restart the trunk and wait for the registration result."""
    from xplogent.voip.telephony import sip as sip_mod

    trunk = await sip_mod.apply_settings()
    if trunk is None:
        return {"ok": False, "error": "choose SIP trunk (or both) and enter the server and username first",
                "status": _sip_status()}
    if not trunk.started:
        return {"ok": False, "error": trunk.reg_error, "status": _sip_status()}
    if not trunk.s.register:
        return {"ok": True, "status": _sip_status(), "note": "registration is off: calls are accepted from the server"}
    ok = await trunk.wait_registered(10.0)
    st = _sip_status()
    return {"ok": ok, "status": st, "error": "" if ok else (st.get("error") or "no answer from the server")}


@router.get("/voip/sip/detect-ip")
async def voip_sip_detect_ip() -> dict:
    """This server's LAN address (for a PBX on the same network) and its internet address."""
    import os
    import socket

    import httpx

    lan = os.environ.get("XPLOGENT_HOST_IP", "")
    if not lan:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 9))
                lan = s.getsockname()[0]
        except OSError:
            lan = ""
    internet = ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://api.ipify.org")
            if r.status_code == 200 and re.fullmatch(r"[0-9.]+", r.text.strip()):
                internet = r.text.strip()
    except httpx.HTTPError:
        pass
    in_docker = bool(os.environ.get("XPLOGENT_IN_DOCKER")) and not os.environ.get("XPLOGENT_HOST_IP")
    return {"lan": lan, "internet": internet,
            "note": "inside Docker the LAN address is the container's: run `xplogent-docker sip on` "
                    "so the host address is known" if in_docker else ""}


# ── Call recordings ──────────────────────────────────────────────────────────
def _recording_path(call_id: int) -> Path | None:
    from xplogent.voip.recording import recordings_dir

    with Store(load_config().db_path) as store:
        call = store.get_call(call_id) or {}
    path = Path(call.get("recording") or "")
    # Only ever serve files from the recordings folder.
    if not call.get("recording") or not path.is_file() or recordings_dir().resolve() not in path.resolve().parents:
        return None
    return path


@router.get("/voip/calls/{call_id}/recording")
async def voip_call_recording(call_id: int):
    from fastapi.responses import FileResponse

    path = _recording_path(call_id)
    if path is None:
        return JSONResponse({"error": "no recording for this call"}, status_code=404)
    return FileResponse(path, media_type="audio/wav", filename=f"call-{call_id}.wav")


@router.delete("/voip/calls/{call_id}/recording")
async def voip_call_recording_delete(call_id: int) -> dict:
    path = _recording_path(call_id)
    if path is not None:
        path.unlink(missing_ok=True)
    with Store(load_config().db_path) as store:
        store.set_call_recording(call_id, "")
    deps.audit_event("voip_recording_deleted", target=str(call_id), risk="low")
    return {"ok": True}


@router.post("/voip/calls/{call_id}/recording/train")
async def voip_call_recording_train(call_id: int) -> dict:
    """Add a recorded call to the training material (caller left, agent right)."""
    import shutil

    from xplogent.voip import training

    path = _recording_path(call_id)
    if path is None:
        return {"ok": False, "error": "no recording for this call"}
    dest = training.training_dir() / f"call-{call_id}.wav"
    await asyncio.to_thread(shutil.copyfile, path, dest)
    with Store(load_config().db_path) as store:
        item_id = store.add_training_item("recording", f"call-{call_id}.wav", str(dest),
                                          dest.stat().st_size, {"layout": "agent-right"})
    _kick_training()
    return {"ok": True, "item_id": item_id}
