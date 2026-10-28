"""Post-call CRM delivery (speech/CRM brief Part 3a, vs-Retell C12).

When a call ends, its summary is POSTed to the customer's CRM
(``{crm.base_url}/api/xplogent/call-summary``) with an HMAC-SHA256 signature.
Delivery is fire-and-forget with retries: a CRM outage never blocks, delays or
loses a call record. Every attempt is appended to
``data_dir/voip/crm_deliveries.jsonl`` — the delivery log a Resend action (and
the owner, at breakfast) can read. (The brief sketches a ``crm_deliveries``
table; tonight's no-schema-change rule puts the same log in a file — the row
shape matches, so promoting it to a table later is mechanical.)

The API token is read from an environment variable (``crm.api_token_env``,
default ``XPLOGENT_CRM_API_TOKEN``) — the repo's existing secrets pattern —
never from plain config. Without a token nothing is sent (an unsigned summary
with a full transcript must never leave the server).

Signatures: ``X-Xplogent-Signature`` is the HMAC of the raw body (kept for
existing receivers); ``X-Xplogent-Signature-V2`` is the HMAC of
``"{X-Xplogent-Timestamp}.{body}"``, so a receiver can also refuse old,
replayed requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from xplogent.core.config import Config, data_dir
from xplogent.core.logging import get_logger
from xplogent.voip import alerts, jsonl

_log = get_logger("voip.crm")

_TASKS: set[asyncio.Task] = set()


def crm_config(cfg: Config) -> dict[str, Any]:
    return ((cfg.voip or {}).get("crm") or {})


def crm_enabled(cfg: Config) -> bool:
    crm = crm_config(cfg)
    return bool(crm.get("enabled")) and bool(str(crm.get("base_url", "")).strip())


def deliveries_path():
    return data_dir() / "voip" / "crm_deliveries.jsonl"


def _log_attempt(call_id: int, attempt: int, status: str, detail: str = "") -> None:
    try:
        jsonl.append(deliveries_path(), {"ts": time.time(), "call_id": call_id,
                                         "attempt": attempt, "status": status,
                                         "detail": detail})
    except Exception:  # noqa: BLE001 - logging must never break delivery
        _log.exception("could not write CRM delivery log")


def recent_deliveries(limit: int = 20) -> list[dict[str, Any]]:
    """The newest delivery attempts, newest first (dashboard)."""
    return list(reversed(jsonl.read(deliveries_path(), limit)))


def token_env(cfg: Config) -> str:
    return str(crm_config(cfg).get("api_token_env") or "XPLOGENT_CRM_API_TOKEN")


def sign(body: bytes, token: str) -> str:
    return "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()


def sign_v2(body: bytes, token: str, timestamp: str) -> str:
    return sign(timestamp.encode() + b"." + body, token)


def build_payload(cfg: Config, store, call_id: int, result: dict[str, Any],
                  brain=None, session=None) -> dict[str, Any]:
    """The call.completed payload (brief Part 3a) + Part D field completeness.

    Without ``brain``/``session`` (a call cut off by a restart) it is built from the
    stored call alone."""
    call = store.get_call(call_id) or {}
    st = brain.state if brain is not None else None
    started = call.get("started_at") or time.time()
    duration = (session.duration_s if session is not None
                else max(0.0, float(call.get("ended_at") or started) - float(started)))
    brief = call.get("brief") if isinstance(call.get("brief"), dict) else {}
    fields = result.get("fields") or {}
    values = dict(fields.get("values") or {})
    email = str(values.get("email") or "")
    return {
        "event": "call.completed",
        "api_version": API_VERSION,
        "call_id": call_id,
        "applicant_id": brief.get("applicant_id"),
        "sale_id": brief.get("sale_id"),
        "campaign_id": brief.get("campaign_id"),
        "final_attempt": bool(brief.get("final_attempt")),
        "provider_call_id": call.get("provider_call_id", ""),
        "direction": call.get("direction", ""),
        "from": call.get("from_number", ""),
        "to": call.get("to_number", ""),
        "transport": call.get("transport", ""),
        "line": call.get("line", ""),
        "via_queue": bool(call.get("via_queue")),
        "original_called_number": call.get("original_called_number", ""),
        "diversion_reason": call.get("diversion_reason", ""),
        "queue_wait_s": call.get("queue_wait_s"),
        "started_at": datetime.fromtimestamp(started, tz=UTC).isoformat(),
        "duration_s": round(duration, 1),
        "outcome": contract_outcome(str(result.get("outcome", "")), exit_path=st.exit_path if st else "",
                                    screening=bool(brief)),
        "outcome_detail": result.get("outcome", ""),
        "summary": call.get("summary", ""),
        "callback": st.callback if st else "",
        "callback_at": (getattr(st, "callback_at", "") or None) if st else None,
        "answers": values,
        "email_confirmed": email if email and (fields.get("confirmed") or {}).get("email") else "",
        "do_not_call": bool(st.do_not_call) if st else False,
        "exit_path": st.exit_path if st else "",
        "fields": result.get("fields"),
        "cost": result.get("cost", 0.0),
        "turn_timings": session.turn_timings if session is not None else [],
        "transcript": [{"speaker": t["speaker"], "text": t["text"]}
                       for t in call.get("turns", [])],
        "test": bool(call.get("test")),
        "recording_available": bool(call.get("recording")),   # audio stays on this server
    }


async def deliver(cfg: Config, payload: dict[str, Any], *, attempts: int = 3,
                  backoff_s: float = 0.5, transport=None, on_status=None) -> dict[str, Any]:
    """POST the payload with retries. Returns the final delivery status and
    logs every attempt. Never raises: the call record is already safe.

    ``on_status(status, detail)`` follows it on the call row: sending → retrying →
    sent / failed / skipped."""
    import httpx

    def status(state: str, detail: str = "") -> None:
        if on_status is not None:
            try:
                on_status(state, detail)
            except Exception:  # noqa: BLE001 - bookkeeping must never break delivery
                _log.exception("could not record the CRM delivery status")

    crm = crm_config(cfg)
    url = str(crm.get("base_url", "")).rstrip("/") + "/api/xplogent/call-summary"
    token = os.environ.get(token_env(cfg), "")
    call_id = int(payload.get("call_id", 0))
    last = {"call_id": call_id, "status": "failed", "attempts": 0, "detail": ""}
    if not token:
        last.update(status="skipped", detail=f"no API token ({token_env(cfg)} is not set)")
        _log_attempt(call_id, 0, "skipped", last["detail"])
        _log.warning("CRM delivery for call %s skipped: %s", call_id, last["detail"])
        status("skipped", last["detail"])
        return last
    status("sending")
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {token}",
               "X-Xplogent-Timestamp": ts,
               "X-Xplogent-Signature": sign(body, token),
               "X-Xplogent-Signature-V2": sign_v2(body, token, ts)}
    for attempt in range(1, attempts + 1):
        last["attempts"] = attempt
        try:
            async with httpx.AsyncClient(timeout=5.0, transport=transport or _TRANSPORT) as client:
                resp = await client.post(url, content=body, headers=headers)
            if resp.status_code < 300:
                last.update(status="sent", detail=f"HTTP {resp.status_code}")
                _log_attempt(call_id, attempt, "sent", last["detail"])
                alerts.crm_result(True)
                status("sent", last["detail"])
                return last
            last["detail"] = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - retry, never propagate
            last["detail"] = f"{type(exc).__name__}: {exc}"
        _log_attempt(call_id, attempt, "failed", last["detail"])
        if attempt < attempts:
            status("retrying", f"attempt {attempt} of {attempts}: {last['detail']}")
            await asyncio.sleep(backoff_s * (3 ** (attempt - 1)))
    _log.warning("CRM delivery for call %s failed after %s attempts: %s",
                 call_id, attempts, last["detail"])
    alerts.crm_result(False, f"call summary: {last['detail']}")
    status("failed", last["detail"])
    return last


def status_writer(db_path, call_id: int):
    """An ``on_status`` that records the delivery state on the call row."""
    def write(state: str, detail: str = "") -> None:
        from xplogent.memory.store import Store

        with Store(db_path) as store:
            store.set_crm_status(call_id, state, detail)
    return write


# ── pre-call lookup (phase 2) ──────────────────────────────────────────────────
_LOOKUP_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
LOOKUP_TIMEOUT_S = 1.5
_CACHE_S = 60.0
_LIMITS = {"name": 100, "company": 100, "notes": 500, "last_outcome": 50}


def lookup_enabled(cfg: Config) -> bool:
    return crm_enabled(cfg) and bool(crm_config(cfg).get("lookup", True))


def _clean(value: Any, limit: int) -> str:
    """CRM text is untrusted: plain, single-line-ish, bounded."""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def _signed_headers(body: bytes, token: str) -> dict[str, str]:
    ts = str(int(time.time()))
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}",
            "X-Xplogent-Timestamp": ts, "X-Xplogent-Signature": sign(body, token),
            "X-Xplogent-Signature-V2": sign_v2(body, token, ts)}


async def lookup(cfg: Config, phone: str, *, direction: str = "inbound", line: str = "",
                 original_called_number: str = "", sale_id: int | None = None,
                 timeout_s: float = LOOKUP_TIMEOUT_S, transport=None) -> dict[str, Any]:
    """Ask the CRM who is calling: ``POST {base_url}/api/xplogent/contact-lookup``,
    signed exactly like the push. Fail-open and never raises: a slow or broken CRM
    means the call goes on with the local contact store.

    Returns ``{"ok", "found", "contact", "error", "ms", "cached"}``; ``contact`` is
    ``{name, company, notes, last_outcome, do_not_call}`` with every text capped, plus
    the v2 candidate record (``applicant_id``, ``first_name``, ``flags``, ``history``…)
    when the CRM sends it.
    """
    out: dict[str, Any] = {"ok": False, "found": False, "contact": None, "error": "", "ms": 0.0,
                           "cached": False}
    if not phone or not lookup_enabled(cfg):
        out["error"] = "not configured"
        return out
    if not os.environ.get(token_env(cfg), ""):
        out["error"] = f"no API token ({token_env(cfg)} is not set)"
        return out
    key = f"{phone}|{sale_id or ''}"
    hit = _LOOKUP_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_S:
        return {**hit[1], "cached": True, "ms": 0.0}
    body: dict[str, Any] = {"phone": phone, "direction": direction, "line": line,
                            "original_called_number": original_called_number}
    if sale_id:
        body["sale_id"] = int(sale_id)
    res = await post(cfg, "contact-lookup", body, timeout_s=timeout_s, transport=transport)
    out["ms"] = res["ms"]
    if res["status"] == 404:
        out.update(ok=True)                                  # the CRM doesn't know this number
    elif not res["ok"]:
        out["error"] = res["error"] if res["status"] == 0 else f"HTTP {res['status']}"
    else:
        data = res["data"]
        data = data.get("contact", data) if isinstance(data.get("contact"), dict) else data
        if any(data.get(k) for k in (*_LIMITS, "do_not_call", "applicant_id")):
            out.update(ok=True, found=True, contact=parse_lookup_contact(data))
        else:
            out.update(ok=True)
    if out["ok"]:
        _LOOKUP_CACHE[key] = (time.monotonic(), dict(out))
        while len(_LOOKUP_CACHE) > 1000:
            _LOOKUP_CACHE.pop(next(iter(_LOOKUP_CACHE)))
    else:
        _log.warning("CRM lookup for a %s call failed (%s): using local contacts", direction, out["error"])
    return out


def enqueue_delivery(cfg: Config, payload: dict[str, Any], on_status=None) -> None:
    """Fire-and-forget from finalize_call (sync, called inside async wrap-ups)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning("CRM delivery skipped for call %s: no event loop",
                     payload.get("call_id"))
        return
    task = loop.create_task(deliver(cfg, payload, on_status=on_status))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


