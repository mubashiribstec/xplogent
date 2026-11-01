"""WP2 + WP5: campaigns, the dialler, retries, dial locks, TPS and the consent gate."""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip import alerts, compliance, crm
from xplogent.voip.dialler import Dialler, enable_problem, unanswered_outcome
from xplogent.voip.mock_crm import MockCRM, create_app

TOKEN = "t0ken-for-tests-0123456789abcdef"


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.setenv("TEST_CRM_TOKEN", TOKEN)
    save_user_config({"voip": {"enabled": True, "calling_hours": {"enabled": False},
                               "crm": {"enabled": True, "base_url": "https://crm.example.com",
                                       "api_token_env": "TEST_CRM_TOKEN"}}})
    state = MockCRM(TOKEN)
    crm.set_transport(httpx.ASGITransport(app=create_app(TOKEN, state)))
    crm._LOOKUP_CACHE.clear()
    alerts.reset()
    yield state
    crm.set_transport(None)


class FakeService:
    """Just enough of VoipService: dial() records, active is empty."""

    def __init__(self, result=None):
        self.active, self.dialled, self.result = {}, [], result or {"ok": True, "state": "ringing"}

    async def dial(self, number, cfg=None, **kw):
        self.dialled.append((number, kw))
        return dict(self.result)


def campaign(**kw):
    fields = {"name": "Care LS6", "sale_id": 45, "line": "extension", "enabled": 1, "max_concurrent": 1,
              "calls_per_min": 4, "consent_basis": "applied", "retry_policy": {"max_attempts": 3,
                                                                                "gaps_hours": [2, 22, 24]}, **kw}
    with Store(load_config().db_path) as s:
        return s.create_campaign(fields)


def items(cid):
    with Store(load_config().db_path) as s:
        return {it["applicant_id"]: it for it in s.list_items(cid)}


async def test_refill_then_dial_within_the_limits(mock):
    cid = campaign()
    svc = FakeService()
    d = Dialler(svc)
    await d.tick()
    its = items(cid)
    assert set(its) == {123, 124}                         # Priya is blocked: the CRM skipped her
    assert len(svc.dialled) == 1                          # max_concurrent 1
    number, kw = svc.dialled[0]
    assert number == "+447700900123" and kw["via"] == "extension"
    brief = kw["brief"]
    assert brief["campaign_id"] == cid and brief["attempt"] == 1 and not brief["final_attempt"]
    assert brief["sale"]["salary"] == "£12.60 per hour" and brief["candidate"]["first_name"] == "Sarah"
    assert its[123]["state"] == "dialling"
    assert mock.locks                                       # the CRM dial lock is held
    await d.tick()
    assert len(svc.dialled) == 1                          # one call in flight: wait
    await d.refill(load_config(), {"id": cid, "sale_id": 45, "radius_km": 15})
    assert len(items(cid)) == 2                           # no duplicates
    asks = [r for r in mock.requests if r["path"] == "campaigns/queue"]
    await d.tick()
    assert [r for r in mock.requests if r["path"] == "campaigns/queue"] == asks   # not every tick


async def test_pace_per_minute(mock):
    campaign(max_concurrent=5, calls_per_min=1)
    svc = FakeService()
    d = Dialler(svc)
    await d.tick()
    await d.tick()
    assert len(svc.dialled) == 1


async def test_stop_all_and_agent_off(mock):
    campaign()
    svc = FakeService()
    save_user_config({"voip": {"campaigns": {"stop_all": True}}})
    assert (await Dialler(svc).tick())["stopped"] == "Stop all is on"
    save_user_config({"voip": {"campaigns": {"stop_all": False}, "enabled": False}})
    assert "off" in (await Dialler(svc).tick())["stopped"]
    assert svc.dialled == []


async def test_fresh_crm_flags_and_locks_are_respected(mock):
    cid = campaign(max_concurrent=5)
    d = Dialler(FakeService())
    await d.refill(load_config(), {"id": cid, "sale_id": 45, "radius_km": 15})
    mock.candidates[0]["temp_not_interested"] = True        # Sarah said no to a recruiter meanwhile
    mock.lock({"phone": "07700900124"}, by="Jane Recruiter")  # Tom is on the phone with Jane
    await d.tick()
    its = items(cid)
    assert its[123]["state"] == "skipped" and its[123]["skip_reason"] == "not interested"
    assert its[124]["state"] == "retry_wait" and "Jane Recruiter" in its[124]["skip_reason"]
    assert its[124]["attempts"] == 0 and d.service.dialled == []


async def test_local_dnc_skips(mock):
    cid = campaign()
    with Store(load_config().db_path) as s:
        s.add_dnc("+447700900123", "asked")
    d = Dialler(FakeService())
    await d.tick()
    assert items(cid)[123]["skip_reason"] == "do-not-call list"


