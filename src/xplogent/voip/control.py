"""Live call control and outbound-call rules.

Every running call (Telnyx, SIP or a browser test) is registered as a
:class:`LiveCall`, so the dashboard can hang it up, transfer it, make the agent
say something, give the agent an instruction, or pause the AI while a person
takes over — the same way whatever the phone line.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xplogent.voip.brain import CallBrain
from xplogent.voip.engine import TRANSFER_FAILED_LINE, CallSession  # noqa: F401 - re-exported


@dataclass
class LiveCall:
    call_id: int
    session: CallSession
    brain: CallBrain
    number: str = ""
    direction: str = "inbound"
    transport: str = "telnyx"
    line: str = ""                       # SIP line the call is on: trunk | extension
    test: bool = False
    contact_name: str = ""
    # Transport-level transfer to a given number (None: this line can't transfer).
    transfer_to: Callable[[str], Awaitable[None]] | None = None
    ref: str = ""                       # carrier call id (Telnyx call_control_id / SIP Call-ID)
    via_queue: bool = False             # reached the AI through a PBX queue
    started: float = field(default_factory=time.time)

    def summary(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "number": self.number,
                "direction": self.direction, "transport": self.transport, "line": self.line,
                "test": self.test, "via_queue": self.via_queue,
                "contact_name": self.contact_name, "started": self.started,
                "paused": self.brain.paused, "notes": list(self.brain.notes),
                "can_transfer": self.transfer_to is not None}

    async def hangup(self) -> None:
        await self.session.end()

    async def transfer(self, number: str) -> None:
        """Hand the caller to ``number``. Raises if it fails at once (the agent then
        resumes and apologises); a later failure is reported by the transport."""
        if self.transfer_to is None:
            raise ValueError("this call can't be transferred")
        await self.session.hush()
        await self.session.start_transfer(lambda: self.transfer_to(number),
                                          "transferred from the dashboard")

    async def say(self, text: str) -> None:
        await self.session.say(text)

    def instruct(self, text: str) -> None:
        self.brain.instruct(text)

    def pause(self, paused: bool) -> None:
        self.brain.paused = paused


# ── outbound rules ───────────────────────────────────────────────────────────
_E164 = re.compile(r"^\+?[1-9]\d{6,14}$")


def clean_number(number: str) -> str:
    """``+1 (555) 010-0199`` → ``+15550100199``; "" if it doesn't look like a phone number."""
    raw = (number or "").strip()
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.count("+") > 1 or ("+" in digits and not digits.startswith("+")):
        return ""
    return digits if _E164.match(digits) else ""


def calling_hours_problem(v: dict[str, Any], contact: dict[str, Any] | None = None,
                          now: datetime | None = None) -> str:
    """Why an outbound call may not be placed right now ("" = fine).

    Uses the contact's time zone when known, else ``voip.calling_hours.timezone``,
    else the server's. Disabled when ``voip.calling_hours.enabled`` is false.
    """
    ch = v.get("calling_hours") or {}
    if not ch.get("enabled", True):
        return ""
    start, end = str(ch.get("start", "09:00")), str(ch.get("end", "20:00"))
    tz_name = str((contact or {}).get("timezone") or ch.get("timezone") or "").strip()
    tz = None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - unknown zone: fall back to server time
            tz = None
    current = (now.astimezone(tz) if now and tz else now) or datetime.now(tz)
    hhmm = current.strftime("%H:%M")
    inside = start <= hhmm < end if start <= end else (hhmm >= start or hhmm < end)
    if inside:
        return ""
    where = f" in {tz_name}" if tz else ""
    return f"outside calling hours ({start}–{end}{where}; it's {hhmm} there)"


def resolve_transfer_number(v: dict[str, Any], requested: str = "", *, transport: str = "") -> str:
    """The number (or SIP URI / PBX extension) a transfer goes to; "" if there is none.

    One rule for every path (agent, dashboard, Telnyx, SIP): the requested number, else
    the configured hand-off number. Short extensions only make sense on a SIP trunk.
    """
    raw = str(requested or v.get("transfer_number") or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("sip:"):
        return raw if transport in ("sip", "") else ""
    number = clean_number(raw)
    if number:
        return number
    short = re.sub(r"[\s()-]", "", raw)
    return short if transport == "sip" and re.fullmatch(r"[0-9*#+]{2,15}", short) else ""