# ── API v2: the calls Xplogent makes to the CRM (docs/crm-integration-prompt.md) ──────
API_VERSION = 2
# The outcome values the CRM understands (contract 4.4); anything else goes as "other".
OUTCOMES_V2 = ("interested", "callback", "recruiter_callback", "not_interested", "no_job",
               "do_not_call", "no_answer", "voicemail", "busy", "number_unobtainable",
               "wrong_person", "transferred", "screen_incomplete", "interrupted", "other")
_TRANSPORT: Any = None              # tests and the dev mock route every v2 call here


def set_transport(transport: Any) -> None:
    """Route v2 calls through ``transport`` (an httpx transport), or back to the network."""
    global _TRANSPORT
    _TRANSPORT = transport


def contract_outcome(outcome: str, *, exit_path: str = "", screening: bool = False) -> str:
    """Map the agent's outcome onto the contract's list."""
    if outcome in OUTCOMES_V2 and outcome != "other":
        return outcome
    if exit_path == "wrong_party":
        return "wrong_person"
    if exit_path in ("not_interested", "do_not_call"):
        return exit_path
    if outcome == "transfer_failed":
        return "recruiter_callback"           # the caller was promised a person
    if outcome == "abandoned":
        return "screen_incomplete" if screening else "other"
    return "other"


