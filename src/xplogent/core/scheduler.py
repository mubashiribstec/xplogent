"""Natural-language scheduler (cron without the cryptic syntax).

Define recurring or one-off agent jobs in plain English — "every day at 9am",
"every 2 hours", "every monday at 08:00", "in 30 minutes" — or a raw 5-field cron
string (needs the optional ``croniter`` package). Jobs are persisted in SQLite so
they survive restarts, run with full tool/skill/memory access, and are unattended
(auto-approve up to HIGH risk; critical stays blocked).

The :class:`Scheduler` runs as an asyncio loop inside the API server process.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta
from typing import Any

from xplogent.core.logging import get_logger
from xplogent.memory.store import Store

_log = get_logger("scheduler")

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _tzinfo(tz: str):
    if tz:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - bad/unknown tz → local
            return None
    return None


def _now(tz: str) -> datetime:
    info = _tzinfo(tz)
    return datetime.now(info) if info else datetime.now().astimezone()


def _parse_time(s: str) -> tuple[int, int]:
    """Parse '9am', '9:30pm', '14:00', '9' → (hour, minute)."""
    s = s.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", s)
    if not m:
        raise ValueError(f"could not parse time '{s}'")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"time out of range '{s}'")
    return hour, minute


def canonical_spec(text: str) -> str:
    """Normalize a human schedule string; raises ValueError if unrecognized."""
    spec = " ".join(text.strip().lower().split())
    # Validate by attempting to compute a next run from "now".
    if next_run_after(spec, "") is None and not spec.startswith("in "):
        raise ValueError(f"unrecognized schedule: '{text}'")
    return spec


def next_run_after(spec: str, tz: str, after: float | None = None) -> float | None:
    """Return the next fire time (epoch seconds) strictly after ``after``.

    ``None`` means the job will not run again (a one-shot that already fired, or
    an unparseable spec).
    """
    info = _tzinfo(tz)
    if after is not None:
        base = datetime.fromtimestamp(after, info) if info \
            else datetime.fromtimestamp(after).astimezone()
    else:
        base = _now(tz)
    spec = spec.strip().lower()

    # one-shot: "in N minutes/hours/days" — only fires from creation time, never again.
    if spec.startswith("in ") or spec == "once":
        if after:
            return None  # already fired once
        m = re.fullmatch(r"in (\d+) (minute|hour|day)s?", spec)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n),
                 "day": timedelta(days=n)}[unit]
        return (_now(tz) + delta).timestamp()

    # interval: "every N minutes/hours"
    m = re.fullmatch(r"every (?:(\d+) )?(minute|hour)s?", spec)
    if m:
        n = int(m.group(1) or 1)
        delta = timedelta(minutes=n) if m.group(2) == "minute" else timedelta(hours=n)
        return (base + delta).timestamp()

    # weekly: "every <weekday> at <time>"
    m = re.fullmatch(r"every (\w+) at (.+)", spec)
    if m and m.group(1) in _WEEKDAYS:
        target_dow = _WEEKDAYS[m.group(1)]
        hour, minute = _parse_time(m.group(2))
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_dow - cand.weekday()) % 7
        cand = cand + timedelta(days=days_ahead)
        if cand <= base:
            cand = cand + timedelta(days=7)
        return cand.timestamp()

    # daily: "every day at <time>" / "daily at <time>" / "at <time>"
    m = re.fullmatch(r"(?:every day|daily|each day) at (.+)", spec) \
        or re.fullmatch(r"at (.+)", spec)
    if m:
        hour, minute = _parse_time(m.group(1))
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= base:
            cand = cand + timedelta(days=1)
        return cand.timestamp()

    # raw cron (5 fields) via optional croniter
    if len(spec.split()) == 5 and re.fullmatch(r"[\d*/,\-\s]+", spec):
        try:
            from croniter import croniter

            info = _tzinfo(tz)
            start = datetime.fromtimestamp(after, info) if after else _now(tz)
            return croniter(spec, start).get_next(float)
        except Exception:  # noqa: BLE001 - croniter missing or bad expr
            _log.warning("cron spec %r needs the 'croniter' package", spec)
            return None

    return None


def parse_schedule(text: str, tz: str = "") -> tuple[str, float]:
    """Validate a schedule string and return ``(canonical_spec, first_next_run)``."""
    spec = canonical_spec(text)
    nxt = next_run_after(spec, tz)
    if nxt is None:
        raise ValueError(f"could not compute a run time for '{text}'")
    return spec, nxt


# Unattended runs approve everything except CRITICAL (shared implementation).
from xplogent.safety.approval import auto_approve_up_to_high as _auto_approve  # noqa: E402


class Scheduler:
    def __init__(self, config: Any, *, tick: float = 30.0) -> None:
        self.config = config
        self.tick = tick
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._realign()
        self._task = asyncio.create_task(self._loop())
        _log.info("scheduler started (tick=%ss)", self.tick)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    def _realign(self) -> None:
        """On startup, recompute next_run for enabled jobs so none are stuck."""
        store = Store(self.config.db_path)
        try:
            now = time.time()
            for job in store.list_schedules():
                if not job.get("enabled"):
                    continue
                nr = job.get("next_run")
                if nr is None or nr < now - 1:
                    nxt = next_run_after(job["spec"], job.get("tz") or "")
                    store.set_schedule_next(job["id"], nxt)
        finally:
            store.close()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick_once()
            except Exception:  # noqa: BLE001 - a bad job must not kill the loop
                _log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick)
            except TimeoutError:
                pass

    async def _tick_once(self) -> None:
        store = Store(self.config.db_path)
        try:
            for job in store.due_schedules(time.time()):
                await self._fire(store, job)
        finally:
            store.close()

    async def _fire(self, store: Store, job: dict[str, Any]) -> None:
        now = time.time()
        tz = job.get("tz") or ""
        nxt = next_run_after(job["spec"], tz, after=now)
        # Advance next_run up front so a long job can't double-fire on the next tick.
        store.set_schedule_run(job["id"], last_run=now, last_status="running", next_run=nxt)
        _log.info("running schedule %s: %s", job["id"], job["name"])
        status = "ok"
        try:
            await self._run_job(job)
        except Exception as exc:  # noqa: BLE001
            status = f"error: {exc}"
            _log.exception("schedule %s failed", job["id"])
        store.set_schedule_run(job["id"], last_run=now, last_status=status, next_run=nxt)
        if nxt is None:  # one-shot finished
            store.set_schedule_enabled(job["id"], False)

    async def _run_job(self, job: dict[str, Any]) -> None:
        from xplogent.core.events import EventBus
        from xplogent.runtime import build_orchestrator, build_runtime

        prompt = job["prompt"]
        approve = _auto_approve()
        if (job.get("mode") or "agent") == "team":
            runtime = build_orchestrator(bus=EventBus(), approve=approve)
            try:
                await runtime.orchestrator.run_goal(prompt)
            finally:
                await runtime.aclose()
        else:
            runtime = build_runtime(bus=EventBus(), approve=approve)
            try:
                await runtime.agent.run(prompt)
            finally:
                await runtime.aclose()
