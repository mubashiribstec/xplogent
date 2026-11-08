"""Latency / cancellation-loop fix (2026-11-07).

The 28 Oct call was 27 s of silence: every "Hello?" while the agent was replying
cancelled the reply and started a fresh turn, so nothing was ever said. These
tests replay that incident plus a real interruption and the greeting rule, all
offline with fake STT/TTS/LLM.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS, make_session, speak_utterance
from xplogent.providers.base import Message, Role
from xplogent.voip.engine import is_filler_utterance, split_greeting_disclosure

LONG_REPLY = "This is a long reply that keeps playing while the caller speaks over it."


async def _wait_done(session, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if session._turn_task is None or session._turn_task.done():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("turn did not finish")


async def _start_long_reply(session) -> None:
    """Drive one caller turn, then wait until the agent is mid-reply."""
    await session.start()
    await speak_utterance(session)
    for _ in range(100):
        if session._speaking:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("agent never started speaking")


# ── filler vs real interruption ───────────────────────────────────────────────
@pytest.mark.parametrize("text,speech_s,expected", [
    ("Hello?", 0.4, True),
    ("hello", 0.3, True),
    ("hmm", 0.5, True),
    ("uh-huh", 0.4, True),
    ("yes", 0.3, True),
    ("yeah, go on", 0.8, True),
    ("hello hello hello", 1.5, True),          # repeated backchannel, however slow
    ("", 0.0, True),                           # unintelligible blip
    ("Sorry, what's the hourly pay?", 1.5, False),
    ("what are your opening hours", 1.2, False),
    ("stop", 0.3, False),
    ("wait a minute", 0.6, False),
    ("hold on", 0.5, False),
    ("what's the pay?", 0.9, False),           # short but a real question
    ("pay?", 0.4, False),
    ("yeah um the pay", 1.5, False),           # 4 words: substantive
])
def test_filler_rule(text, speech_s, expected):
    assert is_filler_utterance(text, speech_s) is expected


def test_greeting_disclosure_split():
    disclosure, rest = split_greeting_disclosure(
        "Hello, thanks for calling Kingsbury. You're speaking with an AI assistant, "
        "and this call is recorded. How can I help you?")
    assert disclosure.endswith("recorded.")
    assert rest == "How can I help you?"
    assert split_greeting_disclosure(
        "You're speaking with an AI assistant, and this call is recorded.") == (
        "You're speaking with an AI assistant, and this call is recorded.", "")
    assert split_greeting_disclosure("Hello there.") == ("Hello there.", "")


@pytest.mark.asyncio
async def test_filler_hello_does_not_restart_reply():
    """Acceptance 1 — the 28 Oct replay: a short "Hello?" mid-reply must NOT
    restart the reply, and no new turn starts for the filler."""
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content=LONG_REPLY)])
    session, sent, cleared, turns, _ = make_session(prov, tts=FakeTTS(chunks=12))
    session.stt = FakeSTT(text="Hello?")
    await _start_long_reply(session)
    reply_task = session._turn_task
    frames_before = len(sent)
    await speak_utterance(session)              # "Hello?" over the reply
    assert not cleared, "filler must not clear the carrier audio buffer"
    assert session._turn_task is reply_task, "filler must not start a new turn"
    assert not reply_task.done() and not reply_task.cancelled()
    assert any(e["kind"] == "filler_ignored" for e in session.timeline)
    await _wait_done(session)
    assert session.stt.calls == 2              # turn 1 + the filler decision; no 2nd turn
    assert len(session.latencies_ms) == 1       # exactly one turn spoke
    assert ("agent", LONG_REPLY) in turns
    assert not any("done" in t for _, t in turns), "no spurious turn for the filler"
    assert len(sent) > frames_before, "the reply kept playing after the filler"
    await session.stop()


@pytest.mark.asyncio
async def test_real_interruption_cancels_and_answers():
    """Acceptance 2 — "Sorry — what's the hourly pay?" mid-reply stops the reply
    promptly and the new question is answered (STT run once, not twice)."""
    prov = ScriptedProvider([
        Message(role=Role.ASSISTANT, content=LONG_REPLY),
        Message(role=Role.ASSISTANT, content="The hourly pay is twelve pounds fifty."),
    ])
    session, sent, cleared, turns, _ = make_session(prov, tts=FakeTTS(chunks=12))
    session.stt = FakeSTT(text="Sorry, what's the hourly pay?")
    await _start_long_reply(session)
    reply_task = session._turn_task
    await speak_utterance(session)              # real interruption over the reply
    assert cleared, "a real interruption must clear the carrier audio buffer"
    await asyncio.sleep(0.05)                   # let the cancelled turn record itself
    assert reply_task.cancelled() or reply_task.done()
    assert any(t.get("cancelled_by") == "barge_in" for t in session.turn_timings), \
        session.turn_timings
    await _wait_done(session)
    assert session.stt.calls == 2, "STT must run once per utterance, not twice"
    assert ("agent", "The hourly pay is twelve pounds fifty.") in turns
    assert ("agent", LONG_REPLY) not in turns, "the cut-off reply must not be recorded whole"
    assert len(session.latencies_ms) == 2       # both turns measured a first word
    await session.stop()


@pytest.mark.asyncio
async def test_greeting_interruptible_after_disclosure():
    """Acceptance 3 — the disclosure plays in full, but the caller may talk over
    the rest of the greeting once those words have been spoken."""
    greeting = ("You're speaking with an AI assistant, and this call is recorded. "
                "How can I help?")
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="We open at nine.")])
    session, sent, cleared, turns, _ = make_session(
        prov, tts=FakeTTS(chunks=10), greeting=greeting)
    await session.start()
    for _ in range(200):                        # wait until the disclosure has played
        if session._greeting_disclosure_done:
            break
        await asyncio.sleep(0.02)
    assert session._greeting_disclosure_done
    assert not session._greeting_task.done(), "the rest of the greeting is still playing"
    await speak_utterance(session)              # caller talks over the tail of the greeting
    await asyncio.sleep(0.05)                   # let the cancellation land
    assert session._greeting_task.cancelled(), "the greeting tail must be cut off"
    assert any(e["kind"] == "greeting_end" and e["detail"] == "stopped"
               for e in session.timeline)
    assert session.tts.spoken[0].endswith("recorded."), "the disclosure was spoken first"
    await _wait_done(session)
    assert any(s == "agent" and "We open at nine." in t for s, t in turns)
    await session.stop()


@pytest.mark.asyncio
async def test_speech_over_disclosure_is_answered_after_it():
    """The disclosure itself is never cut off: speech over it is answered once
    the greeting has finished, as before."""
    greeting = "You're speaking with an AI assistant, and this call is recorded."
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="We open at nine.")])
    session, sent, cleared, turns, _ = make_session(
        prov, tts=FakeTTS(chunks=10), greeting=greeting)
    await session.start()
    await speak_utterance(session)              # caller talks over the disclosure
    assert not cleared
    assert session._greeting_task is not None and not session._greeting_task.cancelled()
    await _wait_done(session)
    assert session._greeting_task.done() and not session._greeting_task.cancelled()
    assert any(s == "agent" and "We open at nine." in t for s, t in turns)
    await session.stop()


# ── a short answer said over the agent's closing question (follow-up, 8 Nov) ─────
class SequenceSTT:
    """Returns the given texts in turn (the last one repeats)."""

    def __init__(self, *texts):
        self.texts, self.calls = list(texts), 0

    async def transcribe(self, pcm):
        self.calls += 1
        return self.texts[min(self.calls, len(self.texts)) - 1]

    async def aclose(self):
        pass


async def _wait_until(pred, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_yes_over_the_closing_question_is_answered_after_it():
    """'Yes' said while the agent is still asking 'Are you free on Monday?' is the
    caller's answer: the question finishes, then the agent answers the 'yes'
    (it used to be dropped as filler, leaving both sides waiting)."""
    prov = ScriptedProvider([
        Message(role=Role.ASSISTANT, content="Thanks for that. Are you free on Monday morning?"),
        Message(role=Role.ASSISTANT, content="Great, I'll book you in for Monday."),
    ])
    session, sent, cleared, turns, _ = make_session(prov, tts=FakeTTS(delay=0.05, chunks=12))
    session.stt = SequenceSTT("I'm interested in the job", "yes")
    await session.start()
    await speak_utterance(session)
    await _wait_until(lambda: len(session.tts.spoken) >= 2 and session._speaking)  # the question plays
    question_task = session._turn_task
    await speak_utterance(session)              # "yes" over the question
    assert not cleared, "the question is not cut off"
    assert any(e["kind"] == "answer_held" for e in session.timeline)
    await _wait_until(lambda: ("agent", "Great, I'll book you in for Monday.") in turns)
    assert question_task.done() and not question_task.cancelled()
    assert ("agent", "Thanks for that. Are you free on Monday morning?") in turns
    assert turns.count(("caller", "yes")) == 1, turns
    assert session.stt.calls == 2               # the held answer is not transcribed twice
    await session.stop()


@pytest.mark.asyncio
async def test_yeah_over_a_statement_is_still_filler():
    """Backchannel over a statement (no question playing) is ignored, as before."""
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content=LONG_REPLY)])
    session, sent, cleared, turns, _ = make_session(prov, tts=FakeTTS(delay=0.05, chunks=12))
    session.stt = SequenceSTT("tell me about the job", "yeah")
    await _start_long_reply(session)
    await speak_utterance(session)
    await _wait_done(session)
    await asyncio.sleep(0.1)
    assert any(e["kind"] == "filler_ignored" for e in session.timeline)
    assert not any(e["kind"] == "answer_held" for e in session.timeline)
    assert len(session.latencies_ms) == 1       # no second turn
    await session.stop()