async def post(cfg: Config, path: str, body: dict[str, Any], *, timeout_s: float = 5.0,
               transport=None) -> dict[str, Any]:
    """One signed POST to ``{base_url}/api/xplogent/{path}``. Never raises.

    Returns ``{ok, status, data, error, message, ms}``: ``error`` is the CRM's error code
    (``sale_not_callable``…) or a transport problem."""
    import httpx

    started = time.monotonic()
    out: dict[str, Any] = {"ok": False, "status": 0, "data": {}, "error": "", "message": "", "ms": 0.0}
    if not crm_enabled(cfg):
        out["error"] = "not configured"
        return out
    token = os.environ.get(token_env(cfg), "")
    if not token:
        out["error"] = f"no API token ({token_env(cfg)} is not set)"
        return out
    raw = json.dumps(body).encode()
    url = str(crm_config(cfg).get("base_url", "")).rstrip("/") + "/api/xplogent/" + path.lstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport or _TRANSPORT) as client:
            resp = await asyncio.wait_for(
                client.post(url, content=raw, headers=_signed_headers(raw, token)), timeout=timeout_s)
        out["status"] = resp.status_code
        try:
            data = resp.json()
        except ValueError:
            data = {}
        out["data"] = data if isinstance(data, dict) else {}
        out["ok"] = resp.status_code < 300
        if not out["ok"]:
            out["error"] = _clean(out["data"].get("error"), 60) or f"HTTP {resp.status_code}"
            out["message"] = _clean(out["data"].get("message"), 300)
    except TimeoutError:
        out["error"] = f"timed out after {timeout_s:g} s"
    except Exception as exc:  # noqa: BLE001 - the caller decides what a failure means
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    out["ms"] = round((time.monotonic() - started) * 1000, 1)
    # Reachable (even with a 404/409 answer) is what the outage alert is about.
    alerts.crm_result(out["ok"] or 400 <= out["status"] < 500, f"{path}: {out['error']}")
    return out


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _num(v: Any) -> float | None:
    try:
        return round(float(v), 2) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _pick(data: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Only the expected keys, each typed and capped: CRM data is untrusted."""
    data = data if isinstance(data, dict) else {}
    out: dict[str, Any] = {}
    for key, kind in spec.items():
        v = data.get(key)
        if kind == "int":
            out[key] = _int(v)
        elif kind == "num":
            out[key] = _num(v)
        elif kind == "bool":
            out[key] = v is True
        elif kind == "flags":
            out[key] = {_clean(k, 40): b is True for k, b in (v.items() if isinstance(v, dict) else [])
                        if re.fullmatch(r"[a-z_]{1,40}", str(k))}
        else:
            out[key] = _clean(v, kind) if v is not None else ""
    return out


SALE_SPEC = {"sale_id": "int", "job_title": 100, "job_category": 60, "position_type": 60,
             "job_type": 40, "timing": 200, "salary": 120, "experience": 200, "qualification": 200,
             "benefits": 300, "description": 800, "postcode_district": 10, "client_name": 100,
             "site_name": 100, "office_name": 100, "unit_name": 100, "postcode": 10,
             "cvs_sent": "int", "cv_limit": "int", "created_at": 40}
CANDIDATE_SPEC = {"applicant_id": "int", "name": 100, "first_name": 50, "phone_e164": 20,
                  "phone_kind": 20, "postcode_district": 10, "distance_km": "num",
                  "distance_miles": "num", "job_title": 100, "flags": "flags", "last_note": 300,
                  "last_note_at": 40, "last_called_at": 40}
_LOOKUP_V2 = {"applicant_id": "int", "first_name": 50, "phone_kind": 20, "postcode_district": 10,
              "job_title": 100, "callback_note": 300, "flags": "flags"}
_LISTS = {
    "history": {"date": 20, "sale_id": "int", "job": 120, "stage": 40, "sub_stage": 60},
    "applications": {"sale_id": "int", "job": 120, "interested": "bool", "latest_stage": 40,
                     "latest_sub_stage": 60},
    "interviews": {"sale_id": "int", "date": 20, "time": 10, "status": "int"},
    "calls": {"at": 40, "direction": 20, "duration_s": "int", "by": 60, "sale_id": "int"},
}


def parse_lookup_contact(data: dict[str, Any]) -> dict[str, Any]:
    """A contact-lookup match: v1 fields plus the v2 candidate record (lists capped at 5)."""
    contact = {k: _clean(data.get(k), n) for k, n in _LIMITS.items()}
    contact["do_not_call"] = data.get("do_not_call") is True
    if data.get("applicant_id") is not None:
        contact.update(_pick(data, _LOOKUP_V2))
        for key, spec in _LISTS.items():
            rows = data.get(key) if isinstance(data.get(key), list) else []
            contact[key] = [_pick(r, spec) for r in rows[:5] if isinstance(r, dict)]
    return contact


async def sales_open(cfg: Config, search: str = "", limit: int = 50, *, transport=None) -> dict[str, Any]:
    """Open vacancies for the campaign picker (contract 4.1)."""
    res = await post(cfg, "sales/open", {"search": search[:100], "limit": max(1, min(int(limit), 200))},
                     transport=transport)
    rows = res["data"].get("sales") if res["ok"] else None
    return {**res, "sales": [_pick(r, SALE_SPEC) for r in (rows or [])[:200] if isinstance(r, dict)]}


async def campaign_queue(cfg: Config, sale_id: int, *, radius_km: float = 15, limit: int = 50,
                         exclude_called_within_days: int = 14, transport=None) -> dict[str, Any]:
    """Who to call for a vacancy (contract 4.2). A closed / full vacancy is ``error:
    sale_not_callable``."""
    res = await post(cfg, "campaigns/queue", {"sale_id": int(sale_id), "radius_km": float(radius_km),
                                             "limit": max(1, min(int(limit), 200)),
                                             "exclude_called_within_days": int(exclude_called_within_days)},
                     timeout_s=15.0, transport=transport)
    data = res["data"] if res["ok"] else {}
    skipped = data.get("skipped") if isinstance(data.get("skipped"), dict) else {}
    candidates = [_pick(c, CANDIDATE_SPEC) for c in (data.get("candidates") or [])[:200]
                  if isinstance(c, dict)]
    return {**res, "sale": _pick(data.get("sale"), SALE_SPEC) if data.get("sale") else None,
            "candidates": [c for c in candidates if c["applicant_id"] and c["phone_e164"]],
            "skipped": {_clean(k, 40): _int(v) or 0 for k, v in skipped.items()}}


async def send_job_details(cfg: Config, applicant_id: int, sale_id: int, email: str, call_id: int,
                           *, transport=None) -> dict[str, Any]:
    """Ask the CRM to email the vacancy to the candidate (contract 4.5)."""
    return await post(cfg, "send-job-details", {"applicant_id": int(applicant_id), "sale_id": int(sale_id),
                                                "email": str(email)[:200], "call_id": int(call_id)},
                      timeout_s=10.0, transport=transport)


async def dial_lock(cfg: Config, phone: str, applicant_id: int | None = None, ttl_seconds: int = 900,
                    *, transport=None) -> dict[str, Any]:
    """Take the CRM's dial lock so no recruiter rings the same person (contract 4.6).

    ``locked`` is False with ``locked_by`` when someone else holds it."""
    res = await post(cfg, "dial-lock", {"phone": phone, "applicant_id": applicant_id,
                                        "ttl_seconds": int(ttl_seconds)}, transport=transport)
    data = res["data"]
    return {**res, "locked": res["ok"] and data.get("locked") is True,
            "locked_by": _clean(data.get("locked_by"), 100), "expires_at": _clean(data.get("expires_at"), 40)}


async def dial_unlock(cfg: Config, phone: str, *, transport=None) -> dict[str, Any]:
    return await post(cfg, "dial-unlock", {"phone": phone}, transport=transport)


def explain_failure(res: dict[str, Any]) -> dict[str, str]:
    """Settings → CRM → Test connection: what went wrong and what to do, in plain words."""
    status, error = int(res.get("status") or 0), str(res.get("error") or "")
    if res.get("ok"):
        return {"reason": "", "fix": ""}
    if error == "not configured":
        return {"reason": "CRM link is off or has no address.",
                "fix": "Switch it on and enter the CRM address (e.g. https://crm.example.com)."}
    if error.startswith("no API token"):
        return {"reason": "No API token saved.", "fix": "Paste the same token as XPLOGENT_API_TOKEN in the CRM's .env."}
    if status == 401:
        if "skew" in (str(res.get("message")) + error).lower() or "timestamp" in str(res.get("message")).lower():
            return {"reason": "The CRM refused the request time: the two servers' clocks differ too much.",
                    "fix": "Turn on automatic time (NTP) on both servers (timedatectl set-ntp true)."}
        return {"reason": "The CRM refused the token or signature.",
                "fix": "Use exactly the same token here and as XPLOGENT_API_TOKEN in the CRM's .env."}
    if status == 503:
        return {"reason": "The CRM's Xplogent API is switched off (no token on the CRM side).",
                "fix": "Set XPLOGENT_API_TOKEN in the CRM's .env and clear its config cache."}
    if status == 404:
        return {"reason": "The CRM doesn't have the Xplogent endpoints at this address.",
                "fix": "Check the address (no /api at the end) and that the CRM update with /api/xplogent is deployed."}
    if status == 429:
        return {"reason": "The CRM is rate-limiting Xplogent.", "fix": "Wait a minute and try again."}
    if status >= 500:
        return {"reason": f"The CRM had an internal error (HTTP {status}).", "fix": "Check the CRM's laravel.log."}
    if "timed out" in error:
        return {"reason": "The CRM didn't answer in time.", "fix": "Check the CRM is up and reachable from this server."}
    if error:
        return {"reason": f"Could not reach the CRM: {error}", "fix": "Check the address and that this server can reach it."}
    return {"reason": f"Unexpected answer (HTTP {status}).", "fix": "Check the CRM's logs."}
