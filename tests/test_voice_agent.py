"""Deepgram Voice Agent mode (``voip.mode: voice_agent``) against a fake Deepgram agent server."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS
from xplogent.voip.audio import VAD
from xplogent.voip.brain import CallBrain
from xplogent.voip.fields import build_collector
from xplogent.voip.voice_agent import DeepgramAgentSession, probe, think_models

GREETING = "You're speaking with an AI assistant, and this call is recorded."


class FakeAgent:
    """Welcome → (Settings) → SettingsApplied; records what the client sends; on each
    InjectUserMessage plays the next script (text events and binary audio)."""

    def __init__(self, *scripts: list, refuse: str = ""):
        self.received: list[dict] = []
        self.audio_in = 0
        self.scripts = list(scripts)
        self.refuse = refuse
        self.ws = None
        self.auth = ""

    async def handler(self, ws):
        self.ws = ws
        self.auth = ws.request.headers.get("Authorization", "")
        await ws.send(json.dumps({"type": "Welcome", "request_id": "r1"}))
        async for raw in ws:
            if isinstance(raw, bytes):
                self.audio_in += len(raw)
                continue
            ev = json.loads(raw)
            self.received.append(ev)
            if ev["type"] == "Settings":
                if self.refuse:
                    await ws.send(json.dumps({"type": "Error", "description": self.refuse, "code": "BAD"}))
                    continue
                await ws.send(json.dumps({"type": "SettingsApplied"}))
            elif ev["type"] == "InjectUserMessage" and self.scripts:
                await self.play(self.scripts.pop(0))

    async def play(self, script: list) -> None:
        for out in script:
            await self.ws.send(out if isinstance(out, bytes) else json.dumps(out))

    def sent(self, kind: str) -> list[dict]:
        return [e for e in self.received if e["type"] == kind]

    async def __aenter__(self):
        from websockets.asyncio.server import serve

        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


def text(role: str, content: str) -> dict:
    return {"type": "ConversationText", "role": role, "content": content}


DONE = {"type": "AgentAudioDone"}
VA = {"listen_model": "nova-3", "think": {"provider": "open_ai", "model": "gpt-4o-mini", "temperature": 0.6},
      "voice_provider": "deepgram", "voice": "aura-2-draco-en"}


def make(url: str, brain: CallBrain | None = None, va: dict | None = None, **kw):
    sent, cleared, turns, hung = [], [], [], []

    async def send_audio(b):
        sent.append(b)

    async def clear_audio():
        cleared.append(1)

    async def hangup():
        hung.append(1)

    async def retrieve(q):
        return [f"FACT about {q}"]

    brain = brain or CallBrain(ScriptedProvider([]), persona="You work for Acme Solar.", retrieve=retrieve)
    tts = FakeTTS()
    session = DeepgramAgentSession(
        brain=brain, stt=FakeSTT(), tts=tts, send_audio=send_audio,
        clear_audio=clear_audio, hangup=hangup, on_turn=lambda s, t: turns.append((s, t)),
        greeting=GREETING, vad=VAD(threshold=500, silence_ms=100, min_speech_ms=60), realtime=False,
        va_url=url, va_api_key="dg-test", va_settings=va or VA, connect_timeout_s=2, **kw)
    return session, sent, cleared, turns, hung


async def until(cond, t=3.0):
    for _ in range(int(t / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def test_settings_and_a_full_turn():
    async with FakeAgent([text("assistant", "Our panels start at four thousand pounds."), b"\xff" * 480, DONE]) as fake:
        session, sent, _c, turns, _h = make(fake.url)
        await session.start()
        assert session.mode == "voice_agent" and fake.auth == "Token dg-test"
        settings = fake.sent("Settings")[0]
        assert settings["audio"] == {"input": {"encoding": "mulaw", "sample_rate": 8000},
                                     "output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"}}
        agent = settings["agent"]
        assert agent["listen"] == {"provider": {"type": "deepgram", "model": "nova-3"}}
        assert agent["think"]["provider"] == {"type": "open_ai", "model": "gpt-4o-mini", "temperature": 0.6}
        assert "Acme Solar" in agent["think"]["prompt"] and "do not greet again" in agent["think"]["prompt"]
        names = {f["name"] for f in agent["think"]["functions"]}
        assert {"book_callback", "end_call", "search_knowledge", "log_outcome"} <= names
        assert agent["speak"] == {"provider": {"type": "deepgram", "model": "aura-2-draco-en"}}
        assert agent["greeting"] == GREETING
        assert turns[0] == ("agent", GREETING)                 # the greeting is recorded word for word
        assert not session.tts.spoken                          # and spoken by the agent, not our voice

        await fake.play([text("assistant", GREETING), b"\xff" * 160, DONE])     # Deepgram's greeting
        await until(lambda: not session._greeting_live)
        await session.on_audio(b"\x7f" * 160)                  # the caller's audio goes straight in
        await until(lambda: fake.audio_in == 160)
        await session._reply("How much are the panels?")
        await until(lambda: ("agent", "Our panels start at four thousand pounds.") in turns)
        await until(lambda: len(sent) == 4)                    # greeting 1 frame + reply 3 frames
        assert [t for t in turns if t == ("agent", GREETING)] == [("agent", GREETING)]
        assert fake.sent("InjectUserMessage")[0]["content"] == "How much are the panels?"
        assert session.turn_timings and session.turn_timings[-1]["llm"] == "voice_agent:open_ai:gpt-4o-mini"
        await session.stop()


async def test_greeting_text_is_not_recorded_twice_and_is_not_cut_off():
    async with FakeAgent() as fake:
        session, sent, cleared, turns, _h = make(fake.url)
        await session.start()
        await until(lambda: fake.ws is not None and fake.sent("Settings"))
        await fake.play([text("assistant", GREETING), b"\xff" * 320, {"type": "UserStartedSpeaking"}])
        await until(lambda: len(sent) == 2)
        assert not cleared                                     # speech over the disclosure doesn't cut it
        await fake.play([DONE])
        await asyncio.sleep(0.1)
        assert [t for t in turns if t == ("agent", GREETING)] == [("agent", GREETING)]
        await fake.play([b"\xff" * 4800, {"type": "UserStartedSpeaking"}])   # after the greeting: barge-in
        await until(lambda: bool(cleared))
        assert any(e["kind"] == "barge_in" for e in session.timeline)
        await session.stop()


async def test_function_calls_run_through_the_brain_and_its_guard():
    brain = CallBrain(ScriptedProvider([]), persona="x", fields=build_collector([{"name": "postcode", "required": True}]))
    async with FakeAgent() as fake:
        session, _s, _c, _t, hung = make(fake.url, brain=brain)
        await session.start()
        await until(lambda: fake.sent("Settings"))
        await fake.play([{"type": "FunctionCallRequest", "functions": [
            {"id": "f1", "name": "end_call", "arguments": "{}", "client_side": True}]}])
        await until(lambda: fake.sent("FunctionCallResponse"))
        reply = fake.sent("FunctionCallResponse")[0]
        assert reply["id"] == "f1" and reply["name"] == "end_call" and "postcode" in reply["content"]
        assert not brain.state.end                             # refused: a required field is missing
        await fake.play([{"type": "FunctionCallRequest", "functions": [
            {"id": "f2", "name": "record_field", "arguments": json.dumps({"name": "postcode", "value": "LS6 2AB"})},
            {"id": "f3", "name": "end_call", "arguments": "{}"}]}, DONE])
        await until(lambda: bool(hung))                         # ended after the agent's audio was done
        assert brain.state.end
        await session.stop()


async def test_pause_ai_stops_feeding_the_agent():
    async with FakeAgent() as fake:
        session, *_ = make(fake.url)
        await session.start()
        session.brain.paused = True
        await session.on_audio(b"\x7f" * 160)
        await asyncio.sleep(0.1)
        assert fake.audio_in == 0
        await session.stop()


async def test_falls_back_to_the_standard_mode():
    session, sent, _c, turns, _h = make("ws://127.0.0.1:9")
    await session.start()
    assert session.mode == "cascaded" and session._ws is None
    assert any(e["kind"] == "voice_agent_unavailable" for e in session.timeline)
    await until(lambda: session.tts.spoken)                    # the greeting with our own voice
    await session.stop()
    async with FakeAgent(refuse="unsupported model") as fake:  # settings refused: same fallback
        session, *_ = make(fake.url)
        await session.start()
        assert session.mode == "cascaded"
        assert "unsupported model" in next(e["detail"] for e in session.timeline
                                           if e["kind"] == "voice_agent_unavailable")
        await session.stop()


async def test_a_cartesia_voice_goes_through_deepgram(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "car-key")
    va = {**VA, "voice_provider": "cartesia", "cartesia_voice": "62ae83ad-4f6a-430b-af41-a9bede9286ca"}
    async with FakeAgent() as fake:
        session, *_ = make(fake.url, va=va)
        await session.start()
        speak = fake.sent("Settings")[0]["agent"]["speak"]
        assert speak["provider"]["type"] == "cartesia" and speak["provider"]["model_id"] == "sonic-3.6"
        assert speak["provider"]["voice"] == {"mode": "id", "id": "62ae83ad-4f6a-430b-af41-a9bede9286ca"}
        assert speak["endpoint"]["headers"]["x-api-key"] == "car-key"
        await session.stop()


async def test_probe_reports_times_and_errors():
    async with FakeAgent([text("assistant", "Hello back!"), b"\xff" * 1600, DONE]) as fake:
        out = await probe(VA, "dg-test", {}, url=fake.url, timeout_s=5)
    assert out["ok"] and out["reply"] == "Hello back!" and out["audio_seconds"] == 0.2
    assert out["connect_ms"] >= 0 and out["first_audio_ms"] >= 0
    async with FakeAgent(refuse="invalid voice") as fake:
        out = await probe(VA, "dg-test", {}, url=fake.url, timeout_s=5)
    assert not out["ok"] and out["error"] == "invalid voice"
    assert "no Deepgram key" in (await probe(VA, "", {}))["error"]


async def test_think_models_live_list_or_built_in():
    def handler(request):
        assert request.headers["authorization"] == "Token k"
        return httpx.Response(200, json={"models": [{"provider": "open_ai", "id": "gpt-4.1-mini", "name": "GPT-4.1 mini"},
                                                    {"provider": "groq", "id": "x"}]})

    live = await think_models("k", transport=httpx.MockTransport(handler))
    assert live == [{"provider": "open_ai", "model": "gpt-4.1-mini", "name": "GPT-4.1 mini"}]   # hosted only
    fallback = await think_models("k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert fallback[0]["model"] == "gpt-4o-mini" and len(fallback) > 5


def test_build_session_readiness_and_api(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    from xplogent.core.config import load_config, save_user_config
    from xplogent.memory.store import Store
    from xplogent.voip.readiness import checklist
    from xplogent.voip.service import build_session

    save_user_config({"voip": {"mode": "voice_agent", "voice_agent": {"voice": "aura-2-pandora-en",
                                                                     "think": {"provider": "anthropic",
                                                                               "model": "claude-haiku-4-5"}}}})
    cfg = load_config()

    async def send(_b):
        return None

    with Store(cfg.db_path) as store:
        session, *_ = build_session(cfg, store, {"direction": "outgoing", "to": "+44", "from": "+1",
                                                 "brief": {"candidate": {"first_name": "Sarah"}}},
                                    send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                                    provider=ScriptedProvider([]), retrieve=None)
    assert isinstance(session, DeepgramAgentSession) and session.rt_api_key == "dg-key"
    assert session.rt_model == "anthropic:claude-haiku-4-5"
    assert session.amd is None and not session.listen_first   # the engine itself listens
    assert "Sarah" in session.greeting                          # the identity check is kept
    items = {i["id"]: i for i in checklist(cfg.voip, "ollama:x", env={"DEEPGRAM_API_KEY": "x"})}
    assert items["voice_agent"]["ok"] and "claude-haiku-4-5" in items["voice_agent"]["detail"]
    items = {i["id"]: i for i in checklist(cfg.voip, "ollama:x", env={})}
    assert not items["voice_agent"]["ok"] and "Deepgram key" in items["voice_agent"]["detail"]

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    client = TestClient(create_app())
    voices = client.get("/voip/voice-agent/voices").json()["voices"]
    assert voices[0]["id"] == "aura-2-pandora-en"
