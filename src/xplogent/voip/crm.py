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
import time
from datetime import UTC, datetime
from typing import Any

from xplogent.core.config import Config, data_dir
from xplogent.core.logging import get_logger
from xplogent.voip import jsonl

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
                  brain, session) -> dict[str, Any]:
    """The call.completed payload (brief Part 3a) + Part D field completeness."""
    call = store.get_call(call_id) or {}
    st = brain.state
    started = call.get("started_at") or time.time()
    return {
        "event": "call.completed",
        "call_id": call_id,
        "direction": call.get("direction", ""),
        "from": call.get("from_number", ""),
        "to": call.get("to_number", ""),
        "transport": call.get("transport", ""),
        "started_at": datetime.fromtimestamp(started, tz=UTC).isoformat(),
        "duration_s": round(session.duration_s, 1),
        "outcome": result.get("outcome", ""),
        "summary": call.get("summary", ""),
        "callback": st.callback,
        "do_not_call": bool(st.do_not_call),
        "exit_path": st.exit_path,
        "fields": result.get("fields"),
        "cost": result.get("cost", 0.0),
        "turn_timings": session.turn_timings,
        "transcript": [{"speaker": t["speaker"], "text": t["text"]}
                       for t in call.get("turns", [])],
        "test": bool(call.get("test")),
        "recording_available": bool(call.get("recording")),   # audio stays on this server
    }


async def deliver(cfg: Config, payload: dict[str, Any], *, attempts: int = 3,
                  backoff_s: float = 0.5, transport=None) -> dict[str, Any]:
    """POST the payload with retries. Returns the final delivery status and
    logs every attempt. Never raises: the call record is already safe."""
    import httpx

    crm = crm_config(cfg)
    url = str(crm.get("base_url", "")).rstrip("/") + "/api/xplogent/call-summary"
    token = os.environ.get(token_env(cfg), "")
    call_id = int(payload.get("call_id", 0))
    last = {"call_id": call_id, "status": "failed", "attempts": 0, "detail": ""}
    if not token:
        last.update(status="skipped", detail=f"no API token ({token_env(cfg)} is not set)")
        _log_attempt(call_id, 0, "skipped", last["detail"])
        _log.warning("CRM delivery for call %s skipped: %s", call_id, last["detail"])
        return last
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
            async with httpx.AsyncClient(timeout=5.0, transport=transport) as client:
                resp = await client.post(url, content=body, headers=headers)
            if resp.status_code < 300:
                last.update(status="sent", detail=f"HTTP {resp.status_code}")
                _log_attempt(call_id, attempt, "sent", last["detail"])
                return last
            last["detail"] = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - retry, never propagate
            last["detail"] = f"{type(exc).__name__}: {exc}"
        _log_attempt(call_id, attempt, "failed", last["detail"])
        if attempt < attempts:
            await asyncio.sleep(backoff_s * (3 ** (attempt - 1)))
    _log.warning("CRM delivery for call %s failed after %s attempts: %s",
                 call_id, attempts, last["detail"])
    return last


def enqueue_delivery(cfg: Config, payload: dict[str, Any]) -> None:
    """Fire-and-forget from finalize_call (sync, called inside async wrap-ups)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning("CRM delivery skipped for call %s: no event loop",
                     payload.get("call_id"))
        return
    task = loop.create_task(deliver(cfg, payload))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
