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


# Host names that only mean something inside a private network.
_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".intranet", ".localdomain", ".home.arpa", ".corp")


def is_local_network_host(host: str) -> bool:
    """This machine or the local network: loopback/private/link-local IPs, ``localhost``,
    a bare machine name (``crm-server``) or a LAN-only suffix (``crm.local``)."""
    import ipaddress

    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(_LAN_SUFFIXES)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def base_url_problem(url: str, allow_http: bool = False) -> str:
    """Why the CRM address must not be used ("" = fine). The token is the bearer credential
    and the HMAC key, and summaries carry transcripts, so plain http is fine on this
    machine or the local network (a CRM on the office LAN) but never across the internet.
    ``voip.crm.allow_http: true`` also allows an internal DNS name that looks public."""
    from urllib.parse import urlsplit

    parts = urlsplit(str(url or "").strip())
    if parts.scheme == "https" and parts.hostname:
        return ""
    if parts.scheme == "http" and parts.hostname:
        if allow_http or is_local_network_host(parts.hostname):
            return ""
        return ("plain http:// to an internet address would send the API token and call transcripts "
                "unencrypted: use https://, or the CRM's local-network address (e.g. http://192.168.1.20); "
                "for an internal name that looks public set voip.crm.allow_http: true")
    return "the CRM address must look like http://192.168.1.20 or https://crm.example.com"


def address_problem(cfg: Config) -> str:
    c = crm_config(cfg)
    return base_url_problem(str(c.get("base_url", "")), bool(c.get("allow_http")))


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
        "guard_blocked": len(getattr(brain, "guard_blocked", []) or []) if brain is not None else 0,
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
    if problem := base_url_problem(str(crm.get("base_url", "")), bool(crm.get("allow_http"))):
        last.update(status="skipped", detail=f"insecure address: {problem}")
        _log_attempt(call_id, 0, "skipped", last["detail"])
        _log.warning("CRM delivery for call %s skipped: %s", call_id, problem)
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
            async with httpx.AsyncClient(timeout=5.0, transport=transport or _TRANSPORT,
                                         follow_redirects=False) as client:
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


# ── Durable outbox: a summary is stored first, then sent; retried until the CRM has it ──
OUTBOX_LEASE_S = 120                       # a send in progress owns the row this long
OUTBOX_MAX_TRIES = 10                      # then the row is "dead": alert + manual Resend
OUTBOX_BACKOFF_S = (60, 300, 900, 3600)    # after try 1, 2, 3, 4+ (seconds)
OUTBOX_MAX_AGE_S = 7 * 86400               # older summaries are only sent by hand
OUTBOX_EVERY_S = 30


def queue_summary(cfg: Config, store: Any, call_id: int, payload: dict[str, Any]) -> None:
    """Record the call's summary as pending in the database, then try to send it now.
    If the send fails, or the server stops first, the outbox loop sends it later; the CRM
    dedupes on ``call_id``, so a repeat never duplicates its note."""
    store.crm_queue(call_id, payload, time.time() + OUTBOX_LEASE_S)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return                                   # no loop here: the outbox sends it
    task = loop.create_task(send_queued(cfg, call_id, payload, attempts=3))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def send_queued(cfg: Config, call_id: int, payload: dict[str, Any], *, attempts: int = 1,
                      transport=None, backoff_s: float | None = None, now: float | None = None) -> dict[str, Any]:
    """Send a stored summary and book the next try if it failed."""
    from xplogent.memory.store import Store

    extra = {k: v for k, v in (("transport", transport), ("backoff_s", backoff_s)) if v is not None}
    res = await deliver(cfg, payload, attempts=attempts, on_status=status_writer(cfg.db_path, call_id), **extra)
    with Store(cfg.db_path) as store:
        if res["status"] != "failed":
            store.crm_schedule(call_id, next_at=0)
            return res
        tries = store.crm_attempts(call_id) + int(res.get("attempts") or 1)
        if tries >= OUTBOX_MAX_TRIES:
            store.crm_schedule(call_id, attempts=tries, next_at=0, status="dead",
                               detail=f"gave up after {tries} tries: {res['detail']}")
            res["status"] = "dead"
        else:
            wait = OUTBOX_BACKOFF_S[min(tries, len(OUTBOX_BACKOFF_S)) - 1]
            store.crm_schedule(call_id, attempts=tries, next_at=(time.time() if now is None else now) + wait)
    if res["status"] == "dead":
        alerts.alert("crm_dead_letter", f"Call {call_id}'s summary could not reach the CRM after "
                                        f"{OUTBOX_MAX_TRIES} tries ({res['detail']}): use Resend on the call.")
    return res


