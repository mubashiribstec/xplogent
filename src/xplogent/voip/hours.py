"""Calling hours that know the calendar: a window per weekday and UK bank holidays.

``voip.calling_hours`` holds the profile::

    start: "09:00", end: "20:00"          # the default window
    days: {mon: "", ..., sat: "10:00-16:00", sun: closed}   # "" = the default window
    bank_holidays: closed                 # or open
    bank_holiday_region: england-and-wales  # gov.uk division: scotland, northern-ireland
    timezone: Europe/London               # the contact's own time zone wins

Without ``days`` every day uses start/end, and without ``bank_holidays`` holidays are
ordinary days, so older settings keep working. Bank holidays come from gov.uk, cached
for a week; a dial never waits on the network (bundled dates are the fallback).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path
from typing import Any

from xplogent.core.config import data_dir
from xplogent.core.logging import get_logger

_log = get_logger("voip.hours")

GOV_UK_URL = "https://www.gov.uk/bank-holidays.json"
REFRESH_S = 7 * 86400
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
REGIONS = ("england-and-wales", "scotland", "northern-ireland")
_CLOSED = {"closed", "off", "no", "none"}
_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})$")

# Bundled fallback, generated from https://www.gov.uk/bank-holidays.json on 2026-10-28 (2026-2028).
BUNDLED_BANK_HOLIDAYS: dict[str, dict[str, str]] = {
    "england-and-wales": {
        "2026-01-01": "New Year's Day",
        "2026-04-03": "Good Friday",
        "2026-04-06": "Easter Monday",
        "2026-05-04": "Early May bank holiday",
        "2026-05-25": "Spring bank holiday",
        "2026-08-31": "Summer bank holiday",
        "2026-12-25": "Christmas Day",
        "2026-12-28": "Boxing Day",
        "2027-01-01": "New Year's Day",
        "2027-03-26": "Good Friday",
        "2027-03-29": "Easter Monday",
        "2027-05-03": "Early May bank holiday",
        "2027-05-31": "Spring bank holiday",
        "2027-08-30": "Summer bank holiday",
        "2027-12-27": "Christmas Day",
        "2027-12-28": "Boxing Day",
        "2028-01-03": "New Year's Day",
        "2028-04-14": "Good Friday",
        "2028-04-17": "Easter Monday",
        "2028-05-01": "Early May bank holiday",
        "2028-05-29": "Spring bank holiday",
        "2028-08-28": "Summer bank holiday",
        "2028-12-25": "Christmas Day",
        "2028-12-26": "Boxing Day",
    },
    "scotland": {
        "2026-01-01": "New Year's Day",
        "2026-01-02": "2nd January",
        "2026-04-03": "Good Friday",
        "2026-05-04": "Early May bank holiday",
        "2026-05-25": "Spring bank holiday",
        "2026-06-15": "World Cup bank holiday",
        "2026-08-03": "Summer bank holiday",
        "2026-11-30": "St Andrew's Day",
        "2026-12-25": "Christmas Day",
        "2026-12-28": "Boxing Day",
        "2027-01-01": "New Year's Day",
        "2027-01-04": "2nd January",
        "2027-03-26": "Good Friday",
        "2027-05-03": "Early May bank holiday",
        "2027-05-31": "Spring bank holiday",
        "2027-08-02": "Summer bank holiday",
        "2027-11-30": "St Andrew's Day",
        "2027-12-27": "Christmas Day",
        "2027-12-28": "Boxing Day",
        "2028-01-03": "New Year's Day",
        "2028-01-04": "2nd January",
        "2028-04-14": "Good Friday",
        "2028-05-01": "Early May bank holiday",
        "2028-05-29": "Spring bank holiday",
        "2028-08-07": "Summer bank holiday",
        "2028-11-30": "St Andrew's Day",
        "2028-12-25": "Christmas Day",
        "2028-12-26": "Boxing Day",
    },
    "northern-ireland": {
        "2026-01-01": "New Year's Day",
        "2026-03-17": "St Patrick's Day",
        "2026-04-03": "Good Friday",
        "2026-04-06": "Easter Monday",
        "2026-05-04": "Early May bank holiday",
        "2026-05-25": "Spring bank holiday",
        "2026-07-13": "Battle of the Boyne (Orangemen's Day)",
        "2026-08-31": "Summer bank holiday",
        "2026-12-25": "Christmas Day",
        "2026-12-28": "Boxing Day",
        "2027-01-01": "New Year's Day",
        "2027-03-17": "St Patrick's Day",
        "2027-03-26": "Good Friday",
        "2027-03-29": "Easter Monday",
        "2027-05-03": "Early May bank holiday",
        "2027-05-31": "Spring bank holiday",
        "2027-07-12": "Battle of the Boyne (Orangemen's Day)",
        "2027-08-30": "Summer bank holiday",
        "2027-12-27": "Christmas Day",
        "2027-12-28": "Boxing Day",
        "2028-01-03": "New Year's Day",
        "2028-03-17": "St Patrick's Day",
        "2028-04-14": "Good Friday",
        "2028-04-17": "Easter Monday",
        "2028-05-01": "Early May bank holiday",
        "2028-05-29": "Spring bank holiday",
        "2028-07-12": "Battle of the Boyne (Orangemen's Day)",
        "2028-08-28": "Summer bank holiday",
        "2028-12-25": "Christmas Day",
        "2028-12-26": "Boxing Day",
    },
}


# ── bank holidays ────────────────────────────────────────────────────────────
def cache_path() -> Path:
    return data_dir() / "voip" / "bank-holidays.json"


_MEMO: dict[str, Any] = {"mtime": None, "data": {}}


def _cached() -> dict[str, Any]:
    path = cache_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _MEMO["mtime"] != mtime:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _MEMO.update(mtime=mtime, data=data if isinstance(data, dict) else {})
    return _MEMO["data"]


def bank_holidays(region: str = "england-and-wales") -> dict[str, str]:
    """``{"2026-12-25": "Christmas Day", ...}``: the bundled dates plus gov.uk's latest."""
    out = dict(BUNDLED_BANK_HOLIDAYS.get(region, {}))
    fetched = (_cached().get("regions") or {}).get(region)
    if isinstance(fetched, dict):
        out.update({str(k): str(v) for k, v in fetched.items()})
    return out


