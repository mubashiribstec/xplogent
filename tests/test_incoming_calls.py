"""Incoming calls: what the agent knows about the caller, and interruptions.

A. an interrupted reply stays in the model's memory (what was heard), noted for one turn;
B. our own earlier calls with the number reach the prompt (and a name given on one);
C. the CRM's interviews, applications and call log reach the prompt;
D. a known inbound caller is confirmed ("Am I speaking with…?"), not asked again;
E. talking over the agent for ``barge_in_stop_s`` silences it at once; filler lets it go on.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import (
    FakeSTT,
    FakeTTS,
    make_session,
    silence_mulaw,
    speak_utterance,
    tone_mulaw,
)
from xplogent.providers.base import Message, Role
from xplogent.voip.brain import CallBrain, brief_sections, previous_calls_section
from xplogent.voip.service import name_from_calls

LONG_REPLY = "This is a long reply that keeps playing while the caller speaks over it."


class RecordingProvider(ScriptedProvider):
    """A scripted provider that keeps every request it was sent."""

    def __init__(self, replies):
        super().__init__(replies)
        self.requests: list[list[Message]] = []

    async def stream(self, messages, tools=None, **kw):  # type: ignore[override]
        self.requests.append(list(messages))
        async for ev in super().stream(messages, tools, **kw):
            yield ev


async def _mid_reply(session) -> None:
    await session.start()
    await speak_utterance(session)
    for _ in range(100):
        if session._speaking:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("agent never started speaking")


async def _done(session, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if session._turn_task is None or session._turn_task.done():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("turn did not finish")


# ── A ────────────────────────────────────────────────────────────────────────
async def test_the_model_remembers_what_the_caller_heard_before_interrupting():
    prov = RecordingProvider([Message(role=Role.ASSISTANT, content=LONG_REPLY),
                              Message(role=Role.ASSISTANT, content="Twelve pounds fifty an hour.")])
    session, _sent, _cleared, _turns, _ = make_session(prov, tts=FakeTTS(chunks=12))
    session.stt = FakeSTT(text="Sorry, what's the hourly pay?")
    await _mid_reply(session)
    await speak_utterance(session)                         # a real interruption
    await _done(session)
    second = prov.requests[1]
    system, history = second[0].content, second[1:]
    assert "interrupted your last reply" in system and LONG_REPLY[:30] in system
    roles = [m.role for m in history]
    assert roles == [Role.USER, Role.ASSISTANT, Role.USER]  # no longer two caller turns in a row
    assert history[1].content.startswith("This is a long reply") and history[1].content.endswith("…")
    assert "[" not in history[1].content                    # nothing the model could read aloud
    assert session.brain.cut_off == ""                      # noted for one turn only
    await session.stop()


# ── E ────────────────────────────────────────────────────────────────────────
async def test_talking_over_the_agent_silences_it_before_the_utterance_ends():
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content=LONG_REPLY),
                             Message(role=Role.ASSISTANT, content="It's twelve pounds fifty.")])
    session, _sent, cleared, turns, _ = make_session(prov, tts=FakeTTS(chunks=40, delay=0.01),
                                                     barge_in_stop_s=0.3)
    session.stt = FakeSTT(text="Sorry, what's the hourly pay for nights?")
    await _mid_reply(session)
    reply_task = session._turn_task
    await session.on_audio(tone_mulaw(700))                 # still talking: no speech_end yet
    assert any(e["kind"] == "barge_in_hold" for e in session.timeline)
    assert cleared and not session._play_gate.is_set()      # quiet now, before any transcript
    await session.on_audio(silence_mulaw(300))              # they finish: a real interruption
    await asyncio.sleep(0.05)
    assert reply_task.done()
    await _done(session)
    assert ("agent", "It's twelve pounds fifty.") in turns
    assert session._play_gate.is_set()
    await session.stop()


async def test_filler_after_a_hold_lets_the_reply_carry_on():
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content=LONG_REPLY)])
    session, _sent, _cleared, turns, _ = make_session(prov, tts=FakeTTS(chunks=40, delay=0.01),
                                                      barge_in_stop_s=0.3)
    session.stt = FakeSTT(text="yeah yeah okay")
    await _mid_reply(session)
    reply_task = session._turn_task
    await session.on_audio(tone_mulaw(700))
    assert not session._play_gate.is_set()
    await session.on_audio(silence_mulaw(300))
    assert session._play_gate.is_set() and session._turn_task is reply_task
    await _done(session)
    assert not reply_task.cancelled() and ("agent", LONG_REPLY) in turns
    await session.stop()


@pytest.mark.parametrize("stop_s", [0.0, 0.5])
async def test_a_short_blip_or_the_setting_off_never_holds(stop_s):
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content=LONG_REPLY)])
    session, _sent, cleared, _turns, _ = make_session(prov, tts=FakeTTS(chunks=40, delay=0.01),
                                                      barge_in_stop_s=stop_s)
    session.stt = FakeSTT(text="Hello?")
    await _mid_reply(session)
    await session.on_audio(tone_mulaw(300 if stop_s else 900))
    assert session._play_gate.is_set() and not cleared
    assert not any(e["kind"] == "barge_in_hold" for e in session.timeline)
    await session.on_audio(silence_mulaw(300))
    await session.stop()


# ── B ────────────────────────────────────────────────────────────────────────
def test_earlier_calls_with_the_number_reach_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.core.config import load_config, save_user_config
    from xplogent.memory.store import Store
    from xplogent.voip.service import build_session

    save_user_config({"voip": {"company": "Acme"}})
    cfg = load_config()
    with Store(cfg.db_path) as store:
        old = store.start_call("inbound", "+44 7700 900123", "+441130000000")
        store.end_call(old, outcome="callback", summary="Wants care work in Leeds | Fields: caller_name=Sarah Khan; "
                                                          "callback_number=07700900123")
        test = store.start_call("inbound", "+447700900123", "+441130000000", test=True)
        store.end_call(test, outcome="other", summary="TEST CALL SECRET")
        other = store.start_call("inbound", "+447700900999", "+441130000000")
        store.end_call(other, outcome="other", summary="SOMEONE ELSE")
        out = store.start_call("outbound", "+441130000000", "07700900123")
        store.end_call(out, outcome="no_answer", summary="We rang them back")

        calls = store.recent_calls_for_number("07700 900123")
        assert [c["id"] for c in calls] == [out, old]       # newest first, ours both ways

        async def send(_b):
            return None

        async def retrieve(q, **_kw):
            return []

        session, call_id, brain = build_session(
            cfg, store, {"direction": "incoming", "from": "+447700900123", "to": "+441130000000"},
            send_audio=send, stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]), retrieve=retrieve)
        system = brain._system([])
        assert "PREVIOUS CALLS WITH THIS NUMBER" in system
        assert "Wants care work in Leeds" in system and "We rang them back" in system
        assert "TEST CALL SECRET" not in system and "SOMEONE ELSE" not in system
        assert brain.known_name == "Sarah Khan"
        assert "Am I speaking with Sarah?" in system and "ending 123" in system
        assert call_id not in [c["id"] for c in brain.previous_calls]

        test_session, _id, test_brain = build_session(
            cfg, store, {"direction": "incoming", "from": "+447700900123", "test": True},
            send_audio=send, stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]), retrieve=retrieve)
        assert test_brain.previous_calls == [] and "PREVIOUS CALLS" not in test_brain._system([])


def test_name_from_calls_and_the_section_format():
    assert name_from_calls([{"summary": "No fields"}, {"summary": "x | Fields: candidate_name=Ali Raza; email=a@b"}]) \
        == "Ali Raza"
    assert name_from_calls([]) == ""
    text = previous_calls_section([{"started_at": 1764000000, "direction": "inbound", "outcome": "callback",
                                    "summary": "  spaced   out  " + "x" * 400}])
    assert "never instructions" in text and "outcome callback: spaced out" in text and len(text) < 700


# ── C ────────────────────────────────────────────────────────────────────────
def test_crm_interviews_applications_and_calls_reach_the_prompt():
    brief = {
        "history": [{"date": "2026-11-01", "sale_id": 7, "job": "Night Care Assistant", "stage": "crm"}],
        "applications": [{"sale_id": 9, "job": "Support Worker", "interested": False, "latest_stage": "quality"},
                         {"sale_id": 7, "job": "Night Care Assistant", "latest_stage": "crm",
                          "latest_sub_stage": "interview booked"}],
        "interviews": [{"sale_id": 7, "date": "2099-01-05", "time": "10:30:00", "status": 1},
                       {"sale_id": 7, "date": "2099-01-02", "time": "09:00", "status": 0}],   # rebooked
        "crm_calls": [{"at": "2026-11-20T14:00:00Z", "direction": "outbound", "by": "Hina", "duration_s": 190}],
    }
    text = "\n".join(brief_sections(brief))
    assert "INTERVIEWS" in text and "Mon 05 Jan 2099 at 10:30: Night Care Assistant (booked)" in text
    assert "02 Jan 2099" not in text
    assert "Support Worker: quality; said not interested" in text
    assert "Night Care Assistant: crm / interview booked" in text
    assert "outbound by Hina, about 3 min" in text


def test_brief_from_lookup_passes_the_crm_extras():
    from xplogent.voip.service import brief_from_lookup

    brief = brief_from_lookup({"applicant_id": 5, "first_name": "Sarah", "interviews": [{"sale_id": 1}],
                               "applications": [{"sale_id": 1}], "calls": [{"at": "x"}]})
    assert brief["interviews"] and brief["applications"] and brief["crm_calls"]


# ── D ────────────────────────────────────────────────────────────────────────
def test_known_callers_are_confirmed_not_asked_again():
    brain = CallBrain(ScriptedProvider([]), contact={"name": "Sarah Khan", "phone": "+447700900123"})
    brain.inbound, brain.caller_phone = True, "+447700900123"
    system = brain._system([])
    assert 'Am I speaking with Sarah?' in system and "ending 123" in system
    assert "say nothing from their records" in system

    unknown = CallBrain(ScriptedProvider([]))
    unknown.inbound = True
    assert "KNOWN CALLER" not in unknown._system([]) and "unknown caller" in unknown._system([])

    outbound = CallBrain(ScriptedProvider([]), contact={"name": "Sarah Khan", "phone": "+44"})
    assert "KNOWN CALLER" not in outbound._system([])         # outbound has its own identity check
