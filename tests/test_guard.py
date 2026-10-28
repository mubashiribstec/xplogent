"""WP7: the agent can't invent pay, hours or dates."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import ScriptedProvider
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.voip.brain import CallBrain
from xplogent.voip.guard import SAFE_LINE, Sources, claims, unsupported

BRIEF = {"sale": {"job_title": "Care Assistant", "salary": "£12.60 per hour",
                  "timing": "Days, 3 x 12h shifts incl. alternate weekends", "postcode_district": "LS6"},
         "candidate": {"first_name": "Sarah"}}


@pytest.mark.parametrize("text,kind,value", [
    ("The pay is £15 an hour.", "money", 15.0),
    ("It's twelve pounds sixty an hour.", "money", 12.6),
    ("£12 and 60p an hour", "money", 12.6),
    ("around £25k a year", "money", 25000.0),
    ("It pays 25,000 a year.", "money", 25000.0),
    ("It's 37.5 hours a week.", "hours", 37.5),
    ("You'd work 12-hour shifts.", "hours", 12.0),
    ("Interviews are on the 4th of November.", "dates", (11, 4)),
    ("Could you start December 1st?", "dates", (12, 1)),
])
def test_claims(text, kind, value):
    assert [(c.kind, c.value) for c in claims(text)] == [(kind, value)]


@pytest.mark.parametrize("text", ["I'll call you back in two hours.", "Press 1 for sales.",
                                  "I have two quick questions.", "It takes about 5 minutes.",
                                  "The job is about 2.6 miles from you, in LS6."])
def test_ordinary_talk_is_not_a_claim(text):
    assert claims(text) == []


def test_sources_are_lenient():
    s = Sources()
    s.add("Pay: £12.60 per hour")
    s.add("Shifts: Days, 3 x 12h shifts")
    s.add("callback booked for 2026-11-04T17:30")
    assert not unsupported("It pays £12.60 an hour on 12-hour shifts.", s)
    assert not unsupported("I've booked you for the 4th of November.", s)
    assert [c.text.strip() for c in unsupported("It's £15 an hour.", s)] == ["£15"]


def brain_with(reply_text, *, caller="", tool_calls=None, guard=True, knowledge=None):
    msgs = [Message(role=Role.ASSISTANT, content=reply_text, tool_calls=tool_calls or [])]
    if tool_calls:
        msgs.append(Message(role=Role.ASSISTANT, content="Is there anything else?"))

    async def retrieve(_q):
        return knowledge or []
    b = CallBrain(ScriptedProvider(msgs), brief=BRIEF, retrieve=retrieve)
    if guard:
        b.enable_guard(("money", "hours", "dates"), today=date(2026, 10, 28))
    return b


async def say(brain, caller="What does it pay?"):
    return [s async for s in brain.respond(caller)]


async def test_invented_pay_is_blocked_and_true_pay_spoken():
    b = brain_with("It pays £15 an hour. You'd be working with older residents.")
    spoken = await say(b)
    assert spoken == [SAFE_LINE, "You'd be working with older residents."]
    assert b.guard_blocked[0]["claims"] == ["£15"]
    assert "£15" not in b.history[-1].content and SAFE_LINE in b.history[-1].content   # not remembered
    b = brain_with("It's £12.60 an hour, on 12-hour day shifts.")
    assert await say(b) == ["It's £12.60 an hour, on 12-hour day shifts."]
    assert b.guard_blocked == []


async def test_only_one_safe_line_per_turn():
    b = brain_with("It's £15 an hour. With overtime it's £18 an hour. Shall we carry on?")
    assert await say(b) == [SAFE_LINE, "Shall we carry on?"]
    assert len(b.guard_blocked) == 2


async def test_callers_own_figures_and_knowledge_are_allowed():
    b = brain_with("So you'd be looking for £14 an hour, noted.")
    assert await say(b, "I'd want at least 14 pounds an hour") == ["So you'd be looking for £14 an hour, noted."]
    b = brain_with("Night shifts pay £13.10 an hour.", knowledge=["Night rate: 13.10 per hour"])
    assert await say(b, "Do nights pay more?") == ["Night shifts pay £13.10 an hour."]


async def test_figures_from_a_tool_result_are_allowed():
    b = brain_with("Let me book that.", tool_calls=[ToolCall(id="1", name="book_callback",
                                                              arguments={"when": "2027-01-15 17:30"})])
    await say(b, "Call me on the 15th of January")
    b2 = brain_with("Let me check.", tool_calls=[ToolCall(id="1", name="lookup_customer", arguments={})])
    await say(b2)
    assert b2.guard_blocked == []
    assert any(d == (1, 15) for d in b.sources.dates)


async def test_far_dates_need_a_source_but_the_next_fortnight_is_fine():
    b = brain_with("We could call you on the 30th of October.")
    assert await say(b, "When can you call?") == ["We could call you on the 30th of October."]
    b = brain_with("You'd start on the 1st of March.")
    assert await say(b, "When would I start?") == [SAFE_LINE]


async def test_guard_off_changes_nothing():
    b = brain_with("It pays £15 an hour.", guard=False)
    assert await say(b) == ["It pays £15 an hour."]


def test_build_session_turns_it_on_and_finalize_reports_it():
    from tests.test_voip import FakeSTT, FakeTTS
    from xplogent.core.config import load_config, save_user_config
    from xplogent.memory.store import Store
    from xplogent.voip.service import build_session, finalize_call

    cfg = load_config()
    store = Store(cfg.db_path)

    async def send(_b):
        return None
    info = {"direction": "outgoing", "from": "+44113", "to": "+447700900123", "transport": "sip", "brief": BRIEF}
    session, call_id, brain = build_session(cfg, store, info, send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                                            provider=ScriptedProvider([]), retrieve=None)
    assert brain.guard_kinds == ("money", "hours", "dates")
    brain.guard_blocked.append({"text": "It pays £15 an hour.", "claims": ["£15"]})
    finalize_call(cfg, store, call_id, session, brain, "+447700900123")
    assert "Guard: 1 sentence" in store.get_call(call_id)["summary"]
    assert store.get_call_debug(call_id)["guard_blocked"][0]["claims"] == ["£15"]
    store.close()
    save_user_config({"voip": {"guard": {"enabled": False}}})
    store = Store(load_config().db_path)
    _, _, brain = build_session(load_config(), store, info, send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                                provider=ScriptedProvider([]), retrieve=None)
    assert brain.guard_kinds == ()
    store.close()
