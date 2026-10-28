"""WP9: calls cut off by a restart are recovered; alerts fire, rate-limited."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip import alerts


@pytest.fixture(autouse=True)
def _fresh_alerts():
    alerts.reset()
    yield
    alerts.reset()


def _active_call(cfg, test=False):
    with Store(cfg.db_path) as s:
        cid = s.start_call("outbound", "+441130000000", "+447700900123", test=test)
        s.add_call_turn(cid, "agent", "Hi, is that Sarah?")
        s.add_call_turn(cid, "caller", "Yes, speaking")
    return cid


def test_restart_marks_active_calls_interrupted_and_tells_the_crm(monkeypatch):
    from xplogent.voip import service

    monkeypatch.setenv("TEST_CRM_TOKEN", "s3cret")
    save_user_config({"voip": {"crm": {"enabled": True, "base_url": "https://crm.example.com",
                                       "api_token_env": "TEST_CRM_TOKEN"}}})
    cfg = load_config()
    cut, live, test_call = _active_call(cfg), _active_call(cfg), _active_call(cfg, test=True)
    sent = []
    monkeypatch.setattr(service, "enqueue_delivery", lambda c, payload, on_status=None: sent.append(payload))
    ids = service.recover_interrupted_calls(cfg, {live})
    assert ids == [cut, test_call]
    with Store(cfg.db_path) as s:
        row = s.get_call(cut)
        assert row["status"] == "ended" and row["outcome"] == "interrupted"
        assert "cut off" in row["summary"] and row["ended_at"]
        assert s.get_call(live)["status"] == "active"            # still running: untouched
    assert len(sent) == 1                                          # test calls never reach the CRM
    assert sent[0]["call_id"] == cut and sent[0]["outcome"] == "interrupted"
    assert [t["text"] for t in sent[0]["transcript"]] == ["Hi, is that Sarah?", "Yes, speaking"]
    assert alerts.recent()[0]["kind"] == "calls_interrupted"
    assert service.recover_interrupted_calls(cfg, {live}) == []    # only once


def test_app_start_recovers(monkeypatch):
    from xplogent.interfaces.api.server import create_app

    cid = _active_call(load_config())
    with TestClient(create_app()) as client:
        assert client.get(f"/voip/calls/{cid}").json()["call"]["outcome"] == "interrupted"
        r = client.get("/voip/readiness").json()
        item = next(i for i in r["items"] if i["id"] == "alerts")
        assert not item["ok"] and not item["required"] and "cut off" in item["detail"]


def test_five_failures_in_a_row_alert_once(monkeypatch):
    posted = []
    monkeypatch.setenv(alerts.WEBHOOK_ENV, "https://hooks.example.com/T/abc")
    transport = httpx.MockTransport(lambda r: posted.append(json.loads(r.content)) or httpx.Response(200))
    real_post = alerts._post

    async def post(url, body, _transport=None):
        return await real_post(url, body, transport)
    monkeypatch.setattr(alerts, "_post", post)
    for _ in range(4):
        alerts.dial_result({"ok": False, "error": "call to 0113: 503 Service Unavailable"})
    assert alerts.recent() == []
    alerts.dial_result({"ok": False, "error": "call to 0113: 503 Service Unavailable"})
    assert [a["kind"] for a in alerts.recent()] == ["call_failures"]
    for _ in range(10):                                            # rate-limited
        alerts.dial_result({"ok": False, "error": "Telnyx refused the call"})
    assert len(alerts.recent()) == 1
    assert posted and posted[0]["kind"] == "call_failures" and "5 calls in a row" in posted[0]["text"]


def test_busy_and_no_answer_are_not_failures_and_success_resets():
    for _ in range(4):
        alerts.dial_result({"ok": False, "error": "boom"})
    alerts.dial_result({"ok": True})
    for _ in range(4):
        alerts.dial_result({"ok": False, "error": "boom"})
    for code in ("486 Busy Here", "480 Temporarily Unavailable", "603 Decline"):
        alerts.dial_result({"ok": False, "error": f"call to 07700: {code}"})
    assert alerts.recent() == []
    # a call where the agent failed on every turn counts as a failure
    alerts.session_result([{"error": "TimeoutError"}])
    assert [a["kind"] for a in alerts.recent()] == []               # 1 failure after the reset
    alerts.session_result([{"stt_ms": 100}, {"stt_ms": 90}])         # healthy call: resets


def test_crm_down_for_five_minutes():
    alerts.crm_result(False, "HTTP 502", now=1000.0)
    alerts.crm_result(False, "HTTP 502", now=1200.0)
    assert alerts.recent() == []
    alerts.crm_result(False, "HTTP 502", now=1301.0)
    assert alerts.recent()[0]["kind"] == "crm_unreachable"
    alerts.reset()
    alerts.crm_result(False, "HTTP 502", now=1000.0)
    alerts.crm_result(True, now=1100.0)
    alerts.crm_result(False, "HTTP 502", now=1400.0)
    assert len(alerts.recent()) == 1                                # only the first outage alerted


async def test_crm_delivery_feeds_the_crm_alert(monkeypatch):
    from xplogent.voip import crm

    monkeypatch.setenv("TEST_CRM_TOKEN", "s3cret")
    save_user_config({"voip": {"crm": {"enabled": True, "base_url": "https://crm.example.com",
                                       "api_token_env": "TEST_CRM_TOKEN"}}})
    seen = []
    monkeypatch.setattr(alerts, "crm_result", lambda ok, detail="", **k: seen.append(ok))
    down = httpx.MockTransport(lambda r: httpx.Response(502))
    await crm.deliver(load_config(), {"call_id": 1}, transport=down, attempts=1, backoff_s=0)
    up = httpx.MockTransport(lambda r: httpx.Response(200))
    await crm.deliver(load_config(), {"call_id": 1}, transport=up, attempts=1, backoff_s=0)
    assert seen == [False, True]


async def test_send_test_alert(monkeypatch):
    assert (await alerts.send_test())["ok"] is False                  # nothing saved
    monkeypatch.setenv(alerts.WEBHOOK_ENV, "https://hooks.example.com/T/abc")
    ok = httpx.MockTransport(lambda r: httpx.Response(200))
    assert (await alerts.send_test(transport=ok))["ok"] is True
    bad = httpx.MockTransport(lambda r: httpx.Response(404))
    res = await alerts.send_test(transport=bad)
    assert res["ok"] is False and "404" in res["error"]


def test_alert_endpoints_never_return_the_address(monkeypatch):
    from xplogent.interfaces.api.server import create_app

    monkeypatch.setenv(alerts.WEBHOOK_ENV, "https://hooks.slack.com/services/T0/B0/secretpart")
    with TestClient(create_app()) as client:
        r = client.get("/voip/alerts").json()
        assert r["webhook_set"] is True and "secretpart" not in json.dumps(r)