async def outbox_tick(cfg: Config, now: float | None = None, transport=None) -> int:
    """Send whatever is due; returns how many summaries the CRM accepted."""
    from xplogent.memory.store import Store

    if not crm_enabled(cfg):
        return 0
    now = time.time() if now is None else now
    with Store(cfg.db_path) as store:
        due = store.crm_due(now, now - OUTBOX_MAX_AGE_S, OUTBOX_MAX_TRIES)
        work = []
        for call_id in due:
            payload = store.get_crm_payload(call_id)
            if payload is None:
                continue
            store.crm_schedule(call_id, next_at=now + OUTBOX_LEASE_S)     # in flight
            work.append((call_id, payload))
    sent = 0
    for call_id, payload in work:
        res = await send_queued(cfg, call_id, payload, attempts=1, transport=transport, now=now)
        sent += res["status"] == "sent"
    return sent


async def outbox_loop() -> None:
    """Started by the API: every 30 s, resend summaries the CRM hasn't got yet."""
    from xplogent.core.config import load_config

    while True:
        await asyncio.sleep(OUTBOX_EVERY_S)
        try:
            await outbox_tick(load_config())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            _log.exception("CRM outbox pass failed")


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


# ── API v2: the calls Xplogent makes to the CRM (docs/crm-laravel.md) ────────────────
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
    if problem := address_problem(cfg):
        out["error"] = f"insecure address: {problem}"
        return out
    raw = json.dumps(body).encode()
    url = str(crm_config(cfg).get("base_url", "")).rstrip("/") + "/api/xplogent/" + path.lstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport or _TRANSPORT,
                                     follow_redirects=False) as client:   # never carry the token elsewhere
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


# A redacted CV excerpt the CRM may send (opt-in on the CRM side, XPLOGENT_SHARE_CV_EXCERPT).
CV_EXCERPT_MAX = 2000
SALE_SPEC = {"sale_id": "int", "job_title": 100, "job_category": 60, "position_type": 60,
             "job_type": 40, "timing": 200, "salary": 120, "experience": 200, "qualification": 200,
             "benefits": 300, "description": 800, "postcode_district": 10, "client_name": 100,
             "site_name": 100, "office_name": 100, "unit_name": 100, "postcode": 10,
             "cvs_sent": "int", "cv_limit": "int", "created_at": 40}
CANDIDATE_SPEC = {"applicant_id": "int", "name": 100, "first_name": 50, "phone_e164": 20,
                  "phone_kind": 20, "postcode_district": 10, "distance_km": "num",
                  "distance_miles": "num", "job_title": 100, "flags": "flags", "last_note": 300,
                  "last_note_at": 40, "last_called_at": 40, "cv_excerpt": CV_EXCERPT_MAX}
_LOOKUP_V2 = {"applicant_id": "int", "first_name": 50, "phone_kind": 20, "postcode_district": 10,
              "job_title": 100, "callback_note": 300, "flags": "flags", "cv_excerpt": CV_EXCERPT_MAX}
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
                    *, lock_token: str = "", transport=None) -> dict[str, Any]:
    """Take the CRM's dial lock so no recruiter rings the same person (contract 4.6).

    ``locked`` is False with ``locked_by`` when someone else holds it. ``lock_token`` (made
    here when not given) identifies this lease: only the same token releases it, so one
    worker can never free another's lock."""
    import secrets

    token = lock_token or secrets.token_hex(12)
    res = await post(cfg, "dial-lock", {"phone": phone, "applicant_id": applicant_id,
                                        "ttl_seconds": int(ttl_seconds), "lock_token": token}, transport=transport)
    data = res["data"]
    return {**res, "locked": res["ok"] and data.get("locked") is True, "lock_token": token,
            "locked_by": _clean(data.get("locked_by"), 100), "expires_at": _clean(data.get("expires_at"), 40)}


