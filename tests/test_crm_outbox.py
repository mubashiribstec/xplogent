"""Deep audit P2: call summaries survive CRM outages and restarts (a durable outbox), and
manual calls to CRM candidates take the dial lock like campaign calls do."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip import alerts, crm
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


def _ended_call(cfg, started_at: float | None = None) -> int:
    with Store(cfg.db_path) as s:
        cid = s.start_call("outbound", "", "+447700900123", transport="sip")
        s.end_call(cid, outcome="interested", summary="keen")
        if started_at is not None:
            s._write("UPDATE calls SET started_at=? WHERE id=?", (started_at, cid))
    return cid


def _queue(cfg, cid: int) -> None:
    with Store(cfg.db_path) as s:
        crm.queue_summary(cfg, s, cid, {"call_id": cid, "outcome": "interested", "api_version": 2})


def _row(cfg, cid: int) -> dict:
    with Store(cfg.db_path) as s:
        r = s.conn.execute("SELECT crm_status, crm_attempts, crm_next_at FROM calls WHERE id=?", (cid,)).fetchone()
    return {"status": r[0], "attempts": r[1], "next_at": r[2]}


def _down(request):
    return httpx.Response(503, json={"error": "maintenance"})


async def test_a_summary_is_stored_before_it_is_sent_and_the_outbox_delivers_it(mock):
    cfg = load_config()
    cid = _ended_call(cfg)
    _queue(cfg, cid)                                   # inside a loop: sent right away
    await asyncio.sleep(0.2)
    assert _row(cfg, cid)["status"] == "sent" and cid in mock.events
    assert await crm.outbox_tick(cfg) == 0             # nothing left to do: never sent twice
    assert len(mock.events) == 1


def test_without_a_loop_it_waits_in_the_outbox(mock):
    cfg = load_config()
    cid = _ended_call(cfg)
    _queue(cfg, cid)
    row = _row(cfg, cid)
    assert row["status"] == "pending" and row["next_at"] > time.time()          # leased
    assert asyncio.run(crm.outbox_tick(cfg, now=time.time() + crm.OUTBOX_LEASE_S + 1)) == 1
    assert _row(cfg, cid)["status"] == "sent" and mock.events[cid]["outcome"] == "interested"


async def test_failures_back_off_then_dead_letter(mock):
    cfg = load_config()
    cid = _ended_call(cfg)
    _queue(cfg, cid)
    down = httpx.MockTransport(_down)
    now = time.time() + crm.OUTBOX_LEASE_S + 1
    assert await crm.outbox_tick(cfg, now=now, transport=down) == 0
    row = _row(cfg, cid)
    assert row["status"] == "failed" and row["attempts"] == 1
    assert row["next_at"] == pytest.approx(now + 60)                          # 1 minute
    assert await crm.outbox_tick(cfg, now=now + 10, transport=down) == 0     # not due yet
    assert _row(cfg, cid)["attempts"] == 1
    await crm.outbox_tick(cfg, now=now + 61, transport=down)
    row = _row(cfg, cid)
    assert row["attempts"] == 2 and row["next_at"] == pytest.approx(now + 61 + 300)   # 5 minutes
    with Store(cfg.db_path) as s:
        s.crm_schedule(cid, attempts=crm.OUTBOX_MAX_TRIES - 1, next_at=0)
    await crm.outbox_tick(cfg, transport=down)
    assert _row(cfg, cid)["status"] == "dead"
    assert alerts.recent()[0]["kind"] == "crm_dead_letter"
    assert await crm.outbox_tick(cfg, now=time.time() + 10 ** 6) == 0          # dead: manual Resend only
    assert cid not in mock.events


async def test_a_send_cut_off_by_a_restart_is_picked_up(mock):
    cfg = load_config()
    cid = _ended_call(cfg)
    with Store(cfg.db_path) as s:
        s.set_crm_payload(cid, {"call_id": cid, "outcome": "interested"})
        s.crm_schedule(cid, status="sending", next_at=time.time() - 1)        # the old process died mid-send
    assert await crm.outbox_tick(cfg) == 1
    assert _row(cfg, cid)["status"] == "sent"


async def test_old_and_test_calls_are_not_resent_automatically(mock):
    cfg = load_config()
    old = _ended_call(cfg, started_at=time.time() - 8 * 86400)
    with Store(cfg.db_path) as s:
        s.set_crm_payload(old, {"call_id": old})
        s.crm_schedule(old, status="failed", next_at=0)
        t = s.start_call("outbound", "", "201", test=True)
        s.set_crm_payload(t, {"call_id": t, "test": True})
        s.crm_schedule(t, status="failed", next_at=0)
    assert await crm.outbox_tick(cfg) == 0 and not mock.events


def test_resend_restarts_the_retries(mock):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app as api

    cfg = load_config()
    cid = _ended_call(cfg)
    with Store(cfg.db_path) as s:
        s.set_crm_payload(cid, {"call_id": cid, "outcome": "interested"})
        s.crm_schedule(cid, status="dead", attempts=crm.OUTBOX_MAX_TRIES)
    res = TestClient(api()).post(f"/voip/calls/{cid}/crm/resend").json()
    assert res["ok"] and _row(cfg, cid) == {"status": "sent", "attempts": 0, "next_at": 0}


# ── manual calls take the CRM dial lock ──────────────────────────────────────────────
@pytest.fixture
def svc(mock):
    from xplogent.voip.service import VoipService

    s = VoipService()
    s.metas = []

    async def dialer(number, meta):
        s.metas.append(meta)
        return {"ok": True}
    s.sip_dialer = dialer
    return s


async def test_a_manual_call_to_a_candidate_takes_and_releases_the_lock(svc, mock):
    res = await svc.dial("+447700900123", via="sip")
    assert res["ok"] and "warning" not in res
    brief = svc.metas[0]["brief"]
    assert brief["manual_lock"] and brief["lock_token"] and brief["candidate"]["first_name"] == "Sarah"
    assert mock._locked("07700900123")
    svc.not_answered(svc.metas[0], "486 Busy Here")                 # rang out: free it at once
    await asyncio.sleep(0.2)
    assert not mock._locked("07700900123")


async def test_someone_elses_lock_refuses_the_call(svc, mock):
    mock.lock({"phone": "07700900124"}, by="Jane Recruiter")
    res = await svc.dial("+447700900124", via="sip")
    assert not res["ok"] and res["locked"] and "Jane Recruiter" in res["error"]
    assert not svc.metas


async def test_crm_lock_unavailable_still_lets_a_person_call(svc, mock, monkeypatch):
    async def no_lock(cfg, phone, applicant_id=None, ttl_seconds=900, **kw):
        return {"ok": False, "status": 0, "error": "ConnectError", "locked": False, "lock_token": "x",
                "locked_by": "", "expires_at": "", "data": {}}
    monkeypatch.setattr(crm, "dial_lock", no_lock)
    res = await svc.dial("+447700900123", via="sip")
    assert res["ok"] and "dial lock unavailable" in res["warning"]
    assert "lock_token" not in (svc.metas[0].get("brief") or {})


async def test_a_failed_dial_releases_the_lock_and_a_stale_lease_is_never_reused(svc, mock):
    async def failing(number, meta):
        return {"ok": False, "error": "503 Service Unavailable"}
    svc.sip_dialer = failing
    assert not (await svc.dial("+447700900123", via="sip"))["ok"]
    assert not mock._locked("07700900123")
    seen = []

    async def dialer(number, meta):
        seen.append(meta)
        return {"ok": True}
    svc.sip_dialer = dialer
    stale = {"candidate": {"first_name": "Sarah"}, "lock_token": "old", "manual_lock": True, "lock_phone": "x"}
    assert (await svc.dial("+447700900123", via="sip", brief=stale))["ok"]
    assert seen[0]["brief"]["lock_token"] != "old"
