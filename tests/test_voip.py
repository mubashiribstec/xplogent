"""Phone calling agent: audio, VAD, brain, contacts, engine (barge-in), and the
webhook -> media WebSocket round trip — all offline with fake STT/TTS/LLM."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct

import pytest

from tests.conftest import ScriptedProvider
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.voip.audio import VAD, mulaw_to_pcm16, pcm16_to_mulaw, resample
from xplogent.voip.brain import CallBrain, split_sentences
from xplogent.voip.contacts import import_csv
from xplogent.voip.engine import CallSession
from xplogent.voip.telephony.telnyx import parse_stream_message, parse_webhook


def tone_pcm(ms: int, amp: int = 8000, rate: int = 8000) -> bytes:
    n = rate * ms // 1000
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / rate))) for i in range(n))


def tone_mulaw(ms: int, amp: int = 8000) -> bytes:
    return pcm16_to_mulaw(tone_pcm(ms, amp))


def silence_mulaw(ms: int) -> bytes:
    return pcm16_to_mulaw(b"\x00\x00" * (8000 * ms // 1000))


# ── audio ────────────────────────────────────────────────────────────────────────
def test_mulaw_roundtrip_is_close():
    pcm = tone_pcm(40)
    back = mulaw_to_pcm16(pcm16_to_mulaw(pcm))
    assert len(back) == len(pcm)
    a = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    b = struct.unpack(f"<{len(back) // 2}h", back)
    # G.711 is lossy but should stay within a few percent of full scale.
    assert max(abs(x - y) for x, y in zip(a, b, strict=True)) < 700


def test_resample_changes_length():
    pcm = tone_pcm(100)
    assert len(resample(pcm, 8000, 16000)) == pytest.approx(len(pcm) * 2, rel=0.05)
    assert resample(pcm, 8000, 8000) == pcm


def test_vad_detects_speech_start_and_end():
    vad = VAD(threshold=500, silence_ms=100, min_speech_ms=60)
    events = []
    frame = tone_pcm(20)
    quiet = b"\x00\x00" * 160
    for _ in range(6):
        events.append(vad.feed(frame))
    for _ in range(8):
        events.append(vad.feed(quiet))
    assert "speech_start" in events and "speech_end" in events
    assert len(vad.take()) > 0 and not vad.in_speech


def test_vad_ignores_short_blip():
    vad = VAD(threshold=500, silence_ms=100, min_speech_ms=200)
    out = [vad.feed(tone_pcm(20))] + [vad.feed(b"\x00\x00" * 160) for _ in range(10)]
    assert "speech_end" not in out


# ── brain ────────────────────────────────────────────────────────────────────────
def test_split_sentences_holds_back_fragment():
    done, rest = split_sentences("We open at nine. Close at five. And on")
    assert done == ["We open at nine.", "Close at five."] and rest.strip() == "And on"
    done, rest = split_sentences("Sure thing", final=True)
    assert done == ["Sure thing"] and rest == ""


class _Capture(ScriptedProvider):
    def __init__(self, replies):
        super().__init__(replies)
        self.seen: list[list[Message]] = []

    async def stream(self, messages, tools=None, **kw):  # type: ignore[override]
        self.seen.append(list(messages))
        async for ev in super().stream(messages, tools, **kw):
            yield ev


@pytest.mark.asyncio
async def test_brain_grounds_prompt_in_knowledge_and_contact():
    prov = _Capture([Message(role=Role.ASSISTANT, content="We open at nine o'clock every day.")])

    async def retrieve(q):
        return ["Opening hours: 9am to 5pm Monday to Friday."]

    brain = CallBrain(prov, contact={"name": "Ann", "phone": "+15550102000", "status": "new",
                                     "fields": {"plan": "gold"}}, retrieve=retrieve)
    spoken = [s async for s in brain.respond("when do you open?")]
    assert spoken == ["We open at nine o'clock every day."]
    system = prov.seen[0][0].content
    assert "Opening hours: 9am to 5pm" in system
    assert "Ann" in system and "plan: gold" in system
    assert "ONLY from the KNOWLEDGE" in system


@pytest.mark.asyncio
async def test_brain_tools_set_state_and_continue():
    transfer = Message(role=Role.ASSISTANT, content="", tool_calls=[
        ToolCall(id="1", name="log_outcome", arguments={"outcome": "do_not_call", "summary": "asked to stop"}),
        ToolCall(id="2", name="transfer_to_human", arguments={"reason": "angry"}),
    ])
    final = Message(role=Role.ASSISTANT, content="I'm sorry about that. Connecting you now.")
    brain = CallBrain(ScriptedProvider([transfer, final]))
    spoken = [s async for s in brain.respond("stop calling me")]
    assert spoken == ["I'm sorry about that.", "Connecting you now."]
    st = brain.state
    assert st.do_not_call and st.transfer and st.outcome == "do_not_call"


@pytest.mark.asyncio
async def test_brain_survives_retrieval_failure():
    async def boom(q):
        raise RuntimeError("index down")

    brain = CallBrain(ScriptedProvider([Message(role=Role.ASSISTANT, content="Let me arrange a callback.")]),
                      retrieve=boom)
    assert [s async for s in brain.respond("price?")] == ["Let me arrange a callback."]


# ── contacts ─────────────────────────────────────────────────────────────────────
def test_csv_import_maps_headers_custom_fields_dnc_and_dedupes(tmp_path):
    store = Store(tmp_path / "v.db")
    text = ("First Name,Last Name,Mobile,Email,Plan,DNC\n"
            "Ann,Lee,+1 (555) 010-2000,ann@x.com,gold,\n"
            "Bob,Ray,555-010-3000,,silver,yes\n"
            "Ann,Dup,5550102000,,gold,\n"
            "Bad,Row,12,,,\n")
    res = import_csv(store, text)
    assert res["imported"] == 2 and res["skipped"] == 2 and res["dnc"] == 1
    ann = store.find_contact_by_phone("(555) 010-2000")
    assert ann["name"] == "Ann Lee" and ann["fields"] == {"Plan": "gold"}
    assert store.is_dnc("555-010-3000")
    store.close()


def test_csv_without_phone_column_is_rejected(tmp_path):
    store = Store(tmp_path / "v.db")
    assert not import_csv(store, "name,email\nA,a@x.com\n")["ok"]
    store.close()


# ── telephony parsing ──────────────────────────────────────────────────────────────
def test_parse_stream_and_webhook():
    media = json.dumps({"event": "media", "media": {"payload": base64.b64encode(b"\x01\x02").decode(),
                                                     "track": "inbound"}})
    assert parse_stream_message(media)["audio"] == b"\x01\x02"
    outbound = json.dumps({"event": "media", "media": {"payload": "AAA=", "track": "outbound"}})
    assert parse_stream_message(outbound)["event"] == "other"  # never echo our own audio
    assert parse_stream_message(json.dumps({"event": "start", "start": {"streamSid": "S1"}}))["stream_id"] == "S1"
    assert parse_stream_message("not json")["event"] == "other"
    hook = parse_webhook({"data": {"event_type": "call.initiated", "payload": {
        "call_control_id": "cc1", "from": "+15550102000", "to": "+15559990000", "direction": "incoming"}}})
    assert hook["event"] == "call.initiated" and hook["call_control_id"] == "cc1"


# ── engine ─────────────────────────────────────────────────────────────────────────
class FakeSTT:
    def __init__(self, text="what are your opening hours"):
        self.text, self.calls = text, 0

    async def transcribe(self, pcm):
        self.calls += 1
        return self.text

    async def aclose(self):
        pass


class FakeTTS:
    sample_rate = 24000

    def __init__(self, delay=0.0, chunks=3):
        self.delay, self.chunks, self.spoken = delay, chunks, []

    async def synthesize(self, text):
        self.spoken.append(text)
        for _ in range(self.chunks):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield b"\x10\x20" * 2400  # 100 ms @ 24 kHz

    async def aclose(self):
        pass


def make_session(provider, tts=None, **kw):
    sent: list[bytes] = []
    cleared: list[int] = []
    turns: list[tuple[str, str]] = []
    hung: list[int] = []

    async def send_audio(b):
        sent.append(b)

    async def clear_audio():
        cleared.append(1)

    async def hangup():
        hung.append(1)

    brain = CallBrain(provider)
    session = CallSession(
        brain=brain, stt=FakeSTT(), tts=tts or FakeTTS(), send_audio=send_audio,
        clear_audio=clear_audio, hangup=hangup, on_turn=lambda s, t: turns.append((s, t)),
        vad=VAD(threshold=500, silence_ms=100, min_speech_ms=60), **kw)
    return session, sent, cleared, turns, hung


async def speak_utterance(session, ms=300):
    await session.on_audio(tone_mulaw(ms))
    await session.on_audio(silence_mulaw(300))


async def settle(session):
    for _ in range(100):
        if session._turn_task is None or session._turn_task.done():
            return
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_engine_full_turn_records_and_speaks():
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="We open at nine. Anything else I can help with?")])
    session, sent, _, turns, _ = make_session(prov, greeting="Hello there, this is the AI assistant.")
    await session.start()
    await settle(session)
    await speak_utterance(session)
    await settle(session)
    assert ("agent", "Hello there, this is the AI assistant.") in turns
    assert ("caller", "what are your opening hours") in turns
    assert any(s == "agent" and "We open at nine." in t for s, t in turns)
    assert sent and all(len(c) <= 800 for c in sent)          # mu-law chunks went out
    assert len(session.latencies_ms) == 1
    await session.stop()


@pytest.mark.asyncio
async def test_engine_barge_in_cancels_speech_and_clears_buffer():
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="This is a long reply that will be cut off.")])
    session, sent, cleared, _, _ = make_session(prov, tts=FakeTTS(delay=0.15, chunks=10))
    await speak_utterance(session)
    for _ in range(60):                       # wait until the agent is mid-sentence
        if session._speaking:
            break
        await asyncio.sleep(0.02)
    assert session._speaking
    await session.on_audio(tone_mulaw(200))   # caller talks over the agent
    assert cleared, "carrier audio buffer must be cleared on barge-in"
    assert not session._speaking
    await session.stop()


@pytest.mark.asyncio
async def test_engine_hangs_up_after_end_call_tool():
    bye = Message(role=Role.ASSISTANT, content="", tool_calls=[
        ToolCall(id="1", name="log_outcome", arguments={"outcome": "resolved"}),
        ToolCall(id="2", name="end_call", arguments={})])
    prov = ScriptedProvider([bye, Message(role=Role.ASSISTANT, content="Thanks for calling. Goodbye now!")])
    session, _, _, turns, hung = make_session(prov)
    await speak_utterance(session)
    await settle(session)
    assert hung and session.finished.is_set()
    assert session.brain.state.outcome == "resolved"
    await session.stop()


@pytest.mark.asyncio
async def test_engine_ignores_empty_transcripts():
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="should not be used")])
    session, sent, _, turns, _ = make_session(prov)
    session.stt = FakeSTT(text=" ")
    await speak_utterance(session)
    await settle(session)
    assert not turns and not sent
    await session.stop()


# ── webhook -> media socket round trip ────────────────────────────────────────────────
def test_webhook_to_media_socket_end_to_end(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import xplogent.providers.registry as registry
    import xplogent.voip.service as service
    from xplogent.core.config import load_config, save_user_config
    from xplogent.interfaces.api.server import create_app

    save_user_config({"voip": {"enabled": True, "webhook_secret": "s3cret",
                               "public_url": "https://agent.test", "cost_per_minute": 0.6,
                               "vad": {"threshold": 500, "silence_ms": 100}}})

    actions: list[tuple[str, str]] = []

    class FakeTelnyx:
        def __init__(self, *a, **k):
            pass

        async def answer(self, cc):
            actions.append(("answer", cc))

        async def start_stream(self, cc, url):
            actions.append(("stream", url))

        async def hangup(self, cc):
            actions.append(("hangup", cc))

        async def transfer(self, cc, to):
            actions.append(("transfer", to))

        async def aclose(self):
            pass

    monkeypatch.setattr(service, "TelnyxClient", FakeTelnyx)
    monkeypatch.setattr(service, "build_stt", lambda cfg: FakeSTT())
    monkeypatch.setattr(service, "build_tts", lambda cfg: FakeTTS())
    monkeypatch.setattr(service, "_make_retriever", lambda *a, **k: None)
    monkeypatch.setattr(registry, "build_provider", lambda spec, **k: ScriptedProvider(
        [Message(role=Role.ASSISTANT, content="We open at nine. Anything else I can help with?")]))
    service._SERVICE = None  # fresh singleton

    cfg = load_config()
    store = Store(cfg.db_path)
    store.add_contact("Ann Lee", "+15550102000")
    store.close()

    c = TestClient(create_app())

    # wrong secret is rejected
    assert c.post("/telephony/webhook/nope", json={}).status_code == 403

    hook = lambda ev: {"data": {"event_type": ev, "payload": {   # noqa: E731
        "call_control_id": "cc-1", "from": "+1 555 010 2000", "to": "+15559990000", "direction": "incoming"}}}
    assert c.post("/telephony/webhook/s3cret", json=hook("call.initiated")).json()["ok"]
    assert c.post("/telephony/webhook/s3cret", json=hook("call.answered")).json()["ok"]

    # let the fire-and-forget carrier actions run, then find the stream URL we gave Telnyx
    import time
    for _ in range(50):
        if any(a[0] == "stream" for a in actions):
            break
        time.sleep(0.05)
    assert ("answer", "cc-1") in actions
    stream_url = next(u for a, u in actions if a == "stream")
    assert stream_url.startswith("wss://agent.test/ws/telephony/")
    token = stream_url.rsplit("/", 1)[1]

    # an unknown token is refused
    with pytest.raises(Exception):  # noqa: B017
        with c.websocket_connect("/ws/telephony/bogus"):
            pass

    with c.websocket_connect(f"/ws/telephony/{token}") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "S1"}}))
        greeting = json.loads(ws.receive_text())               # agent speaks first
        assert set(greeting) == {"event", "media"} and set(greeting["media"]) == {"payload"}
        for ms_audio in (tone_mulaw(300), silence_mulaw(300)):
            ws.send_text(json.dumps({"event": "media", "media": {
                "payload": base64.b64encode(ms_audio).decode(), "track": "inbound"}}))
        # drain until the reply audio for the caller's question has been sent
        for _ in range(40):
            store = Store(cfg.db_path)
            turns = [t["text"] for t in (store.get_call(1) or {}).get("turns", [])]
            store.close()
            if any("We open at nine" in t for t in turns):
                break
            time.sleep(0.1)
        ws.send_text(json.dumps({"event": "stop"}))

    # wrap-up record
    for _ in range(40):
        store = Store(cfg.db_path)
        call = store.get_call(1)
        store.close()
        if call and call["status"] == "ended":
            break
        time.sleep(0.1)
    assert call["status"] == "ended" and call["direction"] == "inbound"
    assert call["contact_name"] == "Ann Lee"                   # caller-ID matched the contact
    assert any(t["speaker"] == "caller" for t in call["turns"])
    assert call["outcome"] == "abandoned" and call["cost"] > 0


# ── Python 3.13 has no stdlib audioop: the pure-Python fallback must behave the same ──
def test_audio_fallback_matches_audioop(monkeypatch):
    from xplogent.voip import audio

    pcm = tone_pcm(60)
    with_audioop = (audio.pcm16_to_mulaw(pcm), audio.mulaw_to_pcm16(audio.pcm16_to_mulaw(pcm)),
                    audio.rms(pcm), len(audio.resample(pcm, 8000, 16000)))
    monkeypatch.setattr(audio, "_audioop", None)           # force the fallback code paths
    mu = audio.pcm16_to_mulaw(pcm)
    back = audio.mulaw_to_pcm16(mu)
    assert mu == with_audioop[0]                           # identical G.711 encoding
    assert back == with_audioop[1]                         # identical decoding
    assert audio.rms(pcm) == pytest.approx(with_audioop[2], rel=0.01)
    assert len(audio.resample(pcm, 8000, 16000)) == pytest.approx(with_audioop[3], rel=0.02)
    # the VAD runs on the fallback too
    vad = audio.VAD(threshold=500, silence_ms=100, min_speech_ms=60)
    events = [vad.feed(tone_pcm(20)) for _ in range(6)] + [vad.feed(b"\x00\x00" * 160) for _ in range(8)]
    assert "speech_start" in events and "speech_end" in events


def test_empty_speech_config_falls_back_to_defaults():
    # default.yaml ships `base_url: ""`; that must mean "use the OpenAI endpoint", not "no URL".
    from xplogent.voip.providers import OpenAICompatSTT, OpenAICompatTTS, build_stt, build_tts

    stt = build_stt({"provider": "openai-compatible", "base_url": "", "model": ""})
    tts = build_tts({"base_url": "", "model": "", "voice": ""})
    assert isinstance(stt, OpenAICompatSTT) and str(stt._client.base_url).startswith("https://api.openai.com")
    assert isinstance(tts, OpenAICompatTTS) and str(tts._client.base_url).startswith("https://api.openai.com")
    assert stt.model == "whisper-1" and tts.model == "tts-1" and tts.voice == "alloy"
