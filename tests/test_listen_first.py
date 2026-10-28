"""WP6: outbound calls listen first, check identity, and hang up on answering machines."""

from __future__ import annotations

import asyncio
import math
import random
import struct

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS, make_session, settle, silence_mulaw, tone_mulaw
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.voip.amd import BeepDetector, MachineDetector, is_voicemail_text
from xplogent.voip.audio import pcm16_to_mulaw

GREETING = "Hi, this is Kingsbury's AI assistant, on a recorded line. Am I speaking with Sarah?"


def sine(freq, ms, amp=6000):
    n = 8 * ms
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / 8000))) for i in range(n))


def voiced(ms, f0=130, amp=1500):
    """Speech-like: a fundamental with many harmonics and a little noise."""
    rnd = random.Random(1)
    out = []
    for i in range(8 * ms):
        v = sum(amp / k * math.sin(2 * math.pi * f0 * k * i / 8000) for k in range(1, 25))
        out.append(struct.pack("<h", max(-32000, min(32000, int(v + rnd.randint(-300, 300))))))
    return b"".join(out)


def test_beep_detector():
    b = BeepDetector()
    assert any(b.feed(sine(1000, 20)) for _ in range(15))         # 300 ms at 1 kHz
    for f in (440, 1400):
        b = BeepDetector()
        assert not any(b.feed(sine(f, 20)) for _ in range(25))
    b = BeepDetector()
    speech = voiced(2000)
    assert not any(b.feed(speech[i:i + 320]) for i in range(0, len(speech), 320))
    b = BeepDetector()
    assert not any(b.feed(sine(1000, 20)) for _ in range(10))     # 200 ms: too short


@pytest.mark.parametrize("text", [
    "The person you are calling is not available. Please leave a message after the tone.",
    "Welcome to the Vodafone voicemail service.",
    "Hi, you've reached Sarah. I can't take your call right now.",
    "Sorry, I'm unavailable at the moment, please leave your name and number.",
    "The O2 customer you are calling is not available, please try later.",
])
def test_voicemail_phrases(text):
    assert is_voicemail_text(text)


@pytest.mark.parametrize("text", ["Hello?", "Yes, speaking.", "Who's this?", "Sarah here, go on",
                                  "Hello, who's calling?"])
def test_people_are_not_voicemail(text):
    assert not is_voicemail_text(text)


def test_long_greeting_without_a_pause_is_a_machine():
    frame = b"\x00" * 320                                         # 20 ms
    d = MachineDetector(window_s=6, long_speech_s=4)
    d.start(0.0)
    assert not any(d.frame(frame, False) for _ in range(25))       # 0.5 s of silence
    reasons = [d.frame(frame, True) for _ in range(205)]           # 4.1 s talking
    assert "without a pause" in next(r for r in reasons if r)
    human = MachineDetector()
    human.start(0.0)
    for _ in range(3):
        assert not any(human.frame(frame, True) for _ in range(40))   # "Hello?" 0.8 s
        assert not any(human.frame(frame, False) for _ in range(20))  # pause
    human.conversation()
    assert not any(human.frame(frame, True) for _ in range(300))


# ── the engine ───────────────────────────────────────────────────────────────
def outbound_session(stt_text="Hello?", replies=None, **kw):
    prov = ScriptedProvider(replies or [Message(role=Role.ASSISTANT, content="Great, thanks Sarah.")])
    session, sent, cleared, turns, hung = make_session(
        prov, greeting=GREETING, listen_first=True, hello_wait_s=kw.pop("hello_wait_s", 0.4),
        amd=MachineDetector(window_s=6, long_speech_s=kw.pop("long_speech_s", 4)), **kw)
    session.stt = FakeSTT(stt_text)
    return session, sent, turns, hung


async def wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_waits_for_hello_then_greets():
    session, sent, turns, hung = outbound_session(hello_wait_s=5)
    await session.start()
    await asyncio.sleep(0.2)
    assert not sent and not any(e["kind"] == "greeting" for e in session.timeline)   # silent
    await session.on_audio(tone_mulaw(400))
    await session.on_audio(silence_mulaw(300))                   # "Hello?"
    assert await wait_for(lambda: ("agent", GREETING) in turns)
    assert await wait_for(lambda: ("caller", "Hello?") in turns)
    assert turns.index(("agent", GREETING)) >= 0 and session.brain.opening_words == "Hello?"
    assert session.brain.state.outcome == "" and not hung
    await session.stop()


async def test_greets_after_the_wait_when_nobody_speaks():
    session, sent, turns, _ = outbound_session(hello_wait_s=0.3)
    await session.start()
    assert await wait_for(lambda: ("agent", GREETING) in turns, 2.0)
    kinds = [e["kind"] for e in session.timeline]
    assert kinds.index("listening") < kinds.index("greeting")
    await session.stop()


async def test_voicemail_phrase_hangs_up_silently():
    session, sent, turns, hung = outbound_session("Hi, you've reached Sarah, please leave a message")
    await session.start()
    await session.on_audio(tone_mulaw(600))
    await session.on_audio(silence_mulaw(300))
    assert await wait_for(lambda: bool(hung))
    assert session.brain.state.outcome == "voicemail"
    assert any(e["kind"] == "voicemail" for e in session.timeline)
    await session.stop()


async def test_long_recorded_greeting_hangs_up():
    session, sent, turns, hung = outbound_session(long_speech_s=1.0, hello_wait_s=5)
    await session.start()
    await session.on_audio(tone_mulaw(1300))                    # 1.3 s with no pause
    assert hung and session.brain.state.outcome == "voicemail"
    assert not any(t == ("agent", GREETING) for t in turns)      # nothing said to the machine
    await session.stop()