def parse_gov_uk(data: Any) -> dict[str, dict[str, str]]:
    """gov.uk's ``{division: {"events": [{"date", "title"}]}}`` → ``{division: {date: title}}``."""
    out: dict[str, dict[str, str]] = {}
    if not isinstance(data, dict):
        return out
    for region in REGIONS:
        events = (data.get(region) or {}).get("events") if isinstance(data.get(region), dict) else None
        days = {}
        for ev in events or []:
            when, title = str((ev or {}).get("date", "")), str((ev or {}).get("title", ""))[:80]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
                days[when] = title
        if days:
            out[region] = days
    return out


async def refresh_bank_holidays(*, force: bool = False, transport=None) -> dict[str, Any]:
    """Fetch gov.uk's list when the cache is older than a week. Never raises.

    ``XPLOGENT_BANK_HOLIDAYS_URL`` overrides the address; set it empty to stay offline."""
    import httpx

    url = os.environ.get("XPLOGENT_BANK_HOLIDAYS_URL", GOV_UK_URL)
    fetched_at = float(_cached().get("fetched_at") or 0)
    if not url:
        return {"ok": False, "status": "offline", "fetched_at": fetched_at}
    if not force and time.time() - fetched_at < REFRESH_S:
        return {"ok": True, "status": "fresh", "fetched_at": fetched_at}
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        regions = parse_gov_uk(resp.json())
        if sum(len(d) for d in regions.values()) < 5:
            raise ValueError("the list has no bank holidays")
    except Exception as exc:  # noqa: BLE001 - keep the cached / bundled dates
        _log.warning("could not refresh UK bank holidays (%s): using the saved list", exc)
        return {"ok": False, "status": "failed", "error": str(exc)[:200], "fetched_at": fetched_at}
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "source": url, "regions": regions}),
                   encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "status": "updated", "fetched_at": time.time()}


