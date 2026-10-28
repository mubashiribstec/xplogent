"""A stand-in for the CRM's ``/api/xplogent/*`` API (contract v2, docs/crm-integration-prompt.md).

It checks requests exactly the way the real CRM must:
- the bearer token;
- the V2 signature over ``"<timestamp>.<body>"``;
- the clock skew and the body size.

It answers every endpoint with sample vacancies and candidates, so the whole flow can be
tried before the CRM side is deployed: Test connection, lookups, campaign queues, dial
locks, call summaries.

Tests mount it in-process (``httpx.ASGITransport(app=create_app(token))``); a person can run
``xplogent voip mock-crm --port 8089`` and point Settings → CRM at ``http://127.0.0.1:8089``.
``GET /_mock/state`` (unsigned) shows what it received.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MAX_SKEW = 300
MAX_BODY = 256 * 1024

SAMPLE_SALES: list[dict[str, Any]] = [
    {"sale_id": 45, "job_title": "Care Assistant", "job_category": "Care", "postcode": "LS6 2AB",
     "postcode_district": "LS6", "town": "Leeds", "office_name": "Oak Care Group", "unit_name": "Oak House",
     "position_type": "Full time", "job_type": "regular",
     "timing": "Days, 3 x 12h shifts incl. alternate weekends", "salary": "£12.60 per hour",
     "experience": "6 months in care", "qualification": "Care Certificate preferred",
     "benefits": "Paid training", "description": "Supporting older residents with daily living.",
     "cvs_sent": 3, "cv_limit": 8, "status": 1, "created_at": "2026-10-20T09:12:00Z"},
    {"sale_id": 46, "job_title": "Senior Carer", "job_category": "Care", "postcode": "BD1 1AA",
     "postcode_district": "BD1", "town": "Bradford", "office_name": "Oak Care Group", "unit_name": "Elm Court",
     "position_type": "Full time", "job_type": "regular", "timing": "Nights, 4 x 10h",
     "salary": "£13.40 per hour", "experience": "1 year as a carer", "qualification": "NVQ 3",
     "benefits": "", "description": "Leading a small night team.",
     "cvs_sent": 8, "cv_limit": 8, "status": 1, "created_at": "2026-10-18T10:00:00Z"},
]
SAMPLE_CANDIDATES: list[dict[str, Any]] = [
    {"applicant_id": 123, "name": "Sarah Jones", "phone": "07700900123", "postcode_district": "LS7",
     "distance_km": 4.2, "job_title": "Care Assistant", "blocked": False, "temp_not_interested": False,
     "no_job": False, "callback": False, "email": "sarah.jones@example.com"},
    {"applicant_id": 124, "name": "Tom Hughes", "phone": "07700900124", "postcode_district": "LS4",
     "distance_km": 6.8, "job_title": "Care Assistant", "blocked": False, "temp_not_interested": False,
     "no_job": False, "callback": True, "email": ""},
    {"applicant_id": 125, "name": "Priya Shah", "phone": "07700900125", "postcode_district": "LS2",
     "distance_km": 2.1, "job_title": "Care Assistant", "blocked": True, "temp_not_interested": False,
     "no_job": False, "callback": False, "email": ""},
]


def e164(national: str) -> str:
    digits = re.sub(r"\D", "", national or "")
    if digits.startswith("44"):
        return "+" + digits
    return "+44" + digits[1:] if digits.startswith("0") and len(digits) == 11 else ""


def tail(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


class MockCRM:
    def __init__(self, token: str) -> None:
        self.token = token
        self.sales = copy.deepcopy(SAMPLE_SALES)
        self.candidates = copy.deepcopy(SAMPLE_CANDIDATES)
        self.events: dict[int, dict[str, Any]] = {}       # call_id -> call-summary body
        self.locks: dict[str, dict[str, Any]] = {}        # phone tail -> {by, expires}
        self.emails: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []          # endpoint, status (no personal data)

    # -- auth (the CRM's VerifyXplogentSignature middleware) ------------------------
    def check(self, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]] | None:
        if not self.token:
            return 503, {"ok": False, "error": "disabled", "message": "XPLOGENT_API_TOKEN is not set"}
        if len(body) > MAX_BODY:
            return 413, {"ok": False, "error": "too_large", "message": "body over 256 KB"}
        auth = headers.get("authorization", "")
        ts = headers.get("x-xplogent-timestamp", "")
        sig = headers.get("x-xplogent-signature-v2", "")
        if not hmac.compare_digest(auth, f"Bearer {self.token}"):
            return 401, {"ok": False, "error": "unauthorized", "message": "bad token"}
        if not ts.isdigit():
            return 401, {"ok": False, "error": "unauthorized", "message": "timestamp missing"}
        if abs(time.time() - int(ts)) > MAX_SKEW:
            return 401, {"ok": False, "error": "stale", "message": "timestamp skew too large"}
        want = "sha256=" + hmac.new(self.token.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return 401, {"ok": False, "error": "unauthorized", "message": "bad signature"}
        return None

    # -- endpoints ------------------------------------------------------------------
    def _sale(self, sale_id: Any) -> dict[str, Any] | None:
        return next((s for s in self.sales if s["sale_id"] == sale_id), None)

    def _cand(self, phone: str) -> dict[str, Any] | None:
        t = tail(phone)
        return next((c for c in self.candidates if t and tail(c["phone"]) == t), None)

    def sales_open(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        words = str(body.get("search") or "").lower().split()
        rows = [s for s in self.sales if s["status"] == 1
                and all(w in f"{s['job_title']} {s['postcode_district']} {s['town']} {s['office_name']}".lower()
                       for w in words)]
        keep = ("sale_id", "job_title", "job_category", "postcode", "postcode_district", "office_name",
                "unit_name", "position_type", "salary", "cvs_sent", "cv_limit", "created_at")
        return 200, {"sales": [{k: s[k] for k in keep} for s in rows[: int(body.get("limit") or 50)]]}

    def queue(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        sale = self._sale(body.get("sale_id"))
        if not sale or sale["status"] != 1 or sale["cvs_sent"] >= sale["cv_limit"]:
            return 409, {"ok": False, "error": "sale_not_callable",
                         "message": "the vacancy is closed, on hold or has reached its CV limit"}
        skipped = dict.fromkeys(("blocked", "not_interested", "no_job", "closed", "already_sent",
                                 "already_interested", "locked", "recently_called", "no_phone"), 0)
        called = {e.get("applicant_id") for e in self.events.values() if e.get("sale_id") == sale["sale_id"]}
        out = []
        for c in sorted(self.candidates, key=lambda c: c["distance_km"]):
            reason = ("blocked" if c["blocked"] else "not_interested" if c["temp_not_interested"]
                      else "no_job" if c["no_job"] else "locked" if self._locked(c["phone"])
                      else "recently_called" if c["applicant_id"] in called
                      else "no_phone" if not e164(c["phone"]) else "")
            if reason:
                skipped[reason] += 1
                continue
            if c["distance_km"] >= float(body.get("radius_km") or 15):
                continue
            out.append({"applicant_id": c["applicant_id"], "name": c["name"],
                        "first_name": c["name"].split()[0], "phone_e164": e164(c["phone"]),
                        "phone_kind": "mobile", "postcode_district": c["postcode_district"],
                        "distance_km": c["distance_km"], "distance_miles": round(c["distance_km"] / 1.609, 1),
                        "job_title": c["job_title"],
                        "flags": {"callback": c["callback"], "no_response": False, "circuit_busy": False},
                        "last_note": "", "last_note_at": None, "last_called_at": None})
        brief = {k: sale[k] for k in ("sale_id", "job_title", "job_category", "position_type", "job_type",
                                      "timing", "salary", "experience", "qualification", "benefits",
                                      "description", "postcode_district", "cvs_sent", "cv_limit")}
        brief.update(client_name=None, site_name=None)
        return 200, {"sale": brief, "candidates": out[: int(body.get("limit") or 50)], "skipped": skipped}

    def lookup(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        c = self._cand(str(body.get("phone") or ""))
        if not c:
            return 404, {"ok": True, "found": False}
        return 200, {"found": True, "contact": {
            "applicant_id": c["applicant_id"], "name": c["name"], "first_name": c["name"].split()[0],
            "company": "", "phone_kind": "mobile", "postcode_district": c["postcode_district"],
            "job_title": c["job_title"], "do_not_call": c["blocked"],
            "flags": {"blocked": c["blocked"], "temp_not_interested": c["temp_not_interested"],
                      "callback": c["callback"], "no_response": False, "no_job": c["no_job"],
                      "circuit_busy": False},
            "notes": "", "last_outcome": "callback" if c["callback"] else "", "callback_note": "",
            "history": [], "applications": [], "interviews": [],
            "calls": [{"at": "", "direction": "outbound", "duration_s": int(e.get("duration_s") or 0),
                       "by": "Xplogent AI", "sale_id": e.get("sale_id")}
                      for e in self.events.values() if e.get("applicant_id") == c["applicant_id"]][-5:]}}

    def call_summary(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if body.get("test"):
            return 200, {"ok": True, "test": True}
        call_id = int(body.get("call_id") or 0)
        if call_id in self.events:
            return 200, {"ok": True, "duplicate": True}
        self.events[call_id] = body
        c = next((c for c in self.candidates if c["applicant_id"] == body.get("applicant_id")), None) \
            or self._cand(str(body.get("to") if body.get("direction") == "outbound" else body.get("from")))
        outcome = body.get("outcome")
        if c:
            if outcome == "not_interested":
                c["temp_not_interested"] = True
            elif outcome == "do_not_call":
                c["blocked"] = True
            elif outcome == "no_job":
                c["no_job"] = True
            elif outcome in ("callback", "recruiter_callback"):
                c["callback"] = True
            elif outcome == "interested":
                c.update(callback=False, temp_not_interested=False)
        return 200, {"ok": True, "call_log_id": 1000 + len(self.events)}

    def send_job_details(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        c = next((c for c in self.candidates if c["applicant_id"] == body.get("applicant_id")), None)
        if not c or not self._sale(body.get("sale_id")):
            return 404, {"ok": False, "error": "not_found", "message": "no such applicant or vacancy"}
        if c["blocked"]:
            return 409, {"ok": False, "error": "blocked", "message": "the applicant is blocked"}
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", str(body.get("email") or ""), re.I):
            return 422, {"ok": False, "error": "invalid_email", "message": "that email address is not valid"}
        if sum(1 for e in self.emails if e["applicant_id"] == c["applicant_id"]) >= 3:
            return 429, {"ok": False, "error": "rate_limited", "message": "3 emails a day at most"}
        self.emails.append({"applicant_id": c["applicant_id"], "sale_id": body.get("sale_id"),
                            "email": body["email"]})
        return 200, {"ok": True, "sent_email_id": len(self.emails)}

    def _locked(self, phone: str) -> dict[str, Any] | None:
        lock = self.locks.get(tail(phone))
        return lock if lock and lock["expires"] > time.time() else None

    def lock(self, body: dict[str, Any], by: str = "Xplogent AI") -> tuple[int, dict[str, Any]]:
        current = self._locked(str(body.get("phone") or ""))
        if current and current["by"] != by:
            return 409, {"locked": False, "locked_by": current["by"], "expires_at": current["expires"]}
        expires = time.time() + int(body.get("ttl_seconds") or 900)
        self.locks[tail(str(body.get("phone") or ""))] = {"by": by, "expires": expires}
        return 200, {"locked": True, "expires_at": expires}

    def unlock(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        key = tail(str(body.get("phone") or ""))
        if (self.locks.get(key) or {}).get("by") == "Xplogent AI":
            self.locks.pop(key, None)
        return 200, {"ok": True}


def create_app(token: str, crm: MockCRM | None = None) -> FastAPI:
    crm = crm or MockCRM(token)
    app = FastAPI(title="Mock CRM (Xplogent API v2)")
    app.state.crm = crm
    routes = {"sales/open": crm.sales_open, "campaigns/queue": crm.queue, "contact-lookup": crm.lookup,
              "call-summary": crm.call_summary, "send-job-details": crm.send_job_details,
              "dial-lock": crm.lock, "dial-unlock": crm.unlock}

    @app.post("/api/xplogent/{path:path}")
    async def endpoint(path: str, request: Request):
        body = await request.body()
        refused = crm.check({k.lower(): v for k, v in request.headers.items()}, body)
        if refused is None and path not in routes:
            refused = 404, {"ok": False, "error": "not_found", "message": "no such endpoint"}
        if refused is None:
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                data = None
            refused = (422, {"ok": False, "error": "bad_json", "message": "body is not JSON"}) \
                if not isinstance(data, dict) else None
        status, out = refused or routes[path](data)
        crm.requests.append({"path": path, "status": status})
        return JSONResponse({"api_version": 2, **out}, status_code=status)

    @app.get("/_mock/state")
    async def state():
        return {"requests": crm.requests[-50:], "events": list(crm.events.values())[-20:],
                "locks": crm.locks, "emails": crm.emails,
                "candidates": [{k: c[k] for k in ("applicant_id", "name", "blocked", "temp_not_interested",
                                                  "no_job", "callback")} for c in crm.candidates]}

    return app
