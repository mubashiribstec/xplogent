"""Exact-time callbacks: "call me tomorrow after five" becomes a checked, stored time.

The agent works the date out from NOW (in the prompt) and books ``at`` (ISO local time);
:func:`check` makes sure it is in the future, within 30 days and inside the hours of
whoever will call:
- ``ai`` callbacks are placed by Xplogent itself inside the calling hours, by the
  callback runner in :class:`~xplogent.voip.service.VoipService`;
- ``recruiter`` callbacks are for a person, inside the recruiter hours. They go to the
  CRM as ``recruiter_callback`` with ``callback_at``; the AI never makes them.

A refused time comes back with the nearest allowed slots, for the agent to offer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from xplogent.voip import hours

MAX_DAYS = 30
MIN_LEAD = timedelta(minutes=10)
RECRUITER_HOURS = {"enabled": True, "start": "09:00", "end": "17:30", "timezone": "Europe/London",
                   "days": {"mon": "", "tue": "", "wed": "", "thu": "", "fri": "", "sat": "closed",
                            "sun": "closed"},
                   "bank_holidays": "closed", "bank_holiday_region": "england-and-wales"}


def profile(v: dict[str, Any], who: str) -> dict[str, Any]:
    """The hours that apply to a callback by ``who`` (ai | recruiter), as a ``voip``-like dict."""
    if who == "recruiter":
        rh = {**RECRUITER_HOURS, **(v.get("recruiter_hours") or {})}
        return {"calling_hours": rh}
    return {"calling_hours": v.get("calling_hours") or {}}


def spoken(dt: datetime) -> str:
    """``Thursday 29 October at 5:30 pm`` (12-hour, as people say it)."""
    hour = dt.strftime("%I").lstrip("0") or "12"
    minute = "" if dt.minute == 0 else f":{dt.minute:02d}"
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')} at {hour}{minute} {dt.strftime('%p').lower()}"


def now_line(v: dict[str, Any], contact: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    tz, name = hours._zone(v, contact)
    local = hours._local(now, tz)
    return (f"NOW: {local.strftime('%A')} {local.day} {local.strftime('%B %Y')}, {local.strftime('%H:%M')}"
            f" ({name or 'server time'}).")


def slots(v: dict[str, Any], contact: dict[str, Any] | None, who: str, after: datetime, n: int = 3) -> list[datetime]:
    """The next ``n`` allowed times: the first opening, a little later that day, the next day."""
    p = profile(v, who)
    out: list[datetime] = []
    extra = (-after.minute) % 15
    cursor = (after + timedelta(minutes=extra)).replace(second=0, microsecond=0)   # :00 :15 :30 :45
    while len(out) < n:
        nxt = hours.next_allowed(p, contact, cursor)
        if nxt is None:
            break
        if not out or nxt - out[-1] >= timedelta(hours=2):
            out.append(nxt)
        later = nxt + timedelta(hours=3)
        cursor = later if hours.problem(p, contact, later) == "" else nxt + timedelta(hours=12)
    return out


def parse_at(text: str, tz) -> datetime | None:
    raw = str(text or "").strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def check(v: dict[str, Any], contact: dict[str, Any] | None, at: str, who: str = "ai",
          now: datetime | None = None) -> dict[str, Any]:
    """``{ok, at, spoken, reason, slots, slots_spoken}`` for a proposed callback time."""
    who = "recruiter" if who == "recruiter" else "ai"
    tz, _ = hours._zone(v, contact)
    current = hours._local(now, tz)
    when = parse_at(at, tz)

    def refuse(reason: str, after: datetime) -> dict[str, Any]:
        found = slots(v, contact, who, after)
        return {"ok": False, "at": None, "spoken": "", "reason": reason, "slots": found,
                "slots_spoken": [spoken(s.astimezone(tz)) for s in found], "who": who}

    if when is None:
        return refuse(f"'{at}' is not a date and time (use e.g. 2026-10-29T17:30)", current + MIN_LEAD)
    when = when.astimezone(tz)
    if when < current + MIN_LEAD:
        return refuse("that time has already passed or is too soon", current + MIN_LEAD)
    if when > current + timedelta(days=MAX_DAYS):
        return refuse(f"that is more than {MAX_DAYS} days away", current + MIN_LEAD)
    problem = hours.problem(profile(v, who), contact, when)
    if problem:
        who_hours = "recruiters' hours" if who == "recruiter" else "our calling hours"
        return refuse(f"that is outside {who_hours} ({problem.removeprefix('outside calling hours').strip(' :()')})",
                      when)
    return {"ok": True, "at": when, "spoken": spoken(when), "reason": "", "slots": [], "slots_spoken": [],
            "who": who}
