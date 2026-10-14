"""Phone calling agent: carrier webhook, media stream, calls, contacts, do-not-call."""

from __future__ import annotations

import asyncio
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
               "active": [c.summary() for c in get_service().active.values()]}
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
    from xplogent.voip.control import clean_number

    live = _live(call_id)
    if live is None:
        return _gone()
    raw = str((body or {}).get("to") or (load_config().voip or {}).get("transfer_number") or "")
    number = clean_number(raw) or (raw.strip() if raw.strip().lower().startswith("sip:") else "")
    if not number:
        return {"ok": False, "error": "no transfer number (set the hand-off number or enter one)"}
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
async def voip_dial(body: dict) -> dict:
    """Manual call (or "call me" test call) to a number or a contact."""
    from xplogent.voip.service import get_service

    default_via = "sip" if (load_config().voip or {}).get("telephony") == "sip" else "telnyx"
    via = str(body.get("via") or default_via)
    if via not in ("telnyx", "sip"):
        return {"ok": False, "error": "via must be telnyx or sip"}
    contact_id = body.get("contact_id")
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