async def test_tps_registered_skips_and_errors_fail_closed(mock, monkeypatch):
    cid = campaign(consent_basis="cold", cold_signoff={"by": "Admin", "at": 1})

    async def registered(cfg, store, numbers, **kw):
        return {"ok": True, "results": {n: {"tps": n.endswith("123"), "ctps": False} for n in numbers}, "error": ""}
    monkeypatch.setattr(compliance, "screen", registered)
    d = Dialler(FakeService())
    await d.tick()
    assert items(cid)[123]["skip_reason"] == "registered with TPS"

    async def broken(cfg, store, numbers, **kw):
        return {"ok": False, "results": {}, "error": "TPS check failed: timeout"}
    monkeypatch.setattr(compliance, "screen", broken)
    await d.tick()
    assert d.service.dialled == []                           # never dialled without a check
    assert any(a["kind"] == "tps_unreachable" for a in alerts.recent())


async def test_outcomes_move_items_on(mock):
    cid = campaign(max_concurrent=5)
    d = Dialler(FakeService())
    await d.tick()
    cfg = load_config()
    its = items(cid)
    sarah, tom = its[123]["id"], its[124]["id"]
    before = time.time()
    await d.advance(cfg, sarah, "no_answer", 1)
    it = items(cid)[123]
    assert it["state"] == "retry_wait" and 2 * 3600 - 60 < it["next_at"] - before < 2 * 3600 + 60
    await d.advance(cfg, tom, "interested", 2)
    assert items(cid)[124]["state"] == "done" and items(cid)[124]["last_outcome"] == "interested"
    with Store(cfg.db_path) as s:
        s.update_item(sarah, {"attempts": 3})
    await d.advance(cfg, sarah, "voicemail", 3)
    assert items(cid)[123]["state"] == "done"                # the third try was the last
    assert mock.locks == {} or all(v["by"] != "Xplogent AI" for v in mock.locks.values())   # released


async def test_last_attempt_is_marked_final(mock):
    cid = campaign()
    with Store(load_config().db_path) as s:
        s.add_campaign_items(cid, [{"applicant_id": 123, "phone": "+447700900123", "brief": {"applicant_id": 123}}])
        it = s.list_items(cid)[0]
        s.update_item(it["id"], {"attempts": 2})
    svc = FakeService()
    await Dialler(svc).tick()
    assert svc.dialled[0][1]["brief"]["attempt"] == 3 and svc.dialled[0][1]["brief"]["final_attempt"] is True


async def test_busy_line_is_logged_and_sent_to_the_crm(mock):
    cid = campaign()
    svc = FakeService({"ok": False, "error": "call to +447700900123: 486 Busy Here"})
    d = Dialler(svc)
    await d.tick()
    it = items(cid)[123]
    assert it["state"] == "retry_wait" and it["last_outcome"] == "busy"
    with Store(load_config().db_path) as s:
        call = s.get_call(it["last_call_id"])
    assert call["outcome"] == "busy" and call["brief"]["campaign_item_id"] == it["id"]
    await __import__("asyncio").sleep(0.2)                    # the summary goes out in the background
    assert [e["outcome"] for e in mock.events.values()] == ["busy"]
    assert unanswered_outcome("404 Not Found") == "number_unobtainable"
    assert unanswered_outcome("Telnyx: timeout") == "no_answer"


async def test_a_line_reporting_no_answer_later(mock):
    cid = campaign()
    svc = FakeService()
    d = Dialler(svc)
    await d.tick()
    meta = {"brief": svc.dialled[0][1]["brief"]}
    d.not_answered(meta, "call to +447700900123: 480 Temporarily Unavailable")
    await __import__("asyncio").sleep(0.2)
    assert items(cid)[123]["state"] == "retry_wait" and items(cid)[123]["last_outcome"] == "no_answer"


async def test_three_calls_this_week_means_no_fourth(mock):
    cid = campaign()
    with Store(load_config().db_path) as s:
        s.start_call("inbound", "+447700900999", "2001")             # an ordinary call: no brief at all
        assert s.campaign_spend_since(cid, 0) == 0 and s.campaign_calls_since(cid, 0) == 0
        for _ in range(3):
            c = s.start_call("outbound", "", "+447700900123")
            s.set_call_brief(c, {"applicant_id": 123, "campaign_id": 99})
    res = await Dialler(FakeService()).refill(load_config(), {"id": cid, "sale_id": 45, "radius_km": 15})
    assert set(items(cid)) == {124} and res["skipped"]["called_3_times_this_week"] == 1


async def test_campaign_cap_pauses(mock):
    cid = campaign(daily_cost_cap=1.0)
    with Store(load_config().db_path) as s:
        c = s.start_call("outbound", "", "+447700900123")
        s.set_call_brief(c, {"campaign_id": cid})
        s.end_call(c, cost=1.2)
    svc = FakeService()
    await Dialler(svc).tick()
    with Store(load_config().db_path) as s:
        camp = s.get_campaign(cid)
    assert not camp["enabled"] and "cap" in camp["paused_reason"] and svc.dialled == []


