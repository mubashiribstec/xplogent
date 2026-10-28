"""WP4: exact-time callbacks — checked against the calendar, stored, and placed on time."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.voip import alerts, callbacks
from xplogent.voip.brain import CallBrain

V = {"calling_hours": {"enabled": True, "start": "09:00", "end": "20:00", "timezone": "Europe/London",
                       "days": {"sat": "10:00-16:00", "sun": "closed"}, "bank_holidays": "closed"}}
NOW = datetime(2026, 10, 28, 10, 15, tzinfo=UTC)          # Wednesday, 10:15 in London (GMT)


def test_now_line():
    assert callbacks.now_line(V, None, NOW) == "NOW: Wednesday 28 October 2026, 10:15 (Europe/London)."


def test_check_accepts_a_good_time_and_offers_slots_otherwise():
    ok = callbacks.check(V, None, "2026-10-29T17:30", "ai", NOW)
    assert ok["ok"] and ok["spoken"] == "Thursday 29 October at 5:30 pm"
    assert ok["at"].isoformat() == "2026-10-29T17:30:00+00:00"
    late = callbacks.check(V, None, "2026-10-29T21:00", "ai", NOW)
    assert not late["ok"] and "calling hours" in late["reason"]
    assert late["slots_spoken"][0] == "Friday 30 October at 9 am"
    sunday = callbacks.check(V, None, "2026-11-01T12:00", "ai", NOW)
    assert "Sundays" in sunday["reason"] and sunday["slots_spoken"][0].startswith("Monday 2 November")
    recruiter = callbacks.check(V, None, "2026-10-29T18:00", "recruiter", NOW)   # recruiters stop at 17:30
    assert not recruiter["ok"] and "recruiters' hours" in recruiter["reason"]
    assert callbacks.check(V, None, "2026-10-29T17:00", "recruiter", NOW)["ok"]
    assert "passed" in callbacks.check(V, None, "2026-10-27T10:00", "ai", NOW)["reason"]
    assert "30 days" in callbacks.check(V, None, "2027-01-20T10:00", "ai", NOW)["reason"]
    assert "not a date" in callbacks.check(V, None, "tomorrow", "ai", NOW)["reason"]
    soon = callbacks.check(V, None, "2026-10-27T10:00", "ai", NOW)
    assert soon["slots_spoken"][0] == "Wednesday 28 October at 10:30 am"     # rounded to the quarter


def brain_booking(args, then="Lovely, speak then.", fixed_now=NOW):
    msgs = [Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="1", name="book_callback",
                                                                          arguments=args)]),
            Message(role=Role.ASSISTANT, content=then)]
    b = CallBrain(ScriptedProvider(msgs))
    b.callback_check = lambda at, who: callbacks.check(V, None, at, who, fixed_now)
    b.now_line = lambda: callbacks.now_line(V, None, fixed_now)
    return b


async def run(b, text="Call me tomorrow at half five"):
    return [s async for s in b.respond(text)]


async def test_agent_books_an_exact_time():
    b = brain_booking({"at": "2026-10-29T17:30", "spoken": "tomorrow at half five"})
    assert "NOW: Wednesday 28 October 2026" in b._system([])
    await run(b)
    st = b.state
    assert st.outcome == "callback" and st.callback_who == "ai"
    assert st.callback_at == "2026-10-29T17:30:00+00:00" and st.callback.startswith("Thursday 29 October")
    assert any("Read that day and time back" in e for e in st.events)


async def test_refused_time_comes_with_slots():
    b = brain_booking({"at": "2026-10-29T21:00"})
    await run(b, "Call me tomorrow at nine at night")
    assert not b.state.callback_at
    assert any("REFUSED" in e and "Friday 30 October at 9 am" in e for e in b.state.events)


async def test_vague_time_is_asked_once_then_noted():
    b = brain_booking({"when": "sometime next week"})
    await run(b)
    assert any("no exact time" in e for e in b.state.events) and not b.state.outcome
    b._replies = None
    b.provider = ScriptedProvider([Message(role=Role.ASSISTANT, content="", tool_calls=[
        ToolCall(id="2", name="book_callback", arguments={"when": "sometime next week"})]),
        Message(role=Role.ASSISTANT, content="OK.")])
    await run(b, "I really don't know, just next week")
    assert b.state.outcome == "callback" and b.state.callback == "sometime next week" and not b.state.callback_at


async def test_a_booked_callback_survives_a_later_interested():
    b = brain_booking({"at": "2026-10-29T17:00", "who": "recruiter"})
    await run(b)
    assert b.state.outcome == "recruiter_callback"
    b._run_tool("log_outcome", {"outcome": "interested"})
    assert b.state.outcome == "recruiter_callback"
    b._run_tool("log_outcome", {"outcome": "not_interested"})
    assert b.state.outcome == "not_interested"


def _session(info):
    from xplogent.voip.service import build_session

    cfg = load_config()
    store = Store(cfg.db_path)

    async def send(_b):
        return None
    session, call_id, brain = build_session(cfg, store, info, send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                                            provider=ScriptedProvider([]), retrieve=None)
    return cfg, store, session, call_id, brain


INFO = {"direction": "outgoing", "from": "+44113", "to": "+447700900123", "transport": "sip", "line": "extension",
        "brief": {"applicant_id": 123, "sale_id": 45, "candidate": {"first_name": "Sarah"}}}


def test_ai_callback_is_stored_and_recruiter_callback_goes_to_the_crm():
    from xplogent.voip import crm
    from xplogent.voip.service import finalize_call

    cfg, store, session, call_id, brain = _session(INFO)
    assert brain.callback_check is not None and "NOW:" in brain._system([])
    brain.state.callback_at, brain.state.callback_who = "2026-10-29T17:30:00+00:00", "ai"
    brain.state.callback, brain.state.outcome = "Thursday 29 October at 5:30 pm", "callback"
    finalize_call(cfg, store, call_id, session, brain, "+447700900123")
    rows = store.list_callbacks()
    assert len(rows) == 1 and rows[0]["via"] == "extension" and rows[0]["status"] == "pending"
    assert rows[0]["brief"]["applicant_id"] == 123 and "call back Thursday" in rows[0]["brief"]["callback_note"]
    cfg, store2, session, call_id, brain = _session(INFO)
    brain.state.callback_at, brain.state.callback_who = "2026-10-29T17:00:00+00:00", "recruiter"
    brain.state.outcome = "recruiter_callback"
    payload = crm.build_payload(cfg, store2, call_id, {"outcome": "recruiter_callback"}, brain, session)
    assert payload["outcome"] == "recruiter_callback" and payload["callback_at"] == "2026-10-29T17:00:00+00:00"
    finalize_call(cfg, store2, call_id, session, brain, "+447700900123")
    assert len(store2.list_callbacks()) == 1                              # the AI never calls these
    store.close()
    store2.close()


async def test_due_callbacks_are_placed_once(monkeypatch):
    from xplogent.voip.service import VoipService

    alerts.reset()
    save_user_config({"voip": {"enabled": True, "calling_hours": {"enabled": False}}})
    cfg = load_config()
    with Store(cfg.db_path) as s:
        due = s.add_scheduled_callback(7, "201", time.time() - 60, via="extension", spoken="now-ish",
                                       brief={"applicant_id": 123, "callback_note": "They asked us to call back now-ish."})
        s.add_scheduled_callback(8, "202", time.time() + 3600, via="extension")
        late = s.add_scheduled_callback(9, "203", time.time() - 7 * 3600, via="extension", spoken="this morning")
    svc = VoipService()
    dialled = []

    async def dialer(number, meta):
        dialled.append((number, meta))
        return {"ok": True, "state": "ringing"}
    svc.sip_extension_dialer = dialer
    await svc.run_due_callbacks(cfg)
    assert [d[0] for d in dialled] == ["201"]
    assert dialled[0][1]["brief"]["callback_note"].startswith("They asked us to call back")
    with Store(cfg.db_path) as s:
        status = {r["id"]: r["status"] for r in s.list_callbacks()}
    assert status[due] == "dialled" and status[late] == "missed"
    assert alerts.recent()[0]["kind"] == "callback_missed"
    await svc.run_due_callbacks(cfg)
    assert len(dialled) == 1                                              # never twice


async def test_hours_changed_since_booking_reschedules(monkeypatch):
    from zoneinfo import ZoneInfo

    from xplogent.voip.service import VoipService

    h = datetime.now(ZoneInfo("Europe/London")).hour       # open later today only, closed right now
    window = f"{(h + 2) % 24:02d}:00-{(h + 3) % 24:02d}:00"
    save_user_config({"voip": {"enabled": True, "calling_hours": {
        "bank_holidays": "open", "days": {d: window for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}}}})
    cfg = load_config()
    with Store(cfg.db_path) as s:
        cid = s.add_scheduled_callback(7, "201", time.time() - 60, via="extension")
    svc = VoipService()

    async def dialer(number, meta):
        raise AssertionError("must not dial outside hours")
    svc.sip_extension_dialer = dialer
    await svc.run_due_callbacks(cfg)
    with Store(cfg.db_path) as s:
        row = next(r for r in s.list_callbacks() if r["id"] == cid)
    assert row["status"] == "pending" and row["at"] > time.time() + 3600         # moved to the next opening


def test_callbacks_endpoints():
    from xplogent.interfaces.api.server import create_app

    with Store(load_config().db_path) as s:
        cid = s.add_scheduled_callback(7, "+447700900123", time.time() + 3600, spoken="Thursday at 5:30 pm")
    with TestClient(create_app()) as c:
        rows = c.get("/voip/callbacks").json()["callbacks"]
        assert rows[0]["spoken"] == "Thursday at 5:30 pm" and rows[0]["status"] == "pending"
        assert c.post(f"/voip/callbacks/{cid}/cancel").json()["ok"]
        assert c.get("/voip/callbacks").json()["callbacks"][0]["status"] == "cancelled"


async def test_agent_that_books_and_hangs_up_silently_still_says_goodbye():
    from tests.test_voip import make_session, settle

    replies = [Message(role=Role.ASSISTANT, content="", tool_calls=[
        ToolCall(id="1", name="book_callback", arguments={"at": "2026-10-29T17:30"}),
        ToolCall(id="2", name="end_call", arguments={})]),
        Message(role=Role.ASSISTANT, content="")]
    session, sent, cleared, turns, hung = make_session(ScriptedProvider(replies))
    session.brain.callback_check = lambda at, who: callbacks.check(V, None, at, who, NOW)
    await session.start()
    await session.on_text("call me tomorrow at half five")
    await settle(session)
    assert ("agent", "Lovely, so we'll call you back on Thursday 29 October at 5:30 pm. "
                     "Thanks for your time, goodbye.") in turns
    assert hung
    await session.stop()


def test_a_booked_callback_lets_an_unfinished_screen_end():
    from xplogent.voip.fields import build_collector

    b = CallBrain(ScriptedProvider([]), fields=build_collector([{"name": "right_to_work", "validator": "nonempty",
                                                                "required": True}]))
    assert b._run_tool("end_call", {}).startswith("REFUSED")
    b.callback_check = lambda at, who: callbacks.check(V, None, at, who, NOW)
    b._run_tool("book_callback", {"at": "2026-10-29T17:30"})
    assert b._run_tool("end_call", {}) == "call will end after your goodbye"
    assert b.state.exit_path == "callback" and b.state.end


async def test_model_failure_after_booking_confirms_the_booking():
    from tests.test_voip import make_session, settle

    class Booker(ScriptedProvider):
        async def stream(self, messages, tools=None, **kw):
            if any(m.role == Role.TOOL for m in messages):
                raise RuntimeError("429 Too Many Requests")
            async for ev in super().stream(messages, tools, **kw):
                yield ev
    replies = [Message(role=Role.ASSISTANT, content="", tool_calls=[
        ToolCall(id="1", name="book_callback", arguments={"at": "2026-10-29T17:30"})])]
    session, sent, cleared, turns, hung = make_session(Booker(replies))
    session.brain.callback_check = lambda at, who: callbacks.check(V, None, at, who, NOW)
    await session.start()
    await session.on_text("call me tomorrow at half five")
    await settle(session)
    assert ("agent", "I've booked your callback for Thursday 29 October at 5:30 pm.") in turns
    await session.stop()
