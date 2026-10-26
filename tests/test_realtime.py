"""Realtime speech-to-speech mode against a fake OpenAI Realtime server."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS
from xplogent.providers.base import Message, Role
from xplogent.voip.audio import VAD
from xplogent.voip.brain import CallBrain
from xplogent.voip.fields import build_collector
from xplogent.voip.realtime import RealtimeSession

GREETING = "You're speaking with an AI assistant, and this call is recorded."


class FakeRealtime:
    """Records what the client sends; answers each response.create with the next script."""

    def __init__(self, *scripts: list[dict]):
        self.received: list[dict] = []
        self.scripts = list(scripts)
        self.ws = None
        self.server = None

    async def handler(self, ws):
        self.ws = ws
        async for raw in ws:
            ev = json.loads(raw)
            self.received.append(ev)
            if ev["type"] == "response.create" and self.scripts:
                for out in self.scripts.pop(0):
                    await ws.send(json.dumps(out))

    async def push(self, ev: dict) -> None:
        await self.ws.send(json.dumps(ev))

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


def audio(mulaw: bytes, beta: bool = False) -> dict:
    return {"type": "response.audio.delta" if beta else "response.output_audio.delta",
            "delta": base64.b64encode(mulaw).decode()}


def said(text: str) -> dict:
    return {"type": "response.output_audio_transcript.done", "transcript": text}


def heard(text: str) -> dict:
    return {"type": "conversation.item.input_audio_transcription.completed", "transcript": text}


DONE = {"type": "response.done"}


def make(url: str, brain: CallBrain | None = None, **kw):
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
    session = RealtimeSession(
        brain=brain, stt=FakeSTT(), tts=kw.pop("tts", FakeTTS()), send_audio=send_audio,
        clear_audio=clear_audio, hangup=hangup, on_turn=lambda s, t: turns.append((s, t)),
        greeting=GREETING, vad=VAD(threshold=500, silence_ms=100, min_speech_ms=60), realtime=False,
        rt_url=url, rt_api_key="sk-test", connect_timeout_s=2, **kw)
    return session, sent, cleared, turns, hung


async def until(cond, t=3.0):
    for _ in range(int(t / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def test_session_setup_greeting_and_a_full_turn():
    async with FakeRealtime([audio(b"\x10" * 480), said("We open at nine."), DONE]) as rt:
        session, sent, _c, turns, _h = make(rt.url)
        await session.start()
        await until(lambda: rt.sent("session.update"))
        cfg = rt.sent("session.update")[0]["session"]
        assert cfg["audio"]["input"]["format"] == {"type": "audio/pcmu"}
        assert cfg["audio"]["output"]["format"] == {"type": "audio/pcmu"}
        assert cfg["audio"]["input"]["turn_detection"]["create_response"] is False
        names = {t["name"] for t in cfg["tools"]}
        assert {"end_call", "record_field", "search_knowledge"} <= names
        assert "Acme Solar" in cfg["instructions"] and GREETING not in json.dumps(rt.received)
        assert session.tts.spoken[0] == GREETING                   # the disclosure, word for word
        await until(lambda: session._greeting_task.done())
        greeting_bytes = sum(len(f) for f in sent)
        await session.on_audio(b"\x7f" * 160)
        await until(lambda: rt.sent("input_audio_buffer.append"))
        assert base64.b64decode(rt.sent("input_audio_buffer.append")[0]["audio"]) == b"\x7f" * 160
        await rt.push({"type": "input_audio_buffer.speech_stopped"})
        await rt.push(heard("What time do you open?"))
        await until(lambda: ("agent", "We open at nine.") in turns)
        await until(lambda: sum(len(f) for f in sent) >= greeting_bytes + 480)    # the model's audio played
        assert ("caller", "What time do you open?") in turns
        assert session.turn_timings[-1]["llm"] == "realtime:gpt-realtime" and "total_ms" in session.turn_timings[-1]
        assert any(e["kind"] == "realtime" for e in session.timeline)
        await session.stop()


async def test_beta_event_names_are_accepted():
    async with FakeRealtime([audio(b"\x22" * 320, beta=True),
                             {"type": "response.audio_transcript.done", "transcript": "Hello."}, DONE]) as rt:
        session, sent, _c, turns, _h = make(rt.url)
        await session.start()
        await until(lambda: session._greeting_task.done())
        greeting_bytes = sum(len(f) for f in sent)
        await rt.push(heard("hi"))
        await until(lambda: ("agent", "Hello.") in turns)
        await until(lambda: sum(len(f) for f in sent) >= greeting_bytes + 320)
        await session.stop()


async def test_tools_keep_the_field_guard_and_search_knowledge():
    fields = build_collector([{"name": "callback_number", "validator": "phone", "required": True}])
    brain = CallBrain(ScriptedProvider([]), retrieve=None, fields=fields)

    async def retrieve(q):
        return [f"FACT about {q}"]

    brain.retrieve = retrieve
    async with FakeRealtime(
            [{"type": "response.function_call_arguments.done", "name": "end_call", "call_id": "c1",
              "arguments": "{}"}],
            [{"type": "response.function_call_arguments.done", "name": "search_knowledge", "call_id": "c2",
              "arguments": json.dumps({"query": "prices"})}]) as rt:
        session, _s, _c, _t, hung = make(rt.url, brain=brain)
        await session.start()
        await until(lambda: session._greeting_task.done())
        await rt.push(heard("bye"))
        await until(lambda: len(rt.sent("conversation.item.create")) >= 1)
        out = rt.sent("conversation.item.create")[0]["item"]
        assert out["type"] == "function_call_output" and out["call_id"] == "c1"
        assert out["output"].startswith("REFUSED") and not session.finished.is_set() and not hung
        await until(lambda: len(rt.sent("conversation.item.create")) >= 2)
        kb = rt.sent("conversation.item.create")[1]["item"]
        assert kb["call_id"] == "c2" and "FACT about prices" in kb["output"]
        await session.stop()


async def test_transfer_runs_the_normal_hand_off():
    attempts = []

    async def transfer(reason):
        attempts.append(reason)

    brain = CallBrain(ScriptedProvider([]), can_transfer=True)
    async with FakeRealtime([{"type": "response.function_call_arguments.done", "name": "transfer_to_human",
                              "call_id": "t1", "arguments": json.dumps({"reason": "wants a person"})},
                             DONE]) as rt:
        session, *_ = make(rt.url, brain=brain, transfer=transfer)
        await session.start()
        await until(lambda: session._greeting_task.done())
        await rt.push(heard("a human please"))
        await until(lambda: attempts)
        assert session.transfer_pending and any(e["kind"] == "transfer" for e in session.timeline)
        session.transfer_done()
        await session.stop()


async def test_barge_in_cancels_and_clears_but_not_during_the_greeting():
    async with FakeRealtime() as rt:
        session, _s, cleared, _t, _h = make(rt.url, tts=FakeTTS(delay=0.05, chunks=10))
        await session.start()
        await until(lambda: rt.ws is not None)
        await rt.push({"type": "input_audio_buffer.speech_started"})    # over the greeting
        await asyncio.sleep(0.1)
        assert not rt.sent("response.cancel") and not session._greeting_task.done()
        await until(lambda: session._greeting_task.done(), t=5)
        session.realtime = True                          # paced playback: audio stays queued
        for _ in range(20):
            await session._play.put(b"\x30" * 160)
        session._model_speaking = True
        await rt.push({"type": "input_audio_buffer.speech_started"})
        await until(lambda: rt.sent("response.cancel"))
        assert cleared and session._play.empty()
        await session.stop()


async def test_paused_ai_does_not_answer():
    async with FakeRealtime() as rt:
        session, *_ = make(rt.url)
        await session.start()
        await until(lambda: session._greeting_task.done())
        session.brain.paused = True
        await rt.push(heard("hello?"))
        await asyncio.sleep(0.2)
        assert not rt.sent("response.create")
        await session.stop()


async def test_unreachable_realtime_falls_back_to_the_normal_engine():
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="Sure, a quote is free.")])
    session, _sent, _c, turns, _h = make("ws://127.0.0.1:9/v1/realtime", brain=CallBrain(prov))
    await session.start()
    assert session.mode == "cascaded" and any(e["kind"] == "realtime_unavailable" for e in session.timeline)
    from tests.test_voip import settle, speak_utterance

    await settle(session)
    await speak_utterance(session)
    await settle(session)
    assert ("agent", "Sure, a quote is free.") in turns                # FakeSTT -> scripted model -> voice
    await session.stop()


def test_build_session_picks_the_realtime_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from xplogent.core.config import load_config, save_user_config
    from xplogent.memory.store import Store
    from xplogent.voip.readiness import checklist
    from xplogent.voip.service import build_session

    save_user_config({"voip": {"mode": "realtime", "realtime": {"voice": "cedar"}}})
    cfg = load_config()

    async def send(_b):
        return None

    with Store(cfg.db_path) as store:
        session, *_ = build_session(cfg, store, {"direction": "incoming", "from": "+1"}, send_audio=send,
                                    stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]), retrieve=None)
    assert isinstance(session, RealtimeSession) and session.rt_voice == "cedar"
    assert session.rt_url.endswith("?model=gpt-realtime") and session.rt_api_key == "sk-test"
    items = {i["id"]: i for i in checklist(cfg.voip, "ollama:x", env={"OPENAI_API_KEY": "x"})}
    assert items["realtime"]["ok"]
    items = {i["id"]: i for i in checklist(cfg.voip, "ollama:x", env={})}
    assert not items["realtime"]["ok"] and "OpenAI key" in items["realtime"]["detail"]


@pytest.mark.parametrize("mode", ["", "cascaded"])
def test_default_mode_is_the_normal_engine(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.voip.service import _session_class

    cls, kw = _session_class({"mode": mode} if mode else {}, {})
    assert cls.__name__ == "CallSession" and kw == {}
