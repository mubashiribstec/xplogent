"""Per-call diagnostics: timeline, stage timings, SIP ladder, RTP stats, carrier events.

Live calls read it from the session; finished calls from the ``call_debug`` table
(saved by ``finalize_call``, pruned with the recordings' ``retention_days``).

Privacy and secrets: the SIP trace keeps a fixed list of headers, so digest
``Authorization`` / ``Proxy-Authorization`` (and challenges) are never stored. The
downloadable bundle carries a config snapshot with secret-looking settings
redacted, and every configured secret value is scrubbed from the whole bundle as a
last line of defence. Recording audio is never part of it.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from typing import Any

# Headers worth seeing in a ladder; anything else (notably Authorization,
# Proxy-Authorization, WWW-Authenticate, Proxy-Authenticate) is never stored.
SIP_HEADERS_KEPT = ("From", "To", "CSeq", "Contact", "Reason", "Event", "Subscription-State",
                    "Refer-To", "Referred-By", "Diversion", "P-Asserted-Identity",
                    "Remote-Party-ID", "X-Xplogent-Via", "X-Xplogent-Queue-Wait", "User-Agent",
                    "Server", "Expires", "Retry-After")
MAX_MESSAGES = 200
MAX_BYTES = 64_000


class SipTrace:
    """A bounded ladder of one call's SIP messages (direction, time, first line, key headers)."""

    def __init__(self) -> None:
        self.started = time.time()
        self.items: deque[dict[str, Any]] = deque()
        self.size = 0
        self.dropped = 0

    def add(self, direction: str, msg: Any, addr: Any = None) -> None:
        first = (f"{msg.method} {msg.uri}" if msg.is_request else f"{msg.status} {msg.reason}").strip()
        headers = {name: msg.get(name)[:200] for name in SIP_HEADERS_KEPT if msg.get(name)}
        item = {"t": round(time.time() - self.started, 3), "dir": direction, "line": first,
                "peer": f"{addr[0]}:{addr[1]}" if addr else "", "headers": headers,
                "sdp": bool(msg.body) and b"m=audio" in msg.body}
        self.items.append(item)
        self.size += len(json.dumps(item))
        while self.items and (len(self.items) > MAX_MESSAGES or self.size > MAX_BYTES):
            self.size -= len(json.dumps(self.items.popleft()))
            self.dropped += 1

    def export(self) -> dict[str, Any]:
        return {"messages": list(self.items), "dropped": self.dropped}


def collect(session: Any) -> dict[str, Any]:
    """Everything the debug view shows for a call that's in memory."""
    out: dict[str, Any] = {"timeline": list(getattr(session, "timeline", [])),
                           "turns": list(getattr(session, "turn_timings", [])),
                           "providers": {
                               "stt_fallbacks": getattr(session.stt, "used_fallback", 0),
                               "tts_fallbacks": getattr(session.tts, "used_fallback", 0)}}
    for source in getattr(session, "debug_sources", []):
        try:
            out.update(source() or {})
        except Exception as exc:  # noqa: BLE001 - a broken source must not hide the rest
            out.setdefault("errors", []).append(f"{type(exc).__name__}: {exc}")
    return out


def save(store: Any, call_id: int, session: Any) -> None:
    store.save_call_debug(call_id, collect(session))


# ── download bundle ─────────────────────────────────────────────────────────────
_SECRETISH = re.compile(r"(secret|password|token|api_key|apikey|private)", re.I)


def redact_config(value: Any, key: str = "") -> Any:
    """Copy of a config tree with secret-looking settings blanked (``*_env`` names stay:
    they say *where* a secret lives, not what it is)."""
    if isinstance(value, dict):
        return {k: redact_config(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_config(v, key) for v in value]
    if key and _SECRETISH.search(key) and not key.endswith("_env") and value not in ("", None):
        return "[redacted]"
    return value


def secret_values() -> list[str]:
    from xplogent.core.config import secret_names

    names = [*secret_names(), "XPLOGENT_AUTH_TOKEN"]
    return [v for v in (os.environ.get(n, "") for n in names) if len(v) >= 6]


def scrub(text: str, secrets: list[str]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def bundle(cfg: Any, store: Any, call_id: int, live: Any = None) -> dict[str, Any] | None:
    """One file for a bug report: the call, its transcript and diagnostics, the config."""
    call = store.get_call(call_id)
    if call is None:
        return None
    data = collect(live.session) if live is not None else (store.get_call_debug(call_id) or {})
    call = {k: v for k, v in call.items() if k != "recording"}
    raw = redact_config(json.loads(json.dumps(cfg.raw, default=str)))
    doc = {"bundle": "xplogent-call-debug/1", "created_at": time.time(), "call": call,
           "debug": data, "config": {"voip": raw.get("voip", {}), "model": raw.get("model", "")}}
    return json.loads(scrub(json.dumps(doc, default=str), secret_values()))


def summary_lines(doc: dict[str, Any]) -> list[str]:
    """Plain-text summary (CLI)."""
    call, dbg = doc.get("call", {}), doc.get("debug", {})
    lines = [f"Call {call.get('id')} · {call.get('direction')} · {call.get('transport')}"
             f"{' · ' + call['line'] if call.get('line') else ''} · outcome {call.get('outcome') or '—'}",
             f"  from {call.get('from_number')} to {call.get('to_number')}"
             + (f" (dialled {call['original_called_number']})" if call.get("original_called_number") else "")
             + (" · via queue" if call.get("via_queue") else "")]
    for ev in dbg.get("timeline", []):
        lines.append(f"  {ev.get('t', 0):7.2f}s  {ev.get('kind')}  {ev.get('detail', '')}".rstrip())
    for n, t in enumerate(dbg.get("turns", []), 1):
        lines.append(f"  turn {n}: stt {t.get('stt_ms', 0)} ms · llm {t.get('llm_first_sentence_ms', 0)} ms · "
                     f"tts {t.get('tts_first_audio_ms', 0)} ms · total {t.get('total_ms', 0)} ms · "
                     f"{t.get('stt', '')} / {t.get('llm', '')} / {t.get('tts', '')}"
                     + (f" · fallbacks {','.join(t['fallbacks'])}" if t.get("fallbacks") else "")
                     + (f" · ERROR {t['error']}" if t.get("error") else ""))
    sip = (dbg.get("sip") or {}).get("messages") or []
    if sip:
        lines.append("  SIP:")
        lines += [f"    {m['t']:7.3f}s {'→' if m['dir'] == 'out' else '←'} {m['line']}" for m in sip]
    if dbg.get("rtp"):
        lines.append("  RTP: " + ", ".join(f"{k} {v}" for k, v in dbg["rtp"].items()))
    for ev in dbg.get("telnyx_events", []):
        lines.append(f"  Telnyx {ev.get('t', 0):7.2f}s {ev.get('event')}")
    return lines