async def dial_unlock(cfg: Config, phone: str, lock_token: str = "", *, transport=None) -> dict[str, Any]:
    return await post(cfg, "dial-unlock", {"phone": phone, "lock_token": lock_token}, transport=transport)


def explain_failure(res: dict[str, Any], base_url: str = "") -> dict[str, str]:
    """Settings → CRM → Test connection: what went wrong and what to do, in plain words."""
    status, error = int(res.get("status") or 0), str(res.get("error") or "")
    if res.get("ok"):
        return {"reason": "", "fix": ""}
    if status == 0 and error and (loop := loopback_problem(base_url)):
        return {"reason": f"Could not reach the CRM: {error}. {loop[0]}", "fix": loop[1]}
    if error == "not configured":
        return {"reason": "CRM link is off or has no address.",
                "fix": "Switch it on and enter the CRM address (e.g. https://crm.example.com)."}
    if error.startswith("insecure address"):
        return {"reason": "The CRM address is plain http:// to an internet address, which would send the token "
                          "and call data unencrypted.",
                "fix": "Use the CRM's https:// address, or its local-network address (e.g. http://192.168.1.20) "
                       "when Xplogent and the CRM are on the same network."}
    if 300 <= status < 400:
        return {"reason": "The CRM answered with a redirect; Xplogent doesn't follow redirects with the token.",
                "fix": "Enter the final https:// address of the CRM (no redirect)."}
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


# ── requests the CRM makes to Xplogent (POST /voip/crm/dial) ──────────────────────────
INBOUND_MAX_SKEW_S = 300
_SEEN_SIGNATURES: dict[str, float] = {}       # signature -> when seen (replay guard)


def verify_inbound(cfg: Config, body: bytes, headers: Any, *, now: float | None = None) -> tuple[int, str]:
    """Check a request the CRM signed the way Xplogent signs its own (the same shared
    token, ``Authorization: Bearer`` plus the V2 HMAC over ``"<timestamp>.<body>"``).

    Returns ``(0, "")`` when it is genuine, else the HTTP status and the reason. A
    signature is accepted once: a copied request can't start a second call."""
    if not crm_enabled(cfg):
        return 503, "the CRM link is switched off (Settings → CRM)"
    token = os.environ.get(token_env(cfg), "")
    if not token:
        return 503, f"no CRM token set ({token_env(cfg)})"
    auth = str(headers.get("authorization") or "")
    if not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
        return 401, "bad token"
    ts = str(headers.get("x-xplogent-timestamp") or "")
    if not ts.isdigit():
        return 401, "timestamp missing"
    now = time.time() if now is None else now
    if abs(now - int(ts)) > INBOUND_MAX_SKEW_S:
        return 401, "timestamp skew too large: check both clocks"
    sig = str(headers.get("x-xplogent-signature-v2") or "")
    if not hmac.compare_digest(sig.encode(), sign_v2(body, token, ts).encode()):
        return 401, "bad signature"
    for seen, at in list(_SEEN_SIGNATURES.items()):
        if now - at > 2 * INBOUND_MAX_SKEW_S:
            del _SEEN_SIGNATURES[seen]
    if sig in _SEEN_SIGNATURES:
        return 401, "this request was already used (replay)"
    _SEEN_SIGNATURES[sig] = now
    return 0, ""


def clean_crm_brief(raw: Any, applicant_id: int, sale_id: int, requested_by: str) -> dict[str, Any]:
    """The call brief the CRM's Agent call page sends, reduced to the expected fields
    (typed and capped: CRM data is untrusted)."""
    raw = raw if isinstance(raw, dict) else {}
    brief: dict[str, Any] = {"applicant_id": applicant_id, "sale_id": sale_id,
                             "requested_by": _clean(requested_by, 100), "source": "crm_agent_call"}
    if isinstance(raw.get("sale"), dict):
        brief["sale"] = {k: v for k, v in _pick(raw["sale"], SALE_SPEC).items() if v not in ("", None)}
        brief["sale"]["sale_id"] = sale_id
    if isinstance(raw.get("candidate"), dict):
        cand = _pick(raw["candidate"], CANDIDATE_SPEC)
        cand.pop("phone_e164", None)                 # the number travels separately
        brief["candidate"] = {k: v for k, v in cand.items() if v not in ("", None, {})}
        brief["candidate"]["applicant_id"] = applicant_id
    return brief


