"""Test calls skip the calling hours and the stats, so they're fenced in (deep audit P2):
only your own test numbers, a daily budget, DNC still applies, and the cost cap counts them."""

from __future__ import annotations

import pytest

from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip import alerts
from xplogent.voip.service import VoipService, is_test_number


@pytest.fixture
def svc():
    alerts.reset()
    save_user_config({"voip": {"enabled": False, "calling_hours": {"enabled": False},
                               "test_numbers": ["+447700900555"], "test_calls_per_day": 3,
                               "sip_extension": {"test_callers": ["201"]}}})
    s = VoipService()
    s.dialled = []

    async def dialer(number, meta):
        s.dialled.append((number, meta.get("test")))
        return {"ok": True}
    s.sip_dialer = dialer
    s.sip_extension_dialer = dialer
    return s


async def test_a_test_call_only_rings_your_own_numbers(svc):
    res = await svc.dial("+447700900999", via="sip", test=True)      # someone else's phone
    assert not res["ok"] and res["test_policy"] and "test numbers" in res["error"]
    assert (await svc.dial("+44 7700 900555", via="sip", test=True))["ok"]   # spacing doesn't matter
    assert (await svc.dial("201", via="extension", test=True))["ok"]       # a PBX test caller
    assert (await svc.dial("2001", via="extension", test=True))["ok"]      # a short internal extension
    assert [n for n, _ in svc.dialled] == ["+447700900555", "201", "2001"]


def test_short_extensions_are_internal_only_on_sip_lines():
    cfg = load_config()
    assert is_test_number(cfg, "2001", "extension") and is_test_number(cfg, "*43", "sip")
    assert not is_test_number(cfg, "2001", "telnyx")                 # Telnyx always reaches the PSTN


async def test_the_daily_test_budget_runs_out(svc):
    cfg = load_config()
    with Store(cfg.db_path) as s:
        for _ in range(3):
            s.end_call(s.start_call("outbound", "", "+447700900555", test=True, transport="sip"), outcome="other")
        s.start_call("test", "", "", test=True, transport="browser")   # browser tests ring nobody
    res = await svc.dial("+447700900555", via="sip", test=True)
    assert not res["ok"] and "3 test calls" in res["error"]
    assert not svc.dialled


async def test_dnc_still_applies_to_test_calls(svc):
    with Store(load_config().db_path) as s:
        s.add_dnc("+447700900555", reason="asked")
    res = await svc.dial("+447700900555", via="sip", test=True)
    assert not res["ok"] and "do-not-call" in res["error"]


async def test_the_cost_cap_stops_test_calls_too(svc):
    save_user_config({"voip": {"daily_cost_cap": 1.0}})
    with Store(load_config().db_path) as s:
        s.end_call(s.start_call("outbound", "", "+447700900555", test=True, transport="sip"),
                   outcome="other", cost=1.5)
    res = await svc.dial("+447700900555", via="sip", test=True)
    assert not res["ok"] and res["cost_cap"]


async def test_never_emergency_numbers(svc):
    save_user_config({"voip": {"enabled": True, "test_numbers": ["999"]}})
    for n in ("999", "112", "911"):
        res = await svc.dial(n, via="extension", test=True)
        assert not res["ok"] and "emergency" in res["error"]
    assert not svc.dialled


def test_test_numbers_route_is_validated_and_audited():
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    c = TestClient(create_app())
    assert c.get("/voip/test-numbers").json()["numbers"] == []
    assert c.post("/voip/test-numbers", json={"add": "+44 7700 900111"}).json()["numbers"] == ["+447700900111"]
    assert c.post("/voip/test-numbers", json={"add": "201"}).json()["numbers"] == ["+447700900111", "201"]
    assert not c.post("/voip/test-numbers", json={"add": "call me"}).json()["ok"]
    assert c.post("/voip/test-numbers", json={"remove": "201"}).json()["numbers"] == ["+447700900111"]
    with Store(load_config().db_path) as s:
        rows = s.audit_rows(action="voip_test_numbers")
    assert len(rows) == 3 and rows[0]["risk"] == "high"


def test_dialling_outside_hours_is_audited(monkeypatch):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app
    from xplogent.voip.service import get_service

    save_user_config({"voip": {"enabled": True, "calling_hours": {"enabled": False}}})

    async def dialer(number, meta):
        return {"ok": True}
    monkeypatch.setattr(get_service(), "sip_extension_dialer", dialer)
    c = TestClient(create_app())
    assert c.post("/voip/dial", json={"number": "201", "via": "extension", "ignore_hours": True}).json()["ok"]
    with Store(load_config().db_path) as s:
        assert s.audit_rows(action="voip_dial_hours_override")[0]["risk"] == "high"