async def refresh_loop() -> None:
    """Started with the API: checks once a day, downloads once a week."""
    import asyncio

    while True:
        await refresh_bank_holidays()
        await asyncio.sleep(86400)


# ── the profile ──────────────────────────────────────────────────────────────
def parse_window(raw: Any) -> tuple[str, str] | None | str:
    """``"10:00-16:00"`` → ("10:00", "16:00"); ``closed`` → None; ``""`` → "" (the
    default window); anything else → "bad"."""
    text = str(raw if raw is not None else "").strip().lower()
    if not text:
        return ""
    if text in _CLOSED or raw is False:
        return None
    m = _RANGE.match(text)
    if not m or int(m[1]) > 23 or int(m[3]) > 24 or int(m[2]) > 59 or int(m[4]) > 59:
        return "bad"
    return f"{int(m[1]):02d}:{m[2]}", f"{int(m[3]):02d}:{m[4]}"


def profile_problems(ch: Any) -> list[str]:
    """Settings mistakes in ``voip.calling_hours`` (for the startup check)."""
    if not isinstance(ch, dict):
        return []
    out = []
    for key in ("start", "end"):
        if key in ch and not re.fullmatch(r"\d{2}:\d{2}", str(ch[key])):
            out.append(f"voip.calling_hours.{key} should look like 09:00, not {ch[key]!r}")
    for day, raw in (ch.get("days") or {}).items() if isinstance(ch.get("days"), dict) else []:
        if parse_window(raw) == "bad":
            out.append(f"voip.calling_hours.days.{day} should be closed, empty (the default "
                       f"hours) or like 10:00-16:00, not {raw!r}")
    if str(ch.get("bank_holiday_region") or "england-and-wales") not in REGIONS:
        out.append("voip.calling_hours.bank_holiday_region should be one of " + ", ".join(REGIONS))
    return out


def _holidays_closed(ch: dict[str, Any]) -> bool:
    flag = ch.get("bank_holidays")
    return flag is True or str(flag or "").strip().lower() == "closed"


def day_window(ch: dict[str, Any], day: date, holidays: dict[str, str] | None = None) -> tuple[str, str] | None:
    """The (start, end) window that opens on ``day``, or None when it's closed."""
    if holidays is not None and _holidays_closed(ch) and day.isoformat() in holidays:
        return None
    days = ch.get("days") if isinstance(ch.get("days"), dict) else {}
    w = parse_window(days.get(DAYS[day.weekday()], ""))
    if w is None or w == "bad":                        # a broken setting fails safe: closed
        return None
    start, end = w or (str(ch.get("start", "09:00")), str(ch.get("end", "20:00")))
    return None if start == end else (start, end)


def _zone(v: dict[str, Any], contact: dict[str, Any] | None) -> tuple[tzinfo, str]:
    ch = v.get("calling_hours") or {}
    name = str((contact or {}).get("timezone") or ch.get("timezone") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name), name
        except Exception:  # noqa: BLE001 - unknown zone: fall back to server time
            pass
    return datetime.now().astimezone().tzinfo, ""


