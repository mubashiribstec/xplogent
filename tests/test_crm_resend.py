"""CRM delivery status on each call, and Resend with the stored payload (idempotent)."""

from __future__ import annotations

import asyncio
import functools
import json

import httpx
import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip import crm


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setenv("XPLOGENT_CRM_API_TOKEN", "tok-123456")
    monkeypatch.setattr(crm, "deliver", functools.partial(crm.deliver, backoff_s=0))


async def test_status_follows_each_attempt():
    save_user_config({"voip": {"crm": {"enabled": True, "base_url": "https://crm.example.com"}}})
    seen = []
    out = await crm.deliver(load_config(), {"call_id": 1}, transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                            on_status=lambda s, d: seen.append(s))
    assert out["status"] == "failed" and seen == ["sending", "retrying", "retrying", "failed"]
    seen.clear()
    await crm.deliver(load_config(), {"call_id": 1}, transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                      on_status=lambda s, d: seen.append(s))
    assert seen == ["sending", "sent"]


async def test_failed_delivery_shows_on_the_call_and_resend_sends_it_once(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app
    from xplogent.voip.service import build_session, finalize_call

    save_user_config({"voip": {"enabled": True, "crm": {"enabled": True, "base_url": "http://127.0.0.1:9"}}})
    cfg = load_config()

    async def send(_b):
        return None

    with Store(cfg.db_path) as store:
        session, call_id, brain = build_session(
            cfg, store, {"direction": "incoming", "from": "+15550001111", "to": "2000"}, send_audio=send,
            stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]), retrieve=None)
        finalize_call(cfg, store, call_id, session, brain, "+15550001111")
        await asyncio.gather(*list(crm._TASKS))                  # the fire-and-forget delivery
        row = store.get_call(call_id)
        assert row["crm_status"] == "failed" and row["crm_detail"]
        assert "crm_payload" not in row and store.get_crm_payload(call_id)["call_id"] == call_id
        assert all("crm_payload" not in c for c in store.list_calls())

    activities: dict[int, int] = {}

    def laravel(request):                                       # dedupes on call_id, as documented
        body = json.loads(request.content)
        activities[body["call_id"]] = 1
        return httpx.Response(200, json={"ok": True})

    save_user_config({"voip": {"crm": {"base_url": "https://crm.example.com"}}})   # URL fixed
    monkeypatch.setattr(crm, "deliver", functools.partial(crm.deliver, transport=httpx.MockTransport(laravel)))
    client = TestClient(create_app())
    first = client.post(f"/voip/calls/{call_id}/crm/resend").json()
    second = client.post(f"/voip/calls/{call_id}/crm/resend").json()
    assert first["ok"] and first["status"] == "sent" and second["ok"]
    assert activities == {call_id: 1}                             # one CRM note per call
    with Store(cfg.db_path) as store:
        assert store.get_call(call_id)["crm_status"] == "sent"
        assert store.audit_rows(10, action="voip_crm_resend")
    assert client.post("/voip/calls/99999/crm/resend").json()["ok"] is False