# ── where the two servers find each other (Docker / LAN) ──────────────────────────────
def loopback_problem(base_url: str) -> tuple[str, str] | None:
    """In Docker, a CRM address on 127.0.0.1 / localhost points at this container, never at
    the CRM: ``(reason, fix)``, else ``None``."""
    from urllib.parse import urlsplit

    from xplogent.core.repo_info import in_docker

    if not base_url or not in_docker():
        return None
    try:
        host = (urlsplit(base_url.strip()).hostname or "").lower()
    except ValueError:
        return None
    if host == "localhost" or host.startswith("127.") or host in ("::1", "0.0.0.0"):
        return (f"Xplogent runs in Docker, where {host} is the container itself, not the CRM.",
                "Use the CRM machine's LAN address (e.g. http://192.168.1.20:8000), or "
                "http://host.docker.internal:<port> if the CRM runs on this same server.")
    return None


def callback_address(voip: dict[str, Any]) -> dict[str, str]:
    """The address the CRM should use to reach Xplogent (its ``XPLOGENT_BASE_URL``, for
    POST /voip/crm/dial), worked out from how this server is published.

    ``{"url", "mode", "warning", "note"}``: ``warning`` is set when a CRM on another machine
    can't reach it as published."""
    from xplogent.core.repo_info import in_docker

    env = os.environ
    lan_host = (env.get("XPLOGENT_LAN_ADDRESS", "").strip() or env.get("XPLOGENT_HOST_IP", "").strip()
                or "<this server's LAN IP>")       # written by `xplogent-docker local --lan` / get.sh --lan
    if in_docker():
        domain = env.get("DOMAIN", "").strip()
        if "https" in env.get("COMPOSE_FILE", "") and domain:
            return {"url": f"https://{domain}", "mode": "https", "warning": "",
                    "note": "The CRM machine must resolve this name to this server (public DNS, or a local "
                            "DNS entry if the router doesn't loop back)."}
        port = env.get("XPLOGENT_PORT", "").strip() or "8765"
        bind = env.get("XPLOGENT_BIND", "").strip() or "127.0.0.1"
        if bind in ("127.0.0.1", "localhost", "::1"):
            return {"url": f"http://127.0.0.1:{port}", "mode": "this server only",
                    "warning": "Xplogent's port is open to this server only, so a CRM on another machine "
                               "can't reach it (Call with AI in the CRM would fail).",
                    "note": f"On this server run: sudo xplogent-docker local --lan, then allow port {port} "
                            "from the CRM machine in the firewall. A CRM on this same server can use "
                            f"http://127.0.0.1:{port} as it is."}
        host = lan_host if bind in ("0.0.0.0", "::") else bind
        return {"url": f"http://{host}:{port}", "mode": "local network", "warning": "",
                "note": f"Allow port {port} from the CRM machine in this server's firewall."}
    public = str((voip or {}).get("public_url") or "").strip().rstrip("/")
    if public:
        return {"url": public, "mode": "public address", "warning": "", "note": ""}
    host = env.get("XPLOGENT_API_HOST", "").strip() or "127.0.0.1"
    port = env.get("XPLOGENT_API_PORT", "").strip() or "8765"
    if host in ("127.0.0.1", "localhost", "::1"):
        return {"url": f"http://127.0.0.1:{port}", "mode": "this server only",
                "warning": "Xplogent listens on this server only, so a CRM on another machine can't reach it.",
                "note": "Start it with --host 0.0.0.0 (and a firewall rule for the CRM machine), or run it "
                        "with Docker in local --lan mode."}
    return {"url": f"http://{lan_host if host in ('0.0.0.0', '::') else host}:{port}", "mode": "local network",
            "warning": "", "note": f"Allow port {port} from the CRM machine in the firewall."}
