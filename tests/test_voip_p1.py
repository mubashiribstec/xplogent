"""Regression tests for the phone-agent P1 fixes."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS, make_session, settle
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.voip import service as service_mod
from xplogent.voip.brain import CallBrain
from xplogent.voip.control import LiveCall, resolve_transfer_number
from xplogent.voip.engine import NO_TRANSFER_LINE, TRANSFER_FAILED_LINE


def _transfer_reply():
    return [Message(role=Role.ASSISTANT, content="Let me connect you.",
                    tool_calls=[ToolCall(id="t", name="transfer_to_human", arguments={"reason": "asked"})]),
            Message(role=Role.ASSISTANT, content="")]


@pytest.mark.parametrize("v, req, transport, out", [
    ({}, "", "sip", ""),
    ({"transfer_number": "+1 555 010 4444"}, "", "telnyx", "+15550104444"),
    ({"transfer_number": "+15550104444"}, "101", "sip", "101"),
    ({}, "101", "telnyx", ""),                       # extensions only exist on a PBX
    ({}, "sip:desk@pbx.local", "sip", "sip:desk@pbx.local"),
    ({}, "sip:desk@pbx.local", "telnyx", ""),
])
def test_resolve_transfer_number(v, req, transport, out):
    assert resolve_transfer_number(v, req, transport=transport) == out


def test_brain_offers_transfer_only_when_possible():
    store = Store(load_config().db_path)

    async def send(_b):
        return None

    async def xfer(reason):
        return None

    info = {"direction": "incoming", "from": "+15550101111", "to": "+1", "transport": "telnyx"}
    _s, _c, brain = service_mod.build_session(load_config(), store, info, send_audio=send, transfer=xfer,
                                              stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]),
                                              retrieve=None)
    assert not brain.can_transfer and "transfer_to_human" not in [t.name for t in brain.tools]
    assert "never offer a transfer" in brain._system([])
    assert "callback" in brain._run_tool("transfer_to_human", {}) and not brain.state.transfer
    save_user_config({"voip": {"transfer_number": "+15550104444"}})
    _s, _c, brain2 = service_mod.build_session(load_config(), store, info, send_audio=send, transfer=xfer,
                                               stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]),
                                               retrieve=None)
    assert brain2.can_transfer and "transfer_to_human" in [t.name for t in brain2.tools]
    store.close()


async def test_promised_transfer_without_a_line_keeps_the_call_going():
    session, sent, cleared, turns, hung = make_session(ScriptedProvider(_transfer_reply()), realtime=False)
    await session.on_text("I want a person")
    await settle(session)
    assert turns[-1] == ("agent", NO_TRANSFER_LINE) and not session.finished.is_set() and not hung


async def test_ai_transfer_failure_and_timeout_resume_the_agent():
    async def broken(reason):
        raise RuntimeError("no route")

    session, sent, cleared, turns, hung = make_session(ScriptedProvider(_transfer_reply()), realtime=False,
                                                       transfer=broken)
    await session.on_text("put me through")
    await settle(session)
    assert turns[-1] == ("agent", TRANSFER_FAILED_LINE) and not session.finished.is_set()
    assert not session.brain.paused and session.brain.state.transfer_failed

    async def silent(reason):            # accepted, but the carrier never reports back
        return None

    session2, _s, _c, turns2, _h = make_session(ScriptedProvider(_transfer_reply()), realtime=False,
                                                transfer=silent, transfer_timeout_s=0.2)
    await session2.on_text("put me through")
    await settle(session2)
    assert session2.transfer_pending and session2.brain.paused
    await asyncio.sleep(0.4)
    await settle(session2)
    assert not session2.transfer_pending and turns2[-1] == ("agent", TRANSFER_FAILED_LINE)
    await session.stop()
    await session2.stop()


async def test_cancelled_say_cannot_clear_a_newer_speaking_flag():
    session, *_ = make_session(ScriptedProvider([]), tts=FakeTTS(delay=0.05, chunks=10), realtime=False)
    first = asyncio.create_task(session._say("one"))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(session._say("two"))
    await asyncio.sleep(0.02)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert session._speaking          # still owned by the second reply
    await second
    assert not session._speaking


# ── kill switch + Telnyx transfer outcomes ────────────────────────────────────
class _Telnyx:
    def __init__(self, *a, **k):
        pass

    async def dial(self, to, frm, conn, webhook_url=""):
        return {"data": {"call_control_id": "cc-dialed"}}

    async def start_stream(self, cc, url):
        return {}

    async def aclose(self):
        pass


async def test_switched_off_agent_only_places_test_calls(monkeypatch):
    save_user_config({"voip": {"enabled": False, "from_number": "+15559990000", "connection_id": "c",
                               "calling_hours": {"enabled": False}}})
    monkeypatch.setattr(service_mod, "TelnyxClient", _Telnyx)
    svc = service_mod.VoipService()
    off = await svc.dial("+15550101111")
    assert not off["ok"] and "switched off" in off["error"]
    assert (await svc.dial("+15550101111", test=True))["ok"]
    # Its webhooks are still handled although the agent is off; strangers' aren't.
    hook = {"data": {"event_type": "call.answered", "payload": {
        "call_control_id": "cc-dialed", "direction": "outgoing", "from": "+1", "to": "+15550101111"}}}
    assert (await svc.handle_webhook(hook))["ok"] and svc.pending
    stranger = {"data": {"event_type": "call.initiated", "payload": {"call_control_id": "cc-x"}}}
    assert (await svc.handle_webhook(stranger))["ok"] is False


async def test_telnyx_transfer_outcomes_reach_the_session():
    import base64

    save_user_config({"voip": {"enabled": True}})
    svc = service_mod.VoipService()
    session, *_ = make_session(ScriptedProvider([]), realtime=False)
    svc.register(LiveCall(call_id=5, session=session, brain=session.brain, ref="cc-a",
                          transfer_to=lambda n: asyncio.sleep(0)))
    await session.start_transfer(lambda: asyncio.sleep(0), "test")
    state = base64.b64encode(b"xfer:cc-a").decode()
    leg_b_gone = {"data": {"event_type": "call.hangup", "payload": {"call_control_id": "cc-b", "client_state": state}}}
    await svc.handle_webhook(leg_b_gone)
    for _ in range(50):
        if not session.transfer_pending:
            break
        await asyncio.sleep(0.01)
    assert not session.transfer_pending and not session.finished.is_set()   # failed: agent resumes
    await session.start_transfer(lambda: asyncio.sleep(0), "again")
    bridged = {"data": {"event_type": "call.bridged", "payload": {"call_control_id": "cc-b", "client_state": state}}}
    await svc.handle_webhook(bridged)
    assert session.finished.is_set()
    await session.stop()


def test_dial_route_rejects_bad_contact_id():
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    res = TestClient(create_app()).post("/voip/dial", json={"contact_id": "abc"})
    assert res.status_code == 400


def test_outcome_is_validated():
    brain = CallBrain(ScriptedProvider([]))
    brain._run_tool("log_outcome", {"outcome": "Totally Sold!!"})
    assert brain.state.outcome == "other"
    brain._run_tool("log_outcome", {"outcome": "Interested"})
    assert brain.state.outcome == "interested"