def _local(now: datetime | None, tz: tzinfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    return (now if now.tzinfo else now.astimezone()).astimezone(tz)


def _holidays_for(ch: dict[str, Any]) -> dict[str, str] | None:
    if not _holidays_closed(ch):
        return None
    return bank_holidays(str(ch.get("bank_holiday_region") or "england-and-wales"))


def is_open(ch: dict[str, Any], local: datetime, holidays: dict[str, str] | None = None) -> bool:
    hhmm = local.strftime("%H:%M")
    today = day_window(ch, local.date(), holidays)
    if today:
        start, end = today
        if (start <= hhmm < end) if start < end else hhmm >= start:
            return True
    yesterday = day_window(ch, local.date() - timedelta(days=1), holidays)
    return bool(yesterday and yesterday[0] > yesterday[1] and hhmm < yesterday[1])


def problem(v: dict[str, Any], contact: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    """Why an outbound call may not be placed right now ("" = fine)."""
    ch = v.get("calling_hours") or {}
    if not ch.get("enabled", True):
        return ""
    tz, tz_name = _zone(v, contact)
    local = _local(now, tz)
    holidays = _holidays_for(ch)
    if is_open(ch, local, holidays):
        return ""
    where = f" in {tz_name}" if tz_name else ""
    today = local.date().isoformat()
    if holidays is not None and today in holidays:
        return f"outside calling hours: UK bank holiday ({holidays[today]}){where}"
    w = day_window(ch, local.date())
    if w is None and isinstance(ch.get("days"), dict):
        return f"outside calling hours: no calls on {DAY_NAMES[local.weekday()]}s{where}"
    start, end = w or (str(ch.get("start", "09:00")), str(ch.get("end", "20:00")))
    day = f"{DAYS[local.weekday()].capitalize()} " if isinstance(ch.get("days"), dict) else ""
    return f"outside calling hours ({day}{start}–{end}{where}; it's {local.strftime('%H:%M')} there)"


def next_allowed(v: dict[str, Any], contact: dict[str, Any] | None = None,
                 after: datetime | None = None, *, days: int = 14) -> datetime | None:
    """The first moment at or after ``after`` when a call may be placed (time-zone aware),
    or None when nothing is open in the next ``days`` days."""
    ch = v.get("calling_hours") or {}
    tz, _ = _zone(v, contact)
    local = _local(after, tz).replace(second=0, microsecond=0)
    if after is not None and _local(after, tz) > local:
        local += timedelta(minutes=1)                  # never earlier than asked
    if not ch.get("enabled", True):
        return local
    holidays = _holidays_for(ch)
    if is_open(ch, local, holidays):
        return local
    for i in range(days + 1):
        day = local.date() + timedelta(days=i)
        w = day_window(ch, day, holidays)
        if w:
            hh, mm = (int(x) for x in w[0].split(":"))
            candidate = datetime.combine(day, dtime(hh, mm), tz)
            if candidate > local:
                return candidate
    return None


def describe(v: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """The profile as the dashboard shows it: each weekday's window, today and what's next."""
    ch = v.get("calling_hours") or {}
    tz, tz_name = _zone(v, None)
    local = _local(now, tz)
    holidays = _holidays_for(ch)
    region = str(ch.get("bank_holiday_region") or "england-and-wales")
    upcoming = sorted((d, n) for d, n in bank_holidays(region).items() if d >= local.date().isoformat())[:5]
    nxt = next_allowed(v, None, now)
    week = []
    for i, key in enumerate(DAYS):
        day = local.date() + timedelta(days=(i - local.weekday()) % 7)
        w = day_window(ch, day)
        week.append({"day": key, "name": DAY_NAMES[i], "window": f"{w[0]}–{w[1]}" if w else "closed"})
    return {"enabled": bool(ch.get("enabled", True)), "timezone": tz_name or "server time",
            "now": local.isoformat(timespec="minutes"), "open_now": problem(v, None, now) == "",
            "reason": problem(v, None, now), "next_allowed": nxt.isoformat(timespec="minutes") if nxt else None,
            "week": week, "bank_holidays": "closed" if holidays is not None else "open",
            "bank_holiday_region": region,
            "upcoming_bank_holidays": [{"date": d, "name": n} for d, n in upcoming],
            "bank_holidays_fetched_at": _cached().get("fetched_at")}
