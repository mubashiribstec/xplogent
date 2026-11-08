"""Warm-up before an outbound call: the opening line is synthesised while the phone rings.

The CRM's Agent call page (and campaigns) send everything the call needs up front: the
vacancy, the candidate, a redacted CV excerpt. While the callee's phone rings, ``prepare``
works out the exact opening line the call will speak (the same ``opening_greeting`` the
session uses) and synthesises it, so the first words play the moment the callee says
"Hello?" instead of after a text-to-speech round trip.

What was prepared for a call is staged under ``<data>/voip/staging/`` (the brief and the
opening line). It is removed when the answered call takes it, and anything older than a
day (calls that were never answered) is swept by the callback loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from xplogent.core.config import Config, data_dir
from xplogent.core.logging import get_logger

_log = get_logger("voip.warmup")

TTL_S = 600                     # prepared audio older than this is never used
MAX_ENTRIES = 50
STAGING_MAX_AGE_S = 24 * 3600
# key -> (created, {text: (pcm chunks, sample rate)})
_CACHE: dict[str, tuple[float, dict[str, tuple[list[bytes], int]]]] = {}
_TASKS: set[asyncio.Task] = set()


def staging_dir() -> Path:
    return data_dir() / "voip" / "staging"


def _key(v: dict[str, Any], greeting: str) -> str:
    """Prepared audio is only valid for the same voice settings and the same words."""
    tts = json.dumps(v.get("tts") or {}, sort_keys=True, default=str)
    return hashlib.sha256(f"{tts}\n{greeting}".encode()).hexdigest()[:24]


def _opening(cfg: Config, number: str, meta: dict[str, Any]) -> tuple[dict[str, Any], str]:
    from xplogent.voip.service import call_template, call_variables, opening_greeting

    v = cfg.voip or {}
    info = {"direction": "outgoing", "from": "", "to": number, "line": str(meta.get("line") or ""),
            "transport": str(meta.get("transport") or ""), "brief": meta.get("brief") or {}}
    variables = call_variables(v, info, None, number)
    return v, opening_greeting(v, info, None, variables, call_template(v, info))[0]


async def prepare(cfg: Config, number: str, meta: dict[str, Any], tts: Any = None) -> str:
    """Synthesise the call's opening line now (best effort; returns the cache key, or "")."""
    from xplogent.voip.engine import split_greeting_disclosure

    try:
        v, greeting = _opening(cfg, number, meta)
        if not greeting or str(v.get("mode") or "cascaded") == "realtime":
            return ""
        key = _key(v, greeting)
        if key in _CACHE and time.time() - _CACHE[key][0] < TTL_S:
            return key
        if tts is None:
            from xplogent.voip.service import _speech_tts
            tts = _speech_tts(v)
        # The session speaks the greeting in two parts (disclosure, then the rest).
        parts = [p for p in split_greeting_disclosure(greeting) if p]
        audio: dict[str, tuple[list[bytes], int]] = {}
        try:
            for part in parts:
                audio[part] = ([c async for c in tts.synthesize(part)], int(tts.sample_rate))
        finally:
            close = getattr(tts, "aclose", None)
            if close is not None:
                await close()
        _CACHE[key] = (time.time(), audio)
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
        _stage(key, number, meta, greeting)
        _log.info("warm-up: opening line ready for %s (%d part(s))", number, len(audio))
        return key
    except Exception:  # noqa: BLE001 - a warm-up must never stop the call
        _log.warning("warm-up failed for %s: the call synthesises as usual", number, exc_info=True)
        return ""


def start(cfg: Config, number: str, meta: dict[str, Any]) -> None:
    """Begin the warm-up in the background (the dial does not wait for it)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(prepare(cfg, number, dict(meta)))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def take(v: dict[str, Any], greeting: str) -> dict[str, tuple[list[bytes], int]]:
    """The prepared audio for this greeting with these voice settings (used once), or {}."""
    key = _key(v, greeting)
    entry = _CACHE.pop(key, None)
    with _suppress():
        (staging_dir() / f"{key}.json").unlink(missing_ok=True)
    if entry is None or time.time() - entry[0] >= TTL_S:
        return {}
    return entry[1]


def _stage(key: str, number: str, meta: dict[str, Any], greeting: str) -> None:
    folder = staging_dir()
    folder.mkdir(parents=True, exist_ok=True)
    brief = {k: val for k, val in (meta.get("brief") or {}).items() if k not in ("lock_token",)}
    (folder / f"{key}.json").write_text(json.dumps(
        {"created": time.time(), "number": number, "brief": brief, "opening": greeting}, default=str),
        encoding="utf-8")


def sweep(now: float | None = None) -> int:
    """Delete staged call data older than a day (calls that were never answered)."""
    now = time.time() if now is None else now
    removed = 0
    folder = staging_dir()
    if not folder.is_dir():
        return 0
    for path in folder.glob("*.json"):
        with _suppress():
            if now - path.stat().st_mtime > STAGING_MAX_AGE_S:
                path.unlink()
                removed += 1
    for key in [k for k, (created, _) in _CACHE.items() if now - created >= TTL_S]:
        _CACHE.pop(key, None)
    return removed


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], OSError)