async def test_beep_hangs_up():
    session, sent, turns, hung = outbound_session(hello_wait_s=5)
    await session.start()
    await session.on_audio(pcm16_to_mulaw(sine(1000, 400)))
    assert hung and "beep" in session.brain.state.summary
    await session.stop()


async def test_human_conversation_stops_the_watch():
    session, sent, turns, hung = outbound_session("Yes, speaking", hello_wait_s=0.2)
    await session.start()
    assert await wait_for(lambda: ("agent", GREETING) in turns)
    await settle(session)
    await session.on_audio(tone_mulaw(300))
    await session.on_audio(silence_mulaw(300))
    assert await wait_for(lambda: any(s == "agent" and "Great" in t for s, t in turns))
    assert session.amd.done and not hung
    await session.stop()


async def test_typed_text_before_the_opening_line_gets_the_line_first():
    session, sent, turns, _ = outbound_session(hello_wait_s=5)
    await session.start()
    await session.on_text("hello?")
    assert await wait_for(lambda: any(s == "agent" and "Great" in t for s, t in turns))
    agent = [t for s, t in turns if s == "agent"]
    assert agent[0] == GREETING
    await session.stop()


# ── build_session wiring ─────────────────────────────────────────────────────
def _build(info, provider=None):
    from xplogent.voip.service import build_session

    cfg = load_config()
    store = Store(cfg.db_path)

    async def send(_b):
        return None
    session, call_id, brain = build_session(cfg, store, info, send_audio=send, stt=FakeSTT(),
                                            tts=FakeTTS(), provider=provider or ScriptedProvider([]),
                                            retrieve=None)
    return cfg, store, session, call_id, brain


BRIEF = {"applicant_id": 123, "sale_id": 45, "sale": {"job_title": "Care Assistant", "postcode_district": "LS6"},
         "candidate": {"first_name": "Sarah"}}


def test_outbound_screens_inbound_unchanged():
    save_user_config({"voip": {"company": "Kingsbury", "template": "kingsbury_outbound_screener"}})
    out = {"direction": "outgoing", "from": "+441130000000", "to": "+447700900123", "transport": "sip",
           "brief": BRIEF}
    _, store, session, _, brain = _build(out)
    assert session.listen_first and session.amd is not None
    assert session.greeting == GREETING
    assert brain.identity_check == "Sarah" and "IDENTITY CHECK" in brain._system([])
    store.close()
    save_user_config({"voip": {"template": "", "greeting": "Thanks for calling {company}."}})
    inbound = {"direction": "incoming", "from": "+447700900123", "to": "+441130000000", "transport": "sip"}
    _, store, session, _, brain = _build(inbound)
    assert not session.listen_first and session.amd is None and not brain.identity_check
    assert "Thanks for calling Kingsbury" in session.greeting
    store.close()
    save_user_config({"voip": {"outbound": {"listen_first": False}, "voicemail": {"detect": False},
                               "recording": {"enabled": False}}})
    _, store, session, _, _ = _build(out)
    assert not session.listen_first and session.amd is None
    assert "recorded" not in session.greeting                   # never claim what isn't true
    store.close()


async def test_wrong_person_hears_no_job_details():
    from xplogent.voip.service import finalize_call

    save_user_config({"voip": {"company": "Kingsbury", "template": "kingsbury_outbound_screener"}})
    wrong = [Message(role=Role.ASSISTANT, content="Sorry to bother you, I'll try another time. Goodbye.",
                     tool_calls=[ToolCall(id="1", name="log_outcome",
                                          arguments={"outcome": "wrong_person", "summary": "not Sarah"}),
                                 ToolCall(id="2", name="end_call", arguments={"exit": "wrong_party"})])]
    out = {"direction": "outgoing", "from": "+441130000000", "to": "+447700900123", "transport": "sip",
           "brief": BRIEF}
    cfg, store, session, call_id, brain = _build(out, ScriptedProvider(wrong))
    spoken = [s async for s in brain.respond("No, this is her mum.")]
    assert all("Care Assistant" not in s and "LS6" not in s for s in [session.greeting, *spoken])
    assert brain.state.outcome == "wrong_person" and brain.state.end
    from xplogent.voip import crm

    payload = crm.build_payload(cfg, store, call_id, {"outcome": brain.state.outcome}, brain, session)
    assert payload["outcome"] == "wrong_person"
    finalize_call(cfg, store, call_id, session, brain, "+447700900123")
    store.close()


async def test_telnyx_machine_event_ends_the_call():
    from xplogent.voip.control import LiveCall
    from xplogent.voip.service import VoipService

    save_user_config({"voip": {"enabled": True}})
    svc = VoipService()
    seen = []

    class S:
        async def machine_detected(self, reason):
            seen.append(reason)
    svc.register(LiveCall(call_id=1, session=S(), brain=None, ref="cc-9"))
    await svc.telnyx.handle_webhook({"data": {"event_type": "call.machine.premium.detection.ended",
                                              "payload": {"call_control_id": "cc-9", "result": "machine"}}})
    await svc.telnyx.handle_webhook({"data": {"event_type": "call.machine.premium.detection.ended",
                                              "payload": {"call_control_id": "cc-9", "result": "human_residence"}}})
    await asyncio.sleep(0.05)
    assert seen == ["Telnyx: machine"]