def test_consent_gate(mock, monkeypatch):
    cfg = load_config()
    base = {"sale_id": 45, "line": "extension", "consent_basis": "applied"}
    assert enable_problem(cfg, base) == ""
    assert "sign-off" in enable_problem(cfg, {**base, "consent_basis": "cold"})
    signed = {**base, "consent_basis": "cold", "cold_signoff": {"by": "Admin"}}
    assert "TPS" in enable_problem(cfg, signed)
    save_user_config({"voip": {"tps": {"url": "https://tps.example.com/check"}}})
    monkeypatch.setenv(compliance.KEY_ENV, "k")
    assert enable_problem(load_config(), signed) == ""


async def test_tps_screen_parses_and_caches(monkeypatch):
    save_user_config({"voip": {"tps": {"url": "https://tps.example.com/check"}}})
    monkeypatch.setenv(compliance.KEY_ENV, "k")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": {"+447700900123": {"tps": True, "ctps": False},
                                                     "+447700900124": {"tps": False, "ctps": True}}})
    cfg = load_config()
    with Store(cfg.db_path) as s:
        r = await compliance.screen(cfg, s, ["+447700900123", "+447700900124"], transport=httpx.MockTransport(handler))
        assert r["ok"] and compliance.registered(r["results"]["+447700900123"]) == "tps"
        assert compliance.registered(r["results"]["+447700900124"]) == "ctps"
        again = await compliance.screen(cfg, s, ["+447700900123"], transport=httpx.MockTransport(handler))
    assert again["results"]["+447700900123"]["cached"] and len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer k"


def test_campaign_callback_is_not_booked_twice(mock):
    from tests.conftest import ScriptedProvider
    from tests.test_voip import FakeSTT, FakeTTS
    from xplogent.voip.service import build_session, finalize_call, get_service

    cid = campaign()
    with Store(load_config().db_path) as s:
        s.add_campaign_items(cid, [{"applicant_id": 123, "phone": "+447700900123", "brief": {"applicant_id": 123}}])
        item = s.list_items(cid)[0]
    cfg = load_config()
    store = Store(cfg.db_path)

    async def send(_b):
        return None
    info = {"direction": "outgoing", "from": "2001", "to": "+447700900123", "transport": "sip", "line": "extension",
            "brief": {"applicant_id": 123, "campaign_id": cid, "campaign_item_id": item["id"], "attempt": 1}}
    session, call_id, brain = build_session(cfg, store, info, send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                                            provider=ScriptedProvider([]), retrieve=None)
    assert store.get_item(item["id"])["state"] == "in_call"
    brain.state.callback_at, brain.state.callback_who = "2026-10-29T17:30:00+00:00", "ai"
    brain.state.callback, brain.state.outcome = "Thursday 29 October at 5:30 pm", "callback"
    calls = []
    real = get_service().dialler.call_finished
    get_service().dialler.call_finished = lambda *a, **k: calls.append(a)
    try:
        finalize_call(cfg, store, call_id, session, brain, "+447700900123")
    finally:
        get_service().dialler.call_finished = real
    assert store.list_callbacks() == []                         # the campaign item redials instead
    assert calls[0][2] == "callback" and calls[0][4] == "2026-10-29T17:30:00+00:00"
    store.close()


def test_campaign_api(mock):
    from xplogent.interfaces.api.server import create_app as app

    with TestClient(app()) as c:
        made = c.post("/voip/campaigns", json={"sale_id": 45, "sale": {"job_title": "Care Assistant"},
                                               "max_concurrent": 99, "consent_basis": "applied"}).json()
        assert made["ok"] and made["campaign"]["max_concurrent"] == 20 and made["campaign"]["name"].startswith("Care")
        cid = made["campaign"]["id"]
        assert c.post(f"/voip/campaigns/{cid}/enable", json={"on": True}).json()["ok"]
        assert c.patch(f"/voip/campaigns/{cid}", json={"consent_basis": "cold"}).json()["campaign"]["enabled"] is False
        bad = c.post(f"/voip/campaigns/{cid}/enable", json={"on": True}).json()
        assert not bad["ok"] and "sign-off" in bad["error"]
        assert not c.post(f"/voip/campaigns/{cid}/cold-signoff", json={"by": "Ann"}).json()["ok"]   # no tick
        assert c.post(f"/voip/campaigns/{cid}/cold-signoff", json={"by": "Ann", "confirm": True}).json()["ok"]
        refill = c.post(f"/voip/campaigns/{cid}/refill").json()
        assert refill["ok"] and refill["added"] == 2
        detail = c.get(f"/voip/campaigns/{cid}").json()
        assert len(detail["items"]) == 2 and detail["campaign"]["counts"]["queued"] == 2
        assert c.post("/voip/campaigns/stop-all", json={"stop": True}).json()["stop_all"]
        assert c.get("/voip/campaigns").json()["stop_all"] is True
