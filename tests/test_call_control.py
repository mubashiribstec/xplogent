"""Live call control, manual calls and browser test calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeTTS, make_session, settle
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.voip import service as service_mod
from xplogent.voip.control import LiveCall, calling_hours_problem, clean_number
from xplogent.voip.service import VoipService, get_service
from xplogent.voip.telephony import telnyx_trunk


@pytest.mark.parametrize("raw, out", [
    ("+1 (555) 010-0199", "+15550100199"), ("0044 20 7946 0958", ""), ("447700900123", "447700900123"),
    ("12", ""), ("+1+2", ""), ("abc", ""), ("5+55", ""),
])
def test_clean_number(raw, out):
    assert clean_number(raw) == out


def test_calling_hours_follow_the_contact_time_zone():
    v = {"calling_hours": {"start": "09:00", "end": "20:00"}}
    noon_utc = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    assert calling_hours_problem(v, {"timezone": "Europe/London"}, noon_utc) == ""
    late = calling_hours_problem(v, {"timezone": "America/Los_Angeles"}, noon_utc)  # 05:00 there
    assert "outside calling hours" in late and "America/Los_Angeles" in late
    assert calling_hours_problem({"calling_hours": {"enabled": False}}, None, noon_utc) == ""
    overnight = {"calling_hours": {"start": "22:00", "end": "06:00", "timezone": "UTC"}}
    assert calling_hours_problem(overnight, None, datetime(2026, 6, 1, 23, 0, tzinfo=UTC)) == ""


# ── engine/brain controls ────────────────────────────────────────────────────
async def test_pause_say_and_instruct():
    prompts = []

    class Recorder(ScriptedProvider):
        async def stream(self, messages, tools=None, **kw):
            prompts.append(messages[0].content)
            async for ev in super().stream(messages, tools, **kw):
                yield ev

    provider = Recorder([Message(role=Role.ASSISTANT, content="Sure, ten percent off.")])
    session, sent, cleared, turns, hung = make_session(provider, tts=FakeTTS(), realtime=False)
    session.brain.paused = True
    await session.on_text("hello?")
    await settle(session)
    assert turns == [("caller", "hello?")] and not prompts      # paused: AI stays quiet

    await session.say("One moment, I'm checking that for you.")
    await settle(session)
    assert turns[-1] == ("agent", "One moment, I'm checking that for you.")
    assert session.brain.history[-1].content.startswith("One moment")

    session.brain.paused = False
    session.brain.instruct("Offer the 10% discount.")
    await session.on_text("can you do better on price?")
    await settle(session)
    assert "SUPERVISOR INSTRUCTIONS" in prompts[0] and "10% discount" in prompts[0]
    assert turns[-1] == ("agent", "Sure, ten percent off.")
    await session.end()
    assert hung and session.finished.is_set()


# ── manual dial ──────────────────────────────────────────────────────────────
class _Telnyx:
    dialed: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def dial(self, to, frm, conn, webhook_url="", **kw):
        self.dialed.append(to)
        return {"data": {"call_control_id": "cc-1"}}

    async def aclose(self):
        pass


@pytest.fixture
def telnyx(monkeypatch):
    save_user_config({"voip": {"enabled": True, "from_number": "+15559990000", "connection_id": "c",
                               "calling_hours": {"start": "00:00", "end": "00:00", "timezone": "UTC",
                                                 "days": {"sat": ""}}}})
    _Telnyx.dialed = []
    monkeypatch.setattr(telnyx_trunk, "TelnyxClient", _Telnyx)
    return _Telnyx


async def test_dial_checks_hours_dnc_and_remembers_the_contact(telnyx):
    with Store(load_config().db_path) as s:
        cid = s.add_contact("Ana", "+15550101111")
        s.add_dnc("+15550102222", "asked")
    svc = VoipService()
    # 00:00–00:00 means never inside hours: a real call is refused, a test call isn't.
    res = await svc.dial("+15550101111", contact_id=cid)
    assert not res["ok"] and res["hours"] and not telnyx.dialed
    assert (await svc.dial("+15550101111", contact_id=cid, ignore_hours=True))["ok"]
    assert svc.telnyx.outbound["cc-1"] == {"contact_id": cid, "test": False, "transport": "telnyx"}
    assert (await svc.dial("+1 555 010 3333", test=True))["ok"]
    assert telnyx.dialed == ["+15550101111", "+15550103333"]
    assert "do-not-call" in (await svc.dial("+15550102222", test=True))["error"]
    assert "valid" in (await svc.dial("hello", test=True))["error"]
    assert "SIP" in (await svc.dial("+15550103333", test=True, via="sip"))["error"]


async def test_test_calls_never_change_contacts():
    from tests.test_voip import FakeSTT
    from xplogent.voip.service import build_session, finalize_call

    cfg = load_config()
    store = Store(cfg.db_path)
    cid = store.add_contact("Ana", "+15550101111")

    async def send(_b):
        return None

    info = {"direction": "outgoing", "from": "+1555", "to": "+15550101111", "test": True,
            "transport": "telnyx", "contact_id": cid}
    session, call_id, brain = build_session(cfg, store, info, send_audio=send, stt=FakeSTT(),
                                            tts=FakeTTS(), provider=ScriptedProvider([]), retrieve=None)
    brain.state.do_not_call, brain.state.outcome = True, "do_not_call"
    res = finalize_call(cfg, store, call_id, session, brain, "+15550101111")
    assert res["test"] and not store.is_dnc("+15550101111")
    assert store.get_contact(cid)["status"] == "new"
    call = store.get_call(call_id)
    assert call["test"] == 1 and call["transport"] == "telnyx" and call["contact_name"] == "Ana"
    store.close()


# ── routes ───────────────────────────────────────────────────────────────────
def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    return TestClient(create_app())


class _FakeSession:
    def __init__(self):
        self.said, self.ended, self.stopped = [], False, False

    async def say(self, text):
        self.said.append(text)

    async def end(self):
        self.ended = True

    async def hush(self):
        pass

    async def start_transfer(self, attempt, reason=""):
        self.transfer_reason = reason
        await attempt()

    async def stop(self):
        self.stopped = True


def test_control_routes():
    from xplogent.voip.brain import CallBrain

    svc = get_service()
    moved = []

    async def transfer_to(number):
        moved.append(number)

    brain = CallBrain(ScriptedProvider([]))
    sess = _FakeSession()
    svc.register(LiveCall(call_id=77, session=sess, brain=brain, number="+15550101111",
                          transfer_to=transfer_to))
    try:
        c = _client()
        active = c.get("/voip/status").json()["active"]
        assert active[0]["call_id"] == 77 and active[0]["can_transfer"] and not active[0]["paused"]
        assert c.post("/voip/calls/77/say", json={"text": "Hold on"}).json()["ok"] and sess.said == ["Hold on"]
        assert c.post("/voip/calls/77/instruct", json={"text": "be brief"}).json()["notes"] == ["be brief"]
        assert c.post("/voip/calls/77/pause-ai", json={"paused": True}).json()["paused"] and brain.paused
        assert c.post("/voip/calls/77/transfer", json={}).json()["ok"] is False     # no number anywhere
        assert c.post("/voip/calls/77/transfer", json={"to": "+1 555 010 4444"}).json()["to"] == "+15550104444"
        assert moved == ["+15550104444"] and sess.transfer_reason == "transferred from the dashboard"
        assert c.post("/voip/calls/77/hangup").json()["ok"] and sess.ended
        assert c.post("/voip/calls/99/hangup").status_code == 404
    finally:
        svc.unregister(77)


def test_dial_route_uses_the_contact_number(monkeypatch):
    calls = []

    async def fake_dial(number, **kw):
        calls.append((number, kw))
        return {"ok": True}

    monkeypatch.setattr(get_service(), "dial", fake_dial)
    with Store(load_config().db_path) as s:
        cid = s.add_contact("Ana", "+15550101111")
    c = _client()
    assert c.post("/voip/dial", json={"contact_id": cid}).json()["ok"]
    assert c.post("/voip/dial", json={"number": "+15550109999", "test": True}).json()["ok"]
    assert calls[0] == ("+15550101111", {"contact_id": cid, "test": False, "via": "telnyx", "ignore_hours": False,
                                         "brief": None})
    assert calls[1][1]["test"] is True
    assert c.post("/voip/dial", json={"number": "1", "via": "pigeon"}).json()["ok"] is False


def test_browser_test_call(monkeypatch):
    from xplogent.interfaces.api import deps

    save_user_config({"voip": {"greeting": "Hi from {company}", "company": "Acme"}})
    with Store(load_config().db_path) as s:
        cid = s.add_contact("Ana", "+15550101111")
    from datetime import datetime, timedelta

    from xplogent.voip import hours
    at = hours.next_allowed(load_config().voip or {}, None, datetime.now().astimezone() + timedelta(days=1))
    replies = [Message(role=Role.ASSISTANT, content="", tool_calls=[
                   ToolCall(id="1", name="book_callback", arguments={"at": at.isoformat(timespec="minutes"),
                                                                     "spoken": "Tuesday"})]),
               Message(role=Role.ASSISTANT, content="Great, I'll call you Tuesday.")]
    monkeypatch.setattr(deps, "build_provider", lambda spec, **kw: ScriptedProvider(list(replies)))

    async def no_knowledge(q):
        return []

    monkeypatch.setattr(service_mod, "_make_retriever", lambda cfg, store, k: no_knowledge)
    c = _client()
    got = []
    with c.websocket_connect("/ws/voip/test") as ws:
        ws.send_text(json.dumps({"type": "start", "voice": False, "contact_id": cid}))
        started = json.loads(ws.receive_text())
        assert started["type"] == "started" and started["contact"] == "Ana"
        # The system-injected AI disclosure precedes the configured greeting (C3);
        # recording is on by default, so it says so.
        assert json.loads(ws.receive_text()) == {"type": "turn", "speaker": "agent",
            "text": "You're speaking with an AI assistant, and this call is recorded "
                    "so we can help you better. Hi from Acme"}
        assert get_service().active[started["call_id"]].transport == "browser"
        ws.send_text(json.dumps({"type": "text", "text": "call me back on Tuesday"}))
        while True:
            msg = json.loads(ws.receive_text())
            got.append(msg)
            if msg["type"] == "events":
                break
        ws.send_text(json.dumps({"type": "hangup"}))
        while msg["type"] != "ended":
            msg = json.loads(ws.receive_text())
    assert {"type": "turn", "speaker": "caller", "text": "call me back on Tuesday"} in got
    assert {"type": "turn", "speaker": "agent", "text": "Great, I'll call you Tuesday."} in got
    assert any("book_callback" in e for e in got[-1]["items"])
    assert msg["outcome"] == "callback" and msg["callback"].startswith(at.strftime("%A"))
    with Store(load_config().db_path) as s:
        call = s.get_call(started["call_id"])
        assert call["test"] == 1 and call["direction"] == "test" and len(call["turns"]) == 3
        assert s.get_contact(cid)["status"] == "new"                # untouched by a test call
    assert started["call_id"] not in get_service().active


async def test_interrupted_reply_keeps_what_was_said():
    import asyncio

    provider = ScriptedProvider([Message(role=Role.ASSISTANT, content="First part. Second part is long.")])
    session, sent, cleared, turns, hung = make_session(provider, tts=FakeTTS(delay=0.05), realtime=False)
    await session.on_text("hi there")
    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0.01)
    await session.say("Let me stop you there.")
    await settle(session)
    agent = [t for s, t in turns if s == "agent"]
    assert agent[0].startswith("First part.") and agent[0].endswith("…")
    assert agent[-1] == "Let me stop you there."


def test_browser_test_call_carries_on_as_text_when_the_voice_fails(monkeypatch):
    """No OpenAI key / speech server down: the greeting and replies still show, the
    browser is told once, and a thinking status precedes each reply."""
    from xplogent.interfaces.api import deps

    class BrokenTTS:
        sample_rate = 24000
        calls = 0

        async def synthesize(self, text):
            BrokenTTS.calls += 1
            raise RuntimeError("401 Unauthorized: no OpenAI key")
            yield b""                                       # pragma: no cover

        async def aclose(self):
            pass

    monkeypatch.setattr(service_mod, "_speech_tts", lambda v: BrokenTTS())
    monkeypatch.setattr(deps, "build_provider", lambda spec, **kw: ScriptedProvider(
        [Message(role=Role.ASSISTANT, content="We open at nine."),
         Message(role=Role.ASSISTANT, content="Bye now.")]))

    async def no_knowledge(q):
        return []

    monkeypatch.setattr(service_mod, "_make_retriever", lambda cfg, store, k: no_knowledge)
    got = []
    with _client().websocket_connect("/ws/voip/test") as ws:
        ws.send_text(json.dumps({"type": "start", "voice": True}))
        assert json.loads(ws.receive_text())["type"] == "started"
        ws.send_text(json.dumps({"type": "text", "text": "when do you open?"}))
        while not any(m.get("text") == "We open at nine." for m in got):
            got.append(json.loads(ws.receive_text()))
        ws.send_text(json.dumps({"type": "text", "text": "thanks"}))
        while not any(m.get("text") == "Bye now." for m in got):
            got.append(json.loads(ws.receive_text()))
        ws.send_text(json.dumps({"type": "hangup"}))
    kinds = [m["type"] for m in got]
    assert got[0]["type"] == "turn" and got[0]["speaker"] == "agent"       # the greeting is shown
    failed = [m for m in got if m["type"] == "voice_failed"]
    assert len(failed) == 1 and "no OpenAI key" in failed[0]["error"]
    assert BrokenTTS.calls == 1                                              # then text only
    caller = kinds.index("turn", 1)
    assert got[caller]["speaker"] == "caller" and kinds[caller + 1] == "thinking"
    assert not any("Sorry, I had a problem" in (m.get("text") or "") for m in got)


def test_browser_test_call_reports_a_session_that_cannot_start(monkeypatch):
    from xplogent.interfaces.api import deps

    def bad(spec, **kw):
        raise ValueError("no Ollama server named 'gpu': add it under Settings → Ollama servers")

    monkeypatch.setattr(deps, "build_provider", bad)
    with _client().websocket_connect("/ws/voip/test") as ws:
        ws.send_text(json.dumps({"type": "start", "voice": False}))
        msg = json.loads(ws.receive_text())
    assert msg["type"] == "error" and "no Ollama server named 'gpu'" in msg["error"]


async def test_a_failed_turn_still_records_what_the_agent_said():
    class FailingTTS(FakeTTS):
        async def synthesize(self, text):
            raise RuntimeError("speech server down")
            yield b""                                       # pragma: no cover

    provider = ScriptedProvider([Message(role=Role.ASSISTANT, content="We open at nine.")])
    session, _sent, _c, turns, _h = make_session(provider, tts=FailingTTS(), realtime=False)
    await session.on_text("when do you open?")
    await settle(session)
    assert ("agent", "We open at nine.") in turns
    assert "speech server down" in session.turn_timings[-1]["error"]
