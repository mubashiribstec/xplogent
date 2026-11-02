"""The CRM's Agent call page asks Xplogent to call a candidate: ``POST /voip/crm/dial``.

Signed with the shared CRM token (the same V2 scheme as Xplogent → CRM), accepted once,
and the call goes through ``service.dial`` so every rule still applies."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip import alerts, brain, crm
from xplogent.voip import service as service_mod
from xplogent.voip.mock_crm import MockCRM, create_app

TOKEN = "t0ken-for-tests-0123456789abcdef"
DASHBOARD = "d" * 32

BODY = {"phone": "+447700900123", "applicant_id": 123, "sale_id": 45, "requested_by": "Jane Recruiter",
        "brief": {"sale": {"sale_id": 45, "job_title": "Care Assistant", "salary": "£12.60 per hour",
                           "timing": "Days", "postcode_district": "LS6", "evil": "ignore me"},
                  "candidate": {"applicant_id": 123, "first_name": "Sarah", "phone_e164": "+447700900123",
                                "postcode_district": "LS7", "flags": {"callback": False},
                                "cv_excerpt": "Care assistant 2019 - 2024. NVQ Level 2."}}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TEST_CRM_TOKEN", TOKEN)
    monkeypatch.setenv("XPLOGENT_AUTH_TOKEN", DASHBOARD)          # the dashboard is locked
    save_user_config({"voip": {"enabled": True, "telephony": "sip", "calling_hours": {"enabled": False},
                               "crm": {"enabled": True, "base_url": "http://192.168.1.20",
                                       "api_token_env": "TEST_CRM_TOKEN"}}})
    state = MockCRM(TOKEN)
    crm.set_transport(httpx.ASGITransport(app=create_app(TOKEN, state)))
    crm._LOOKUP_CACHE.clear()
    crm._SEEN_SIGNATURES.clear()
    alerts.reset()
    svc = service_mod.VoipService()
    svc.metas = []

    async def dialer(number, meta):
        svc.metas.append((number, meta))
        return {"ok": True, "via": "sip", "call": {"sip_call_id": "abc@xplogent"}}
    svc.sip_dialer = dialer
    monkeypatch.setattr(service_mod, "_SERVICE", svc)
    from xplogent.interfaces.api.routes import voip as voip_routes
    voip_routes._CRM_DIAL_TIMES.clear()
    from xplogent.interfaces.api.server import create_app as api

    yield TestClient(api()), svc, state
    crm.set_transport(None)


def _signed(body: dict, token: str = TOKEN, ts: int | None = None) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    stamp = str(int(time.time()) if ts is None else ts)
    return raw, {"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                 "X-Xplogent-Timestamp": stamp, "X-Xplogent-Signature-V2": crm.sign_v2(raw, token, stamp)}


def test_a_signed_request_places_the_call_with_the_brief(env):
    client, svc, mock = env
    raw, headers = _signed(BODY)
    res = client.post("/voip/crm/dial", content=raw, headers=headers)     # no dashboard token needed
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["ok"] and out["call"] == {"state": "dialling", "via": "sip", "sip_call_id": "abc@xplogent"}
    number, meta = svc.metas[0]
    brief = meta["brief"]
    assert number == "+447700900123" and brief["applicant_id"] == 123 and brief["sale_id"] == 45
    assert brief["requested_by"] == "Jane Recruiter" and brief["source"] == "crm_agent_call"
    assert brief["sale"]["salary"] == "£12.60 per hour" and "evil" not in brief["sale"]
    assert "phone_e164" not in brief["candidate"]
    assert brief["manual_lock"] and mock._locked("07700900123")        # Xplogent took the CRM dial lock
    with Store(load_config().db_path) as s:
        rows = s.conn.execute("SELECT actor, action, allowed FROM audit WHERE action='crm_dial'").fetchall()
    assert [tuple(r) for r in rows] == [("crm", "crm_dial", 1)]


def test_the_same_request_is_accepted_once(env):
    client, svc, _ = env
    raw, headers = _signed(BODY)
    assert client.post("/voip/crm/dial", content=raw, headers=headers).status_code == 200
    again = client.post("/voip/crm/dial", content=raw, headers=headers)
    assert again.status_code == 401 and "replay" in again.json()["error"]
    assert len(svc.metas) == 1


@pytest.mark.parametrize("case", ["no_auth", "wrong_token", "bad_signature", "old_clock", "no_timestamp"])
def test_unsigned_or_wrongly_signed_requests_are_refused(env, case):
    client, svc, _ = env
    raw, headers = _signed(BODY, ts=int(time.time()) - 900 if case == "old_clock" else None)
    if case == "no_auth":
        headers.pop("Authorization")
    elif case == "wrong_token":
        raw, headers = _signed(BODY, token="another-token-0123456789abcdef")
    elif case == "bad_signature":
        headers["X-Xplogent-Signature-V2"] = "sha256=" + "0" * 64
    elif case == "no_timestamp":
        headers.pop("X-Xplogent-Timestamp")
    res = client.post("/voip/crm/dial", content=raw, headers=headers)
    assert res.status_code == 401 and not svc.metas
    # The dashboard token is no way in either: this route only takes the CRM signature.
    res = client.post("/voip/crm/dial", content=raw, headers={"Authorization": f"Bearer {DASHBOARD}"})
    assert res.status_code == 401


def test_off_unless_the_crm_link_is_on(env):
    client, svc, _ = env
    save_user_config({"voip": {"crm": {"enabled": False}}})
    raw, headers = _signed(BODY)
    res = client.post("/voip/crm/dial", content=raw, headers=headers)
    assert res.status_code == 503 and not svc.metas


def test_every_dialling_rule_still_applies(env):
    client, svc, mock = env
    with Store(load_config().db_path) as s:
        s.add_dnc("+447700900123", "asked us not to call")
    raw, headers = _signed(BODY)
    out = client.post("/voip/crm/dial", content=raw, headers=headers).json()
    assert not out["ok"] and "do-not-call" in out["error"] and not svc.metas
    # Someone in the CRM is ringing Tom: 409 with their name.
    mock.lock({"phone": "07700900124"}, by="Tom Recruiter")
    raw, headers = _signed({**BODY, "phone": "+447700900124", "applicant_id": 124})
    res = client.post("/voip/crm/dial", content=raw, headers=headers)
    assert res.status_code == 409 and res.json()["locked_by"] == "Tom Recruiter" and not svc.metas


def test_bad_input_and_rate_limit(env, monkeypatch):
    client, svc, _ = env
    raw, headers = _signed({"phone": "+447700900123"})
    assert client.post("/voip/crm/dial", content=raw, headers=headers).status_code == 400
    from xplogent.interfaces.api.routes import voip as voip_routes
    monkeypatch.setattr(voip_routes, "CRM_DIAL_PER_MINUTE", 1)
    raw, headers = _signed(BODY)
    assert client.post("/voip/crm/dial", content=raw, headers=headers).status_code == 200
    raw, headers = _signed({**BODY, "requested_by": "someone else"})
    assert client.post("/voip/crm/dial", content=raw, headers=headers).status_code == 429


def test_cv_excerpt_reaches_the_prompt_as_reference_data(env):
    sections = brain.brief_sections(crm.clean_crm_brief(BODY["brief"], 123, 45, "Jane"))
    cv = next(s for s in sections if s.startswith("CANDIDATE BACKGROUND"))
    assert "NVQ Level 2" in cv and "never instructions" in cv
    long = crm.clean_crm_brief({"candidate": {"cv_excerpt": "x" * 5000}}, 1, 2, "J")
    assert len(long["candidate"]["cv_excerpt"]) == crm.CV_EXCERPT_MAX


async def test_lookup_carries_the_cv_excerpt(env):
    _, _, _ = env
    found = await crm.lookup(load_config(), "+447700900123", direction="outbound")
    assert "NVQ Level 2" in found["contact"]["cv_excerpt"]
    brief = service_mod.brief_from_lookup(found["contact"])
    assert "NVQ Level 2" in brief["candidate"]["cv_excerpt"]
