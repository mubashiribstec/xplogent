"""Campaigns: the automatic dialler for one vacancy's candidates (CRM API v2).

Every few seconds :meth:`Dialler.tick` looks at each switched-on campaign and dials only
when **all** of these hold:
- the phone agent is on and **Stop all** isn't pressed;
- it's inside the calling hours;
- the campaign's and today's spending caps aren't reached;
- the campaign has fewer than ``max_concurrent`` calls in progress and has started fewer than
  ``calls_per_min`` in the last minute.

Candidates come from the CRM's ``campaigns/queue``. Right before each dial the item is
checked again:
1. a fresh contact lookup (do-not-call, blocked / not interested / no job);
2. the local do-not-call list;
3. TPS when the campaign needs it (fail closed);
4. the CRM dial lock, so no recruiter rings the same person at the same moment.

When the call ends, the item moves on by outcome: no answer, busy, voicemail or abandoned
get another try later (``retry_policy``), an agreed callback is dialled at its time, and
everything else is final.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

from xplogent.core.config import Config, load_config
from xplogent.core.logging import get_logger
from xplogent.memory.store import Store
from xplogent.voip import alerts, compliance, crm, hours

_log = get_logger("voip.dialler")

RETRY_OUTCOMES = {"no_answer", "busy", "voicemail", "abandoned"}
DEFAULT_POLICY = {"max_attempts": 3, "gaps_hours": [2, 22, 24]}
MAX_AI_ATTEMPTS_7D = 3                     # across every campaign, per candidate
REFILL_BELOW = 10
REFILL_EVERY_S = 300
LINES = ("telnyx", "sip", "extension")
CONSENT = ("applied", "registered", "cold")


def unanswered_outcome(reason: str) -> str:
    """A failed or unanswered dial → the CRM outcome (SIP status codes or Telnyx causes)."""
    text = (reason or "").lower()
    if re.search(r"\b486\b|\b600\b|busy", text):
        return "busy"
    if re.search(r"\b(404|410|484|604)\b|unallocated|invalid number|not found|unobtainable", text):
        return "number_unobtainable"
    return "no_answer"


# Our own line failing (not the candidate's phone): never counts as an attempt.
_LINE_FAULT = re.compile(r"\b(401|407)\b|unauthori[sz]ed|proxy authentication|not set up|not switched on"
                         r"|not registered", re.I)


def line_fault(reason: str) -> bool:
    """The dial failed because of our line (credentials, line off), not the callee."""
    return bool(_LINE_FAULT.search(reason or ""))


def line_not_ready(line: str) -> str:
    """A SIP line that is set up to register but isn't registered yet (e.g. just after a
    restart): wait for it instead of failing dials. A line that isn't set up at all still
    fails the dial, which raises the line alert."""
    if line not in ("sip", "extension"):
        return ""
    from xplogent.voip.telephony import sip

    trunk = sip.current_line("trunk" if line == "sip" else "extension")
    if trunk is not None and trunk.s.register and not trunk.registered:
        return f"waiting for the {line} line to register"
    return ""


def policy(campaign: dict[str, Any]) -> dict[str, Any]:
    p = {**DEFAULT_POLICY, **(campaign.get("retry_policy") or {})}
    p["max_attempts"] = max(1, int(p.get("max_attempts") or 3))
    p["gaps_hours"] = [float(g) for g in (p.get("gaps_hours") or DEFAULT_POLICY["gaps_hours"])] or [24.0]
    return p


def enable_problem(cfg: Config, campaign: dict[str, Any]) -> str:
    """Why this campaign can't be switched on ("" = it can)."""
    if not campaign.get("sale_id"):
        return "pick a vacancy first"
    if campaign.get("line") not in LINES:
        return "pick the phone line the campaign calls on"
    consent = campaign.get("consent_basis") or "applied"
    if consent not in CONSENT:
        return "pick the consent basis (applied, registered or cold)"
    if consent == "cold" and not (campaign.get("cold_signoff") or {}).get("by"):
        return "cold calling needs an admin's legal sign-off first"
    if compliance.required(cfg, consent) and not compliance.configured(cfg):
        return "this campaign must be TPS-screened: set up TPS screening (Campaigns → TPS screening) first"
    if not crm.crm_enabled(cfg):
        return "the CRM link is off (Calls → Call agent → CRM)"
    return ""


