"""Speech upgrade: streaming STT (with no lost words), new TTS providers, stage timings."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS, make_session, settle, speak_utterance
from xplogent.providers.base import Message, Role
from xplogent.voip import providers as sp


# ── streaming speech-to-text ─────────────────────────────────────────────────────
class FakeStream:
    def __init__(self, text="", fail=False):
        self.text, self.fail, self.fed, self.started, self.closed = text, fail, [], False, False

    def start(self):
        self.started = True

    def feed(self, pcm):
        self.fed.append(pcm)

    async def finish(self):
        if self.fail:
            raise RuntimeError("socket closed")
        return self.text

    async def aclose(self):
        self.closed = True


class FakeStreamingSTT:
    label = "fake-stream"

    def __init__(self, **kw):
        self.kw, self.streams = kw, []

    def new_stream(self):
        self.streams.append(FakeStream(**self.kw))
        return self.streams[-1]


async def test_streamed_transcript_skips_the_batch_call():
    stream_stt = FakeStreamingSTT(text="do you open on sundays")
    batch = FakeSTT("SHOULD NOT BE USED")
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="Yes, from ten.")])
    session, _sent, _c, turns, _h = make_session(prov, stream_stt=stream_stt)
    session.stt = batch
    await speak_utterance(session)
    await settle(session)
    stream = stream_stt.streams[0]
    assert stream.started and sum(len(f) for f in stream.fed) > 0 and stream.closed
    assert ("caller", "do you open on sundays") in turns and batch.calls == 0
    t = session.turn_timings[-1]
    assert t["stt"] == "fake-stream" and t["fallbacks"] == [] and t["error"] == ""
    await session.stop()


async def test_failed_stream_falls_back_to_the_full_recording():
    """No lost words: the batch path gets the whole buffered utterance."""
    seen = []

    class RecordingSTT(FakeSTT):
        async def transcribe(self, pcm):
            seen.append(len(pcm))
            return await super().transcribe(pcm)

    stream_stt = FakeStreamingSTT(fail=True)
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="Sure.")])
    session, _sent, _c, turns, _h = make_session(prov, stream_stt=stream_stt)
    session.stt = RecordingSTT("i want a quote")
    await speak_utterance(session, ms=300)
    await settle(session)
    assert ("caller", "i want a quote") in turns
    # 300 ms of speech + trailing silence at 16 kHz 16-bit: the whole utterance, not a tail.
    assert seen and seen[0] >= 300 * 32
    t = session.turn_timings[-1]
    assert "stt:stream->batch" in t["fallbacks"] and "socket closed" in t["error"]
    await session.stop()


async def test_cough_drops_the_stream():
    from xplogent.voip.audio import VAD

    stream_stt = FakeStreamingSTT(text="x")
    session, *_ = make_session(ScriptedProvider([]), stream_stt=stream_stt)
    session.vad = VAD(threshold=500, silence_ms=100, min_speech_ms=60)
    from tests.test_voip import silence_mulaw, tone_mulaw

    await session.on_audio(tone_mulaw(80) + silence_mulaw(200))     # opens, then resets: a click
    await asyncio.sleep(0)
    assert session._stream is None and stream_stt.streams and stream_stt.streams[0].closed
    await session.stop()


async def test_deepgram_stream_against_a_fake_server():
    from websockets.asyncio.server import serve

    got = {"audio": 0, "auth": ""}

    async def handler(ws):
        got["auth"] = ws.request.headers.get("Authorization", "")
        got["path"] = ws.request.path
        async for msg in ws:
            if isinstance(msg, bytes):
                got["audio"] += len(msg)
                continue
            if json.loads(msg).get("type") == "Finalize":
                for text, fin in (("hello there", False), ("hello there", True)):
                    await ws.send(json.dumps({"type": "Results", "is_final": fin,
                                              "channel": {"alternatives": [{"transcript": text}]}}))
                await ws.send(json.dumps({"type": "Results", "is_final": True, "from_finalize": True,
                                          "channel": {"alternatives": [{"transcript": "how are you"}]}}))
            elif json.loads(msg).get("type") == "CloseStream":
                return

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        dg = sp.DeepgramStreamingSTT(model="nova-3", api_key="dg-key", ws_url=f"ws://127.0.0.1:{port}/v1/listen")
        stream = dg.new_stream()
        stream.start()
        for _ in range(10):
            stream.feed(b"\x01\x00" * 160)
        assert await stream.finish() == "hello there how are you"
        await stream.aclose()
    assert got["audio"] == 10 * 320 and got["auth"] == "Token dg-key"
    assert "encoding=linear16" in got["path"] and "sample_rate=8000" in got["path"]


async def test_deepgram_stream_failure_raises_for_the_fallback():
    stream = sp.DeepgramStreamingSTT(api_key="k", ws_url="ws://127.0.0.1:9/v1/listen").new_stream()
    stream.start()
    stream.feed(b"\x00\x00" * 160)
    with pytest.raises(RuntimeError, match="streaming speech-to-text failed"):
        await stream.finish()
    await stream.aclose()


async def test_deepgram_batch():
    def handler(request):
        assert request.url.path == "/v1/listen" and request.headers["authorization"] == "Token k"
        assert request.url.params["model"] == "nova-3" and request.content[:4] == b"RIFF"
        return httpx.Response(200, json={"results": {"channels": [{"alternatives": [{"transcript": " hi "}]}]}})

    stt = sp.DeepgramSTT(api_key="k")
    stt._client = httpx.AsyncClient(base_url="https://api.deepgram.com", headers={"Authorization": "Token k"},
                                    transport=httpx.MockTransport(handler))
    assert await stt.transcribe(b"\x00\x00" * 1600) == "hi"
    await stt.aclose()


# ── text-to-speech providers ─────────────────────────────────────────────────────
def _mock(tts, handler, base, headers):
    tts._client = httpx.AsyncClient(base_url=base, headers=headers, transport=httpx.MockTransport(handler))


async def test_elevenlabs_streams_pcm_and_lists_voices():
    seen = {}

    def handler(request):
        if request.url.path == "/v1/voices":
            return httpx.Response(200, json={"voices": [{"voice_id": "v1", "name": "Rachel"}]})
        seen.update(path=request.url.path, fmt=request.url.params["output_format"],
                    key=request.headers["xi-api-key"], body=json.loads(request.content))
        return httpx.Response(200, content=b"\x01\x02" * 1000 + b"\x03")

    tts = sp.build_tts({"provider": "elevenlabs", "voice": "v1", "sample_rate": 16000})
    _mock(tts, handler, "https://api.elevenlabs.io", {"xi-api-key": "el-key"})
    pcm = b"".join([c async for c in tts.synthesize("Hello")])
    assert len(pcm) % 2 == 0 and len(pcm) == 2000                 # 16-bit aligned
    assert seen["path"] == "/v1/text-to-speech/v1/stream" and seen["fmt"] == "pcm_16000"
    assert seen["key"] == "el-key" and seen["body"] == {"text": "Hello", "model_id": "eleven_flash_v2_5"}
    assert tts.sample_rate == 16000 and tts.label == "elevenlabs:v1"
    assert await tts.voices() == [{"id": "v1", "name": "Rachel"}]
    await tts.aclose()


async def test_cartesia_streams_pcm_and_reports_errors():
    def handler(request):
        if request.url.path == "/voices":
            return httpx.Response(200, json=[{"id": "c1", "name": "Sarah"}])
        body = json.loads(request.content)
        if body["transcript"] == "fail":
            return httpx.Response(401, json={"error": "bad key"})
        assert body["voice"] == {"mode": "id", "id": "c1"} and request.headers["cartesia-version"]
        assert body["output_format"] == {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000}
        return httpx.Response(200, content=b"\x00\x01" * 500)

    tts = sp.build_tts({"provider": "cartesia", "voice": "c1"})
    _mock(tts, handler, "https://api.cartesia.ai", {"X-API-Key": "k", "Cartesia-Version": "2024-06-10"})
    assert len(b"".join([c async for c in tts.synthesize("Hi")])) == 1000
    with pytest.raises(RuntimeError, match="HTTP 401"):
        _ = [c async for c in tts.synthesize("fail")]
    assert await tts.voices() == [{"id": "c1", "name": "Sarah"}]
    await tts.aclose()


async def test_tts_primary_failing_falls_back_and_is_recorded():
    class Broken(FakeTTS):
        label = "broken-tts"

        async def synthesize(self, text):
            raise RuntimeError("provider down")
            yield b""                                              # pragma: no cover

    backup = FakeTTS()
    backup.label = "backup-tts"
    tts = sp.ResilientTTS(Broken(), backup, retry_delay=0)
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="We open at nine.")])
    session, sent, _c, turns, _h = make_session(prov, tts=tts)
    await speak_utterance(session)
    await settle(session)
    assert sent and ("agent", "We open at nine.") in turns
    t = session.turn_timings[-1]
    assert t["tts"] == "backup-tts" and "tts:fallback" in t["fallbacks"] and "provider down" in t["error"]
    await session.stop()


# ── stage timings + Whisper auto ─────────────────────────────────────────────────
async def test_turn_timings_have_every_stage():
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="We open at nine. Bye.")])
    session, *_ = make_session(prov, tts=FakeTTS(delay=0.02))
    await speak_utterance(session)
    await settle(session)
    t = session.turn_timings[-1]
    for key in ("stt_ms", "llm_first_sentence_ms", "tts_first_audio_ms", "total_ms", "llm_first_ms",
                "stt", "llm", "tts", "fallbacks", "error"):
        assert key in t, key
    assert t["tts_first_audio_ms"] >= 15 and t["total_ms"] >= t["tts_first_audio_ms"]
    await session.stop()


def test_whisper_auto_picks_by_hardware():
    assert sp.resolve_whisper_model("auto", "cpu") == "small.en"
    assert sp.resolve_whisper_model("medium.en", "cuda") == "medium.en"
    assert sp.resolve_whisper_model("auto") in ("small.en", "large-v3-turbo")


def test_streaming_only_for_deepgram_and_keys_are_their_own(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    monkeypatch.setenv("OPENAI_API_KEY", "oa")
    assert sp.build_streaming_stt({"provider": "whisper-local"}) is None
    assert sp.build_streaming_stt({"provider": "deepgram", "streaming": False}) is None
    s = sp.build_streaming_stt({"provider": "deepgram", "api_key_env": "OPENAI_API_KEY"})
    assert s.api_key == "dg"                                      # never the OpenAI key
