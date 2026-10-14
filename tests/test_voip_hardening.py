"""Phone agent hardening: number handling, do-not-call, privacy, pacing, cleanup."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS, make_session, speak_utterance
from xplogent.core.config import load_config
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role
from xplogent.voip.brain import CallBrain
from xplogent.voip.service import VoipService, build_session, customer_number, finalize_call
from xplogent.voip.telephony import telnyx_trunk

OUR_NUMBER = "+15559990000"
CUSTOMER = "+15550102000"


def _outbound(ccid="cc-out") -> dict[str, str]:
    return {"event": "call.answered", "call_control_id": ccid, "from": OUR_NUMBER,
            "to": CUSTOMER, "direction": "outgoing"}


# ── numbers ───────────────────────────────────────────────────────────────────
def test_customer_number_depends_on_direction():
    assert customer_number(_outbound()) == CUSTOMER
    assert customer_number({"from": CUSTOMER, "to": OUR_NUMBER, "direction": "incoming"}) == CUSTOMER


def test_dnc_matches_regardless_of_formatting(tmp_path):
    store = Store(tmp_path / "d.db")
    store.add_dnc("+1 (555) 010-3000", "asked")
    assert store.is_dnc("5550103000")
    assert store.is_dnc("+15550103000")
    assert not store.is_dnc("5550103001")
    assert not store.is_dnc("3000")          # too short to identify anyone
    store.close()


def test_short_caller_id_does_not_match_a_random_contact(tmp_path):
    store = Store(tmp_path / "c.db")
    store.add_contact("Ann", "+15550102000")
    assert store.find_contact_by_phone("2000") is None
    assert store.find_contact_by_phone("") is None
    assert store.find_contact_by_phone("555-010-2000")["name"] == "Ann"
    store.close()


def _fake_session_parts():
    async def send(_b):
        return None

    return dict(send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                provider=ScriptedProvider([]), retrieve=None)


def test_outbound_call_uses_the_called_customer(tmp_path):
    cfg = load_config()
    store = Store(cfg.db_path)
    store.add_contact("Customer", CUSTOMER)
    store.add_contact("Not us", OUR_NUMBER)
    session, call_id, brain = build_session(cfg, store, _outbound(), **_fake_session_parts())
    assert brain.contact and brain.contact["name"] == "Customer"
    call = store.get_call(call_id)
    assert call["direction"] == "outbound" and call["contact_name"] == "Customer"

    # "Stop calling me" on an outbound call blocks the customer, not our own number.
    brain.state.do_not_call = True
    finalize_call(cfg, store, call_id, session, brain, customer_number(_outbound()))
    assert store.is_dnc(CUSTOMER)
    assert not store.is_dnc(OUR_NUMBER)
    store.close()


# ── privacy ───────────────────────────────────────────────────────────────────
def test_lookup_customer_only_returns_the_callers_own_record(tmp_path):
    store = Store(tmp_path / "p.db")
    store.add_contact("Victim", "+15550109999", notes="card ending 4242")
    caller = {"name": "Caller", "phone": CUSTOMER, "status": "new", "notes": ""}
    brain = CallBrain(ScriptedProvider([]), contact=caller, store=store)
    for query in ("Victim", "+15550109999", "5550109999", "9999"):
        out = brain._run_tool("lookup_customer", {"query": query})
        assert "Victim" not in out and "4242" not in out
        assert "Caller" in out
    stranger = CallBrain(ScriptedProvider([]), contact=None, store=store)
    assert "no customer record" in stranger._run_tool("lookup_customer", {"query": "Victim"})
    store.close()


def test_known_dnc_customer_is_flagged_in_the_prompt():
    cfg = load_config()
    store = Store(cfg.db_path)
    store.add_contact("Opted Out", CUSTOMER)
    store.add_dnc(CUSTOMER, "asked")
    inbound = {"from": CUSTOMER, "to": OUR_NUMBER, "direction": "incoming"}
    _session, _cid, brain = build_session(cfg, store, inbound, **_fake_session_parts())
    assert "do-not-call" in brain._system([])
    store.close()


# ── do-not-call enforcement ───────────────────────────────────────────────────
class _FakeTelnyx:
    actions: list[tuple[str, str]] = []

    def __init__(self, *a, **k):
        pass

    async def answer(self, cc):
        self.actions.append(("answer", cc))

    async def start_stream(self, cc, url):
        self.actions.append(("stream", cc))

    async def hangup(self, cc):
        self.actions.append(("hangup", cc))

    async def dial(self, to, frm, conn, webhook_url=""):
        self.actions.append(("dial", to))
        return {"data": {"call_control_id": "new"}}

    async def aclose(self):
        self.actions.append(("close", ""))


@pytest.fixture
def telnyx(monkeypatch):
    from xplogent.core.config import save_user_config

    save_user_config({"voip": {"enabled": True, "public_url": "https://agent.test",
                               "from_number": OUR_NUMBER, "connection_id": "conn",
                               "calling_hours": {"enabled": False}}})
    _FakeTelnyx.actions = []
    monkeypatch.setattr(telnyx_trunk, "TelnyxClient", _FakeTelnyx)
    return _FakeTelnyx


async def _drain(svc: VoipService) -> None:
    for _ in range(50):
        if not svc.telnyx._tasks:
            return
        await asyncio.sleep(0.01)


def _hook(info: dict[str, str]) -> dict:
    return {"data": {"event_type": info["event"], "payload": info}}


@pytest.mark.asyncio
async def test_outbound_call_to_dnc_number_is_hung_up_not_streamed(telnyx):
    store = Store(load_config().db_path)
    store.add_dnc(CUSTOMER, "asked")
    store.close()
    svc = VoipService()
    res = await svc.handle_webhook(_hook(_outbound()))
    await _drain(svc)
    assert res.get("blocked") == "do_not_call"
    assert ("hangup", "cc-out") in telnyx.actions
    assert not any(a == "stream" for a, _ in telnyx.actions)
    assert not svc.telnyx.pending


@pytest.mark.asyncio
async def test_dial_refuses_dnc_numbers(telnyx):
    store = Store(load_config().db_path)
    store.add_dnc("555-010-2000", "asked")
    store.close()
    svc = VoipService()
    res = await svc.dial(CUSTOMER)
    assert res["ok"] is False and "do-not-call" in res["error"]
    assert not any(a == "dial" for a, _ in telnyx.actions)
    ok = await svc.dial("+15550104444")
    assert ok["ok"] and ("dial", "+15550104444") in telnyx.actions


@pytest.mark.asyncio
async def test_every_webhook_client_is_closed(telnyx):
    svc = VoipService()
    for event in ("call.initiated", "call.answered", "call.hangup", "call.bridged"):
        await svc.handle_webhook(_hook({"event": event, "call_control_id": "c1",
                                        "from": CUSTOMER, "to": OUR_NUMBER,
                                        "direction": "incoming"}))
    await _drain(svc)
    opened = sum(1 for a, _ in telnyx.actions if a in ("answer", "stream", "hangup"))
    closed = sum(1 for a, _ in telnyx.actions if a == "close")
    assert opened == closed == 2


@pytest.mark.asyncio
async def test_stale_tokens_and_calls_expire(telnyx, monkeypatch):
    svc = VoipService()
    await svc.handle_webhook(_hook({"event": "call.initiated", "call_control_id": "c1",
                                    "from": CUSTOMER, "to": OUR_NUMBER, "direction": "incoming"}))
    await svc.handle_webhook(_hook({"event": "call.answered", "call_control_id": "c1",
                                    "from": CUSTOMER, "to": OUR_NUMBER, "direction": "incoming"}))
    await _drain(svc)
    assert svc.telnyx.pending and svc.telnyx.by_call
    later = time.monotonic() + telnyx_trunk.TelnyxTrunk.CALL_TTL + 1
    monkeypatch.setattr(telnyx_trunk.time, "monotonic", lambda: later)
    svc.telnyx._expire()
    assert not svc.telnyx.pending and not svc.telnyx.by_call


# ── real-time pacing / barge-in ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_speech_is_paced_at_playback_speed():
    stamps: list[float] = []
    session, sent, *_ = make_session(ScriptedProvider([]), tts=FakeTTS(chunks=5), lead_s=0.05)

    async def send_audio(b):
        stamps.append(time.monotonic())
        sent.append(b)

    session.send_audio = send_audio
    started = time.monotonic()
    await session._say("half a second of audio")
    elapsed = time.monotonic() - started
    assert all(len(f) <= 160 for f in sent)                  # 20 ms frames
    assert sum(len(f) for f in sent) == 4000                 # 500 ms of 8 kHz μ-law
    assert elapsed >= 0.45                                   # not dumped instantly
    assert stamps[-1] - stamps[0] >= 0.35


@pytest.mark.asyncio
async def test_barge_in_works_on_buffered_reply():
    """TTS that is instantly ready used to finish 'speaking' before the caller heard it."""
    prov = ScriptedProvider([Message(role=Role.ASSISTANT,
                                     content="This is a reply that takes a while to play out.")])
    session, _sent, cleared, _turns, _ = make_session(prov, tts=FakeTTS(chunks=20))
    await speak_utterance(session)
    for _ in range(60):
        if session._speaking:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)               # synthesis is long done; playback is not
    assert session._speaking
    from tests.test_voip import tone_mulaw

    await session.on_audio(tone_mulaw(200))
    assert cleared and not session._speaking
    await session.stop()


@pytest.mark.asyncio
async def test_idle_prompt_is_tracked_and_interruptible():
    session, sent, cleared, turns, hung = make_session(
        ScriptedProvider([]), tts=FakeTTS(chunks=20), idle_prompt_s=0.0, idle_hangup_s=60)
    session._last_activity = time.time() - 5
    watchdog = asyncio.create_task(session._watch())
    for _ in range(100):
        if session._speaking:
            break
        await asyncio.sleep(0.02)
    assert ("agent", "Are you still there?") in turns
    assert session._turn_task is not None and not session._turn_task.done()
    await session._barge_in()
    await asyncio.sleep(0)
    assert session._turn_task.cancelled() or session._turn_task.done()
    session.finished.set()
    watchdog.cancel()
    await session.stop()
    assert not hung
