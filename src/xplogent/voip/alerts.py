"""Alerts when the phone agent needs a person: repeated call failures, the CRM down,
calls cut off by a restart (and later the dialler stopping itself, TPS unreachable).

Each alert is logged (``data_dir/voip/alerts.jsonl``, shown on the Calls page), audited,
and POSTed to the webhook in ``XPLOGENT_ALERT_WEBHOOK_URL`` (a secret: Slack and Teams
webhook addresses carry their own token). The body has ``text`` for Slack/Teams plus
``event``, ``kind``, ``detail`` and ``at`` for anything else. One alert per kind per
``voip.alerts.min_gap_minutes`` so a bad hour doesn't become a hundred messages.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from xplogent.core.config import data_dir, load_config
from xplogent.core.logging import get_logger

_log = get_logger("voip.alerts")

WEBHOOK_ENV = "XPLOGENT_ALERT_WEBHOOK_URL"
FAILURES_IN_A_ROW = 5
CRM_DOWN_S = 300
_LAST: dict[str, float] = {}             # kind -> when it was last sent
_TASKS: set[asyncio.Task] = set()


def log_path() -> Path:
    return data_dir() / "voip" / "alerts.jsonl"


def recent(limit: int = 20, since: float = 0.0) -> list[dict[str, Any]]:
    from xplogent.voip import jsonl

    return [r for r in jsonl.read(log_path(), limit) if float(r.get("at") or 0) >= since][::-1]


def _settings() -> dict[str, Any]:
    try:
        return dict((load_config().voip or {}).get("alerts") or {})
    except Exception:  # noqa: BLE001 - alerting must never break a call
        return {}


async def _post(url: str, body: dict[str, Any], transport=None) -> str:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8.0, transport=transport) as client:
            resp = await client.post(url, json=body)
        return "sent" if resp.status_code < 300 else f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}"


def alert(kind: str, text: str, detail: str = "", *, force: bool = False, transport=None) -> bool:
    """Raise an alert; False when the same kind was sent too recently. Never raises."""
    from xplogent.voip import jsonl

    gap = float(_settings().get("min_gap_minutes", 15)) * 60
    now = time.time()
    if not force and now - _LAST.get(kind, 0.0) < gap:
        return False
    _LAST[kind] = now
    _log.warning("ALERT %s: %s", kind, text)
    record = {"at": now, "kind": kind, "text": text[:500], "detail": detail[:1000]}
    try:
        jsonl.append(log_path(), record, max_bytes=500_000)
        from xplogent.memory.store import Store

        with Store(load_config().db_path) as store:
            store.add_audit("voip", "alert", kind, "high", None, text[:300])
    except Exception:  # noqa: BLE001
        _log.exception("could not record the alert")
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if url.startswith(("https://", "http://")):
        body = {"text": f"⚠ Xplogent: {text}", "event": "xplogent.alert", "kind": kind,
                "detail": detail[:1000], "at": now}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(_post(url, body, transport))
            _TASKS.add(task)
            task.add_done_callback(_TASKS.discard)
        else:
            asyncio.run(_post(url, body, transport))
    return True


async def send_test(transport=None) -> dict[str, Any]:
    """Settings → Alerts → Send test: posts straight away and reports the result."""
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not url:
        return {"ok": False, "error": f"no webhook address saved ({WEBHOOK_ENV})"}
    status = await _post(url, {"text": "✓ Xplogent test alert: alerts reach this channel.",
                               "event": "xplogent.alert", "kind": "test", "detail": "",
                               "at": time.time()}, transport)
    return {"ok": status == "sent", "status": status,
            "error": "" if status == "sent" else f"the webhook answered {status}"}


# ── what raises them ──────────────────────────────────────────────────────────
# A busy line, no answer or a wrong number is a normal result, not a failure.
_NORMAL_SIP = re.compile(r"\b(404|408|410|480|484|486|487|600|603|604)\b")
_STREAK: dict[str, Any] = {"n": 0, "last": ""}


def call_result(ok: bool, detail: str = "") -> None:
    """Count system failures in a row (dial errors, calls where the agent kept failing)."""
    if ok or _NORMAL_SIP.search(detail or ""):
        _STREAK.update(n=0, last="")
        return
    _STREAK["n"] += 1
    _STREAK["last"] = detail
    if _STREAK["n"] >= FAILURES_IN_A_ROW:
        alert("call_failures", f"{_STREAK['n']} calls in a row failed. Last: {detail[:200]}", detail)


def dial_result(res: dict[str, Any]) -> None:
    call_result(bool(res.get("ok")), str(res.get("error") or ""))


def session_result(turn_timings: list[dict[str, Any]]) -> None:
    """A call where the agent failed on most turns counts as a failure."""
    errors = [t.get("error") for t in turn_timings or [] if t.get("error")]
    failed = len(errors) >= 2 or (errors and len(errors) == len(turn_timings))
    call_result(not failed, str(errors[-1]) if errors else "")


_CRM: dict[str, Any] = {"down_since": 0.0, "last": ""}


def crm_result(ok: bool, detail: str = "", *, now: float | None = None) -> None:
    """CRM deliveries/lookups: alert once they have failed for 5 minutes with no success."""
    now = time.time() if now is None else now
    if ok:
        _CRM.update(down_since=0.0, last="")
        return
    if not _CRM["down_since"]:
        _CRM["down_since"] = now
    _CRM["last"] = detail
    if now - _CRM["down_since"] >= CRM_DOWN_S:
        alert("crm_unreachable", f"the CRM has not answered for {int((now - _CRM['down_since']) // 60)} "
                                 f"minutes ({detail[:120]}). Call summaries are kept and can be resent.",
              detail)


def reset() -> None:
    """For tests."""
    _LAST.clear()
    _STREAK.update(n=0, last="")
    _CRM.update(down_since=0.0, last="")