class Dialler:
    def __init__(self, service: Any) -> None:
        self.service = service
        self._dials: dict[int, deque[float]] = defaultdict(deque)   # campaign -> dial start times
        self._refilled: dict[int, float] = {}                         # campaign -> last CRM refill
        self._busy = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # -- the loop ------------------------------------------------------------------
    async def loop(self) -> None:
        while True:
            every = float(((load_config().voip or {}).get("campaigns") or {}).get("tick_s", 5))
            await asyncio.sleep(max(1.0, every))
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - keep dialling tomorrow even if today broke
                _log.exception("dialler tick failed")

    async def tick(self, cfg: Config | None = None, now: float | None = None) -> dict[str, Any]:
        cfg = cfg or load_config()
        v = cfg.voip or {}
        if not v.get("enabled"):
            return {"stopped": "the phone agent is off"}
        if (v.get("campaigns") or {}).get("stop_all"):
            return {"stopped": "Stop all is on"}
        now = time.time() if now is None else now
        if self._busy.locked():
            return {"stopped": "previous tick still running"}
        async with self._busy:
            await self.sweep_stale(cfg, now)
            with Store(cfg.db_path) as store:
                campaigns = [c for c in store.list_campaigns() if c["enabled"]]
            out = {}
            for c in campaigns:
                try:
                    out[c["id"]] = await self._run_campaign(cfg, c, now)
                except Exception as exc:  # noqa: BLE001 - one broken campaign must not stop the rest
                    _log.exception("campaign %s failed", c["id"])
                    out[c["id"]] = f"error: {exc}"
            return out

    async def _run_campaign(self, cfg: Config, c: dict[str, Any], now: float) -> str:
        v = cfg.voip or {}
        if problem := hours.problem(v, None, datetime.fromtimestamp(now).astimezone()):
            return problem
        tz, _ = hours._zone(v, None)
        midnight = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with Store(cfg.db_path) as store:
            spent = store.campaign_spend_since(c["id"], midnight)
            counts = store.item_counts(c["id"])
        cap = float(c.get("daily_cost_cap") or 0)
        if cap and spent >= cap:
            self._pause(cfg, c, f"today's cap of ${cap:.2f} is reached (spent ${spent:.2f})")
            return "paused: cost cap"
        if cap and spent >= 0.8 * cap:
            alerts.alert(f"campaign_{c['id']}_80", f"Campaign '{c['name']}' has spent ${spent:.2f} today, "
                                                   f"80 % of its ${cap:.2f} cap.")
        from xplogent.voip.service import daily_cap_problem

        if problem := daily_cap_problem(cfg):
            return problem
        if problem := line_not_ready(c.get("line", "")):
            return problem
        in_flight = counts.get("dialling", 0) + counts.get("in_call", 0)
        recent = self._dials[c["id"]]
        while recent and now - recent[0] >= 60:
            recent.popleft()
        slots = min(int(c.get("max_concurrent") or 1) - in_flight, int(c.get("calls_per_min") or 1) - len(recent))
        if counts.get("queued", 0) < REFILL_BELOW and now - self._refilled.get(c["id"], 0) >= REFILL_EVERY_S:
            self._refilled[c["id"]] = now                   # at most every 5 minutes: spare the CRM
            await self.refill(cfg, c, now)
        if slots <= 0:
            return "at its limit"
        with Store(cfg.db_path) as store:
            due = store.due_items(c["id"], now, slots)
        placed = 0
        for item in due:
            result = await self._dial_item(cfg, c, item, now)
            if result == "stop":                    # TPS or the CRM unreachable: fail closed
                return "stopped this round (checks unavailable)"
            placed += result == "dialled"
        return f"dialled {placed}"

    def _pause(self, cfg: Config, c: dict[str, Any], why: str) -> None:
        with Store(cfg.db_path) as store:
            store.update_campaign(c["id"], {"enabled": 0, "paused_reason": why})
        alerts.alert(f"campaign_{c['id']}_paused", f"Campaign '{c['name']}' paused: {why}. Calls in progress finish.")

    # -- candidates ------------------------------------------------------------------
    async def refill(self, cfg: Config, c: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        """Queue the CRM's next candidates for this vacancy (duplicates are ignored)."""
        now = time.time() if now is None else now
        res = await crm.campaign_queue(cfg, int(c["sale_id"]), radius_km=float(c.get("radius_km") or 15), limit=50)
        if not res["ok"]:
            if res["error"] == "sale_not_callable":
                self._pause(cfg, c, "the vacancy is closed, on hold or has reached its CV limit")
            return {"ok": False, "error": res["error"], "added": 0}
        rows, too_many = [], 0
        with Store(cfg.db_path) as store:
            if res["sale"]:
                store.update_campaign(c["id"], {"sale": res["sale"]})
            for cand in res["candidates"]:
                if store.ai_attempts_since(cand["applicant_id"], now - 7 * 86400) >= MAX_AI_ATTEMPTS_7D:
                    too_many += 1
                    continue
                rows.append({"applicant_id": cand["applicant_id"], "phone": cand["phone_e164"],
                             "name": cand.get("name", ""),
                             "brief": {"applicant_id": cand["applicant_id"], "sale_id": c["sale_id"],
                                       "sale": res["sale"] or c.get("sale") or {}, "candidate": cand}})
            added = store.add_campaign_items(c["id"], rows)
        return {"ok": True, "added": added, "skipped": {**res["skipped"], "called_3_times_this_week": too_many}}

    def _skip(self, cfg: Config, item: dict[str, Any], reason: str) -> str:
        with Store(cfg.db_path) as store:
            store.update_item(item["id"], {"state": "skipped", "skip_reason": reason})
        return "skipped"

    async def _dial_item(self, cfg: Config, c: dict[str, Any], item: dict[str, Any], now: float) -> str:
        phone = item["phone"]
        found = await crm.lookup(cfg, phone, direction="outbound", sale_id=c.get("sale_id"))
        if not found["ok"]:
            return "stop"                                  # can't confirm do-not-call: don't dial
        contact = found.get("contact") or {}
        if contact.get("do_not_call"):
            return self._skip(cfg, item, "do not call (CRM)")
        flags = contact.get("flags") or {}
        for flag, why in (("blocked", "blocked in the CRM"), ("temp_not_interested", "not interested"),
                          ("no_job", "not looking for work")):
            if flags.get(flag):
                return self._skip(cfg, item, why)
        with Store(cfg.db_path) as store:
            if store.is_dnc(phone):
                return self._skip(cfg, item, "do-not-call list")
            if compliance.required(cfg, c.get("consent_basis") or "applied"):
                tps = await compliance.screen(cfg, store, [phone])
                if not tps["ok"]:
                    alerts.alert("tps_unreachable", f"TPS screening failed, so campaign '{c['name']}' is not "
                                                    f"dialling: {tps['error']}")
                    return "stop"
                if reg := compliance.registered(tps["results"].get(phone)):
                    return self._skip(cfg, item, f"registered with {reg.upper()}")
        max_s = int(c.get("max_call_seconds") or 480)
        lock = await crm.dial_lock(cfg, phone, item["applicant_id"], ttl_seconds=max_s + 300)
        if lock["status"] == 409:
            with Store(cfg.db_path) as store:
                store.update_item(item["id"], {"state": "retry_wait", "next_at": now + 2 * 3600,
                                               "skip_reason": f"being called by {lock['locked_by'] or 'someone'}"})
            return "locked"
        if not lock["ok"]:
            return "stop"
        p = policy(c)
        attempt = int(item["attempts"]) + 1
        brief = {**item["brief"], "campaign_id": c["id"], "campaign_item_id": item["id"], "attempt": attempt,
                 "final_attempt": attempt >= p["max_attempts"], "max_call_seconds": max_s,
                 "per_call_cost_cap": float(c.get("per_call_cost_cap") or 0), "lock_token": lock["lock_token"]}
        with Store(cfg.db_path) as store:
            store.update_item(item["id"], {"state": "dialling", "attempts": attempt, "brief": brief,
                                           "skip_reason": ""})
        self._dials[c["id"]].append(now)
        res = await self.service.dial(phone, cfg, via=c["line"], brief=brief)
        if res.get("ok"):
            return "dialled"
        if res.get("hours") or res.get("cost_cap"):           # a rule changed in between: not an attempt
            with Store(cfg.db_path) as store:
                store.update_item(item["id"], {"state": "queued", "attempts": attempt - 1})
            await crm.dial_unlock(cfg, phone, lock["lock_token"])
            return "held"
        if "do-not-call" in str(res.get("error")):
            await crm.dial_unlock(cfg, phone, lock["lock_token"])
            return self._skip(cfg, item, "do-not-call list")
        if line_fault(str(res.get("error"))):
            await self._line_fault(cfg, c, item["id"], attempt, phone, lock["lock_token"], str(res.get("error")))
            return "stop"
        await self.record_unanswered(cfg, item["id"], str(res.get("error") or "dial failed"))
        return "failed"

    async def _line_fault(self, cfg: Config, c: dict[str, Any], item_id: int, attempt: int, phone: str,
                          lock_token: str, error: str) -> None:
        """Our line refused the dial: put the item back untouched and tell someone."""
        with Store(cfg.db_path) as store:
            store.update_item(item_id, {"state": "queued", "attempts": max(0, attempt - 1),
                                        "skip_reason": f"line problem: {error[:120]}"})
        await crm.dial_unlock(cfg, phone, lock_token)
        alerts.alert("campaign_line", f"Campaign '{c.get('name', '')}' can't dial: the {c.get('line', '')} line "
                                      f"refused the call ({error[:160]}). Check the line in Calls → Telephony; "
                                      "nobody's attempts were used up.")

    # -- results ---------------------------------------------------------------------
    async def record_unanswered(self, cfg: Config, item_id: int, reason: str) -> None:
        """A dial that never became a conversation: log it as a call (so the CRM hears about
        the attempt), then move the item on."""
        with Store(cfg.db_path) as store:
            item = store.get_item(item_id)
            if item is None or item["state"] not in ("dialling", "in_call"):
                return
            campaign = store.get_campaign(item["campaign_id"]) or {}
        if line_fault(reason):           # reported after the dial was placed
            await self._line_fault(cfg, campaign, item_id, int(item["attempts"]), item["phone"],
                                   str(item["brief"].get("lock_token") or ""), reason)
            return
        with Store(cfg.db_path) as store:
            outcome = unanswered_outcome(reason)
            call_id = store.start_call("outbound", "", item["phone"], transport="", line="")
            store.set_call_brief(call_id, item["brief"])
            store.end_call(call_id, outcome=outcome, summary=f"Not answered ({reason[:120]})")
            if crm.crm_enabled(cfg):
                payload = crm.build_payload(cfg, store, call_id, {"outcome": outcome})
                crm.queue_summary(cfg, store, call_id, payload)
        await self.advance(cfg, item_id, outcome, call_id)

    async def advance(self, cfg: Config, item_id: int, outcome: str, call_id: int | None = None,
                      callback_at: str = "") -> None:
        """Move an item on after a call or attempt, and release the CRM dial lock."""
        v = cfg.voip or {}
        with Store(cfg.db_path) as store:
            item = store.get_item(item_id)
            if item is None:
                return
            c = store.get_campaign(item["campaign_id"]) or {}
            p = policy(c)
            attempts = int(item["attempts"])
            fields: dict[str, Any] = {"last_outcome": outcome, "last_call_id": call_id}
            if outcome == "callback" and callback_at:
                at = datetime.fromisoformat(callback_at)
                fields.update(state="retry_wait", next_at=at.timestamp(), attempts=max(0, attempts - 1))
            elif outcome in RETRY_OUTCOMES and attempts < p["max_attempts"]:
                gap = p["gaps_hours"][min(attempts - 1, len(p["gaps_hours"]) - 1)]
                after = datetime.now().astimezone() + timedelta(hours=gap)
                nxt = hours.next_allowed(v, None, after) or after
                fields.update(state="retry_wait", next_at=nxt.timestamp())
            else:
                fields.update(state="done")
            store.update_item(item_id, fields)
        try:
            await crm.dial_unlock(cfg, item["phone"], str(item["brief"].get("lock_token") or ""))
        except Exception:  # noqa: BLE001 - the lock expires by itself
            pass

    def call_finished(self, cfg: Config, brief: dict[str, Any], outcome: str, call_id: int,
                      callback_at: str = "") -> None:
        """From finalize_call (sync): move the campaign item on in the background."""
        item_id = brief.get("campaign_item_id")
        if not item_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.advance(cfg, int(item_id), outcome, call_id, callback_at))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def call_started(self, cfg: Config, brief: dict[str, Any], call_id: int) -> None:
        if brief.get("campaign_item_id"):
            with Store(cfg.db_path) as store:
                store.update_item(int(brief["campaign_item_id"]), {"state": "in_call", "last_call_id": call_id})

    def not_answered(self, meta: dict[str, Any], reason: str) -> None:
        """A line reports that a dial rang out / was busy / failed after it was placed."""
        item_id = (meta.get("brief") or {}).get("campaign_item_id")
        if not item_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.record_unanswered(load_config(), int(item_id), reason))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def sweep_stale(self, cfg: Config, now: float) -> None:
        """Items stuck in dialling / in call with no live call (a carrier that never said)."""
        with Store(cfg.db_path) as store:
            stale = store.stale_items(now - 600)
        live = {(getattr(x.brain, "brief", None) or {}).get("campaign_item_id")
                for x in getattr(self.service, "active", {}).values()}
        for item in stale:
            if item["id"] not in live:
                await self.record_unanswered(cfg, item["id"], "no answer (the line gave no result)")
