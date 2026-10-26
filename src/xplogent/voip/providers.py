"""Speech-to-text / text-to-speech adapters for the calling agent.

Everything speaks the same tiny interface so you can start on a cheap cloud API and
move to your own GPU box (or back) by changing config only:

* **STT**: ``transcribe(pcm16_16k) -> str``
* **TTS**: ``synthesize(text) -> AsyncIterator[pcm16 bytes]`` at ``sample_rate`` Hz

The HTTP adapters use the OpenAI-compatible audio endpoints (``/audio/transcriptions``,
``/audio/speech``), which are also what self-hosted servers such as faster-whisper-server
and Kokoro-FastAPI expose — so a local GPU deployment needs no extra code. Deepgram
(speech-to-text, batch and streaming), ElevenLabs and Cartesia (text-to-speech) have
their own adapters behind the same interfaces.

* **Streaming STT** (``voip.stt.provider: deepgram``): ``new_stream()`` gives one
  utterance's stream — ``start()``, ``feed(pcm16)`` at ``sample_rate`` (8 kHz),
  ``await finish() -> str``, ``await aclose()``. The engine feeds it while the caller
  speaks, so the transcript is ready almost as soon as they stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import wave
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from xplogent.core.logging import get_logger

_log = get_logger("voip.speech")


def pcm_to_wav(pcm16: bytes, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()


class STT(Protocol):
    async def transcribe(self, pcm16_16k: bytes) -> str: ...
    async def aclose(self) -> None: ...


class TTS(Protocol):
    sample_rate: int

    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...
    async def aclose(self) -> None: ...


class OpenAICompatSTT:
    """POST audio to an OpenAI-compatible ``/audio/transcriptions`` endpoint."""

    def __init__(self, base_url: str = "https://api.openai.com/v1", model: str = "whisper-1",
                 api_key: str = "", language: str = "en") -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers,
                                         timeout=httpx.Timeout(30.0))
        self.model, self.language = model, language
        self.label = f"openai-compatible:{model}"

    async def transcribe(self, pcm16_16k: bytes) -> str:
        if not pcm16_16k:
            return ""
        files = {"file": ("speech.wav", pcm_to_wav(pcm16_16k), "audio/wav")}
        data = {"model": self.model, "language": self.language, "response_format": "json"}
        resp = await self._client.post("/audio/transcriptions", files=files, data=data)
        resp.raise_for_status()
        return str(resp.json().get("text", "")).strip()

    async def aclose(self) -> None:
        await self._client.aclose()


class WhisperLocalSTT:
    """In-process faster-whisper (best on a GPU). Lazy-loads the model once."""

    _model: Any = None

    def __init__(self, model: str = "small.en", device: str = "auto",
                 compute_type: str = "auto", language: str = "en") -> None:
        self.model_name, self.device = resolve_whisper_model(model, device), device
        self.compute_type, self.language = compute_type, language
        self.label = f"whisper-local:{os.path.basename(str(self.model_name).rstrip('/')) or self.model_name}"

    def _load(self):
        if WhisperLocalSTT._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError("faster-whisper is not installed: pip install 'xplogent[voice]'") from exc
            WhisperLocalSTT._model = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type)
            WhisperLocalSTT._model._xpl_name = self.model_name   # lets training reuse it
        return WhisperLocalSTT._model

    def _run(self, pcm16_16k: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm16_16k, dtype=np.int16).astype("float32") / 32768.0
        segments, _ = self._load().transcribe(
            audio, language=self.language, beam_size=1, vad_filter=False,
            condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, pcm16_16k: bytes) -> str:
        if not pcm16_16k:
            return ""
        return await asyncio.to_thread(self._run, pcm16_16k)

    async def aclose(self) -> None:
        return None


class OpenAICompatTTS:
    """Stream raw PCM from an OpenAI-compatible ``/audio/speech`` endpoint.

    OpenAI returns 24 kHz 16-bit mono PCM for ``response_format=pcm``; Kokoro-FastAPI
    and similar local servers follow the same convention.
    """

    def __init__(self, base_url: str = "https://api.openai.com/v1", model: str = "tts-1",
                 voice: str = "alloy", api_key: str = "", sample_rate: int = 24000) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers,
                                         timeout=httpx.Timeout(30.0))
        self.model, self.voice, self.sample_rate = model, voice, sample_rate
        local = "kokoro" in base_url or ":8880" in base_url
        self.label = f"{'kokoro' if local else 'openai-compatible'}:{voice}"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        payload = {"model": self.model, "voice": self.voice, "input": text,
                   "response_format": "pcm"}
        async with self._client.stream("POST", "/audio/speech", json=payload) as resp:
            resp.raise_for_status()
            carry = b""
            async for chunk in resp.aiter_bytes(4096):
                data = carry + chunk
                usable = len(data) - (len(data) % 2)  # keep 16-bit sample alignment
                carry = data[usable:]
                if usable:
                    yield data[:usable]

    async def aclose(self) -> None:
        await self._client.aclose()


def resolve_whisper_model(name: str, device: str = "auto") -> str:
    """``auto``: large-v3-turbo on an NVIDIA GPU, small.en on CPU (fast enough there)."""
    if str(name or "").strip() not in ("", "auto"):
        return str(name)
    if device == "cpu":
        return "small.en"
    try:
        import ctranslate2

        gpu = ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - no CUDA build / no driver: CPU
        gpu = False
    return "large-v3-turbo" if gpu and device in ("auto", "cuda") else "small.en"


async def _pcm_stream(resp: httpx.Response) -> AsyncIterator[bytes]:
    """Yield 16-bit-aligned PCM chunks from a streaming HTTP response."""
    carry = b""
    async for chunk in resp.aiter_bytes(4096):
        data = carry + chunk
        usable = len(data) - (len(data) % 2)
        carry = data[usable:]
        if usable:
            yield data[:usable]


async def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        body = (await resp.aread())[:300].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")


class DeepgramSTT:
    """Deepgram pre-recorded transcription (``/v1/listen``): the batch path, also the
    streaming adapter's fallback for an utterance whose stream failed."""

    def __init__(self, model: str = "nova-3", api_key: str = "", language: str = "en",
                 base_url: str = "https://api.deepgram.com") -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(30.0),
                                         headers={"Authorization": f"Token {api_key}"} if api_key else {})
        self.model, self.language = model, language
        self.label = f"deepgram:{model}"

    async def transcribe(self, pcm16_16k: bytes) -> str:
        if not pcm16_16k:
            return ""
        resp = await self._client.post(
            "/v1/listen", params={"model": self.model, "language": self.language, "smart_format": "true"},
            content=pcm_to_wav(pcm16_16k), headers={"Content-Type": "audio/wav"})
        resp.raise_for_status()
        alts = resp.json()["results"]["channels"][0]["alternatives"]
        return str(alts[0].get("transcript", "") if alts else "").strip()

    async def aclose(self) -> None:
        await self._client.aclose()


class DeepgramStream:
    """One utterance on Deepgram's live WebSocket. Frames queue up while it connects."""

    def __init__(self, url: str, api_key: str, finish_timeout_s: float = 3.0) -> None:
        self.url, self.api_key, self.finish_timeout_s = url, api_key, finish_timeout_s
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._finals: list[str] = []
        self._done = asyncio.Event()           # finished one way or another
        self._finalized = False                # Deepgram confirmed the end of the utterance
        self._task: asyncio.Task | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def feed(self, pcm16: bytes) -> None:
        if pcm16:
            self._queue.put_nowait(pcm16)

    async def _run(self) -> None:
        import json

        from websockets.asyncio.client import connect

        try:
            async with connect(self.url, additional_headers={"Authorization": f"Token {self.api_key}"},
                               open_timeout=5, close_timeout=1) as ws:
                async def send() -> None:
                    while (chunk := await self._queue.get()) is not None:
                        await ws.send(chunk)
                    await ws.send(json.dumps({"type": "Finalize"}))

                sender = asyncio.create_task(send())
                try:
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            continue
                        msg = json.loads(raw)
                        if msg.get("type") != "Results":
                            continue
                        if msg.get("is_final"):
                            alts = (msg.get("channel") or {}).get("alternatives") or [{}]
                            text = str(alts[0].get("transcript", "")).strip()
                            if text:
                                self._finals.append(text)
                        if msg.get("from_finalize"):
                            self._finalized = True
                            self._done.set()
                            with contextlib.suppress(Exception):
                                await ws.send(json.dumps({"type": "CloseStream"}))
                            break
                finally:
                    sender.cancel()
        except Exception as exc:  # noqa: BLE001 - surfaced by finish()
            self._error = exc
        finally:
            self._done.set()

    async def finish(self) -> str:
        """The utterance's transcript. Raises if the stream failed (the engine then
        transcribes the buffered audio with the batch chain: no words are lost)."""
        self._queue.put_nowait(None)
        await asyncio.wait_for(self._done.wait(), timeout=self.finish_timeout_s)
        if self._error is not None:
            raise RuntimeError(f"streaming speech-to-text failed: {self._error}") from self._error
        if not self._finalized:
            raise RuntimeError("streaming speech-to-text did not confirm the end of speech")
        return " ".join(self._finals).strip()

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task


class DeepgramStreamingSTT:
    """Factory for per-utterance Deepgram live streams (8 kHz 16-bit PCM in)."""

    sample_rate = 8000

    def __init__(self, model: str = "nova-3", api_key: str = "", language: str = "en",
                 ws_url: str = "wss://api.deepgram.com/v1/listen") -> None:
        from urllib.parse import urlencode

        query = urlencode({"model": model, "language": language, "encoding": "linear16",
                           "sample_rate": self.sample_rate, "channels": 1, "smart_format": "true",
                           "interim_results": "false"})
        self.url, self.api_key = f"{ws_url}?{query}", api_key
        self.label = f"deepgram-stream:{model}"

    def new_stream(self) -> DeepgramStream:
        return DeepgramStream(self.url, self.api_key)


class ElevenLabsTTS:
    """ElevenLabs streaming text-to-speech, raw 16-bit PCM at ``sample_rate``."""

    _RATES = (16000, 22050, 24000, 44100)

    def __init__(self, voice: str, model: str = "eleven_flash_v2_5", api_key: str = "",
                 sample_rate: int = 24000, base_url: str = "https://api.elevenlabs.io") -> None:
        self.sample_rate = sample_rate if sample_rate in self._RATES else 24000
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(30.0),
                                         headers={"xi-api-key": api_key} if api_key else {})
        self.model, self.voice = model, voice
        self.label = f"elevenlabs:{voice}"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        async with self._client.stream(
                "POST", f"/v1/text-to-speech/{self.voice}/stream",
                params={"output_format": f"pcm_{self.sample_rate}"},
                json={"text": text, "model_id": self.model}) as resp:
            await _raise_for_status(resp)
            async for chunk in _pcm_stream(resp):
                yield chunk

    async def voices(self) -> list[dict[str, str]]:
        resp = await self._client.get("/v1/voices")
        resp.raise_for_status()
        return [{"id": v["voice_id"], "name": v.get("name", v["voice_id"])} for v in resp.json().get("voices", [])]

    async def aclose(self) -> None:
        await self._client.aclose()


class CartesiaTTS:
    """Cartesia text-to-speech (``/tts/bytes``), raw 16-bit PCM at ``sample_rate``."""

    def __init__(self, voice: str, model: str = "sonic-2", api_key: str = "", sample_rate: int = 24000,
                 language: str = "en", api_version: str = "2024-06-10",
                 base_url: str = "https://api.cartesia.ai") -> None:
        self.sample_rate = sample_rate
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(30.0),
                                         headers={"X-API-Key": api_key, "Cartesia-Version": api_version})
        self.model, self.voice, self.language = model, voice, language
        self.label = f"cartesia:{voice}"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        body = {"model_id": self.model, "transcript": text, "language": self.language,
                "voice": {"mode": "id", "id": self.voice},
                "output_format": {"container": "raw", "encoding": "pcm_s16le",
                                  "sample_rate": self.sample_rate}}
        async with self._client.stream("POST", "/tts/bytes", json=body) as resp:
            await _raise_for_status(resp)
            async for chunk in _pcm_stream(resp):
                yield chunk

    async def voices(self) -> list[dict[str, str]]:
        resp = await self._client.get("/voices")
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        return [{"id": v["id"], "name": v.get("name", v["id"])} for v in items]

    async def aclose(self) -> None:
        await self._client.aclose()


def _env_key(cfg: dict[str, Any], env: str) -> str:
    """Deepgram / ElevenLabs / Cartesia keys always live under their own secret name
    (``api_key_env`` is the OpenAI-compatible endpoints' setting)."""
    return os.environ.get(env, "")


def build_stt(cfg: dict[str, Any]) -> STT:
    kind = str(cfg.get("provider", "whisper-local"))
    if kind == "whisper-local":
        return WhisperLocalSTT(model=cfg.get("model", "auto"), device=cfg.get("device", "auto"),
                               language=cfg.get("language", "en"))
    if kind == "deepgram":
        return DeepgramSTT(model=cfg.get("model") or "nova-3", api_key=_env_key(cfg, "DEEPGRAM_API_KEY"),
                           language=cfg.get("language", "en"))
    key = os.environ.get(str(cfg.get("api_key_env", "OPENAI_API_KEY")), "")
    return OpenAICompatSTT(base_url=cfg.get("base_url") or "https://api.openai.com/v1",
                           model=cfg.get("model") or "whisper-1", api_key=key,
                           language=cfg.get("language", "en"))


def build_streaming_stt(cfg: dict[str, Any]) -> DeepgramStreamingSTT | None:
    """The live (streaming) speech-to-text, when the provider has one and it's on."""
    if str(cfg.get("provider", "")) != "deepgram" or not cfg.get("streaming", True):
        return None
    return DeepgramStreamingSTT(model=cfg.get("model") or "nova-3",
                                api_key=_env_key(cfg, "DEEPGRAM_API_KEY"),
                                language=cfg.get("language", "en"))


def build_tts(cfg: dict[str, Any]) -> TTS:
    kind = str(cfg.get("provider") or "openai")
    rate = int(cfg.get("sample_rate") or 24000)
    if kind == "elevenlabs":
        return ElevenLabsTTS(voice=cfg.get("voice") or "", model=cfg.get("model") or "eleven_flash_v2_5",
                             api_key=_env_key(cfg, "ELEVENLABS_API_KEY"), sample_rate=rate)
    if kind == "cartesia":
        return CartesiaTTS(voice=cfg.get("voice") or "", model=cfg.get("model") or "sonic-2",
                           api_key=_env_key(cfg, "CARTESIA_API_KEY"), sample_rate=rate,
                           language=str(cfg.get("language") or "en"))
    key = os.environ.get(str(cfg.get("api_key_env", "OPENAI_API_KEY")), "")
    return OpenAICompatTTS(base_url=cfg.get("base_url") or "https://api.openai.com/v1",
                           model=cfg.get("model") or "tts-1", voice=cfg.get("voice") or "alloy",
                           api_key=key, sample_rate=rate)


OPENAI_VOICES = ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]


def _voice_entry(v: Any) -> dict[str, str]:
    """Kokoro lists voices as plain names (older images) or as objects with ``id`` /
    ``name`` and quality grades (newer ones): always ``{id, name}`` strings."""
    if isinstance(v, dict):
        vid = str(v.get("id") or v.get("name") or "")
        grade = str(v.get("overall_grade") or "")
        return {"id": vid, "name": str(v.get("name") or vid) + (f" ({grade})" if grade else "")}
    return {"id": str(v), "name": str(v)}


async def list_voices(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """Voices for the Speech card's picker: ``[{id, name}]`` for the configured provider."""
    kind = str(cfg.get("provider") or "openai")
    tts = build_tts(cfg)
    try:
        if kind in ("elevenlabs", "cartesia"):
            return await tts.voices()
        base = str(cfg.get("base_url") or "")
        if "kokoro" in base or "8880" in base:
            resp = await tts._client.get("/audio/voices")
            resp.raise_for_status()
            return [_voice_entry(v) for v in resp.json().get("voices", []) if _voice_entry(v)["id"]]
        return [{"id": v, "name": v} for v in OPENAI_VOICES]
    finally:
        await tts.aclose()


# ── resilience: one retry, then an optional fallback provider ────────────────────
def label_of(provider: Any) -> str:
    return str(getattr(provider, "label", "") or type(provider).__name__)


class ResilientSTT:
    """Retry a failed transcription once, then use the fallback (if any)."""

    def __init__(self, primary: STT, fallback: STT | None = None, retry_delay: float = 0.3) -> None:
        self.primary, self.fallback, self.retry_delay = primary, fallback, retry_delay
        self.used_fallback = 0
        self.label = label_of(primary)
        self.last: dict[str, Any] = {}      # who served the last request, and any error on the way

    async def transcribe(self, pcm16_16k: bytes) -> str:
        self.last = {"provider": label_of(self.primary), "fallback": False, "error": ""}
        try:
            return await self.primary.transcribe(pcm16_16k)
        except Exception as exc:  # noqa: BLE001 - retried below
            self.last["error"] = f"{type(exc).__name__}: {exc}"[:300]
            await asyncio.sleep(self.retry_delay)
        try:
            return await self.primary.transcribe(pcm16_16k)
        except Exception as exc:
            self.last["error"] = f"{type(exc).__name__}: {exc}"[:300]
            if self.fallback is None:
                raise
            _log.warning("speech-to-text failed twice (%s); using the fallback", exc)
            self.used_fallback += 1
            self.last.update(provider=label_of(self.fallback), fallback=True)
            return await self.fallback.transcribe(pcm16_16k)

    async def aclose(self) -> None:
        for p in (self.primary, self.fallback):
            if p is not None:
                with contextlib.suppress(Exception):
                    await p.aclose()


class ResilientTTS:
    """Retry synthesis once if it fails before any audio, then use the fallback.

    Once audio has started playing a failure is not retried (the caller would hear
    the sentence twice). Fallback audio is resampled to the primary's sample rate,
    which is what the call engine expects.
    """

    def __init__(self, primary: TTS, fallback: TTS | None = None, retry_delay: float = 0.3) -> None:
        self.primary, self.fallback, self.retry_delay = primary, fallback, retry_delay
        self.sample_rate = primary.sample_rate
        self.used_fallback = 0
        self.label = label_of(primary)
        self.last: dict[str, Any] = {}

    async def _attempt(self, tts: TTS, text: str, started: list[bool]) -> AsyncIterator[bytes]:
        from xplogent.voip.audio import Resampler

        rs = Resampler(tts.sample_rate, self.sample_rate) if tts.sample_rate != self.sample_rate else None
        async for chunk in tts.synthesize(text):
            started[0] = True
            yield rs.feed(chunk) if rs else chunk

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        started = [False]
        chain = [self.primary, self.primary] + ([self.fallback] if self.fallback else [])
        self.last = {"provider": label_of(self.primary), "fallback": False, "error": ""}
        for n, tts in enumerate(chain):
            try:
                self.last.update(provider=label_of(tts), fallback=tts is not self.primary)
                async for chunk in self._attempt(tts, text, started):
                    yield chunk
                return
            except Exception as exc:
                self.last["error"] = f"{type(exc).__name__}: {exc}"[:300]
                if started[0] or n == len(chain) - 1:
                    raise
                if tts is self.primary and n == 1:
                    _log.warning("text-to-speech failed twice (%s); using the fallback", exc)
                    self.used_fallback += 1
                await asyncio.sleep(self.retry_delay)

    async def aclose(self) -> None:
        for p in (self.primary, self.fallback):
            if p is not None:
                with contextlib.suppress(Exception):
                    await p.aclose()


def default_stt_fallback(stt_cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Remote speech-to-text falls back to the built-in Whisper when it is installed."""
    import importlib.util

    if str(stt_cfg.get("provider", "whisper-local")) == "whisper-local":
        return None
    if importlib.util.find_spec("faster_whisper") is None:
        return None
    return {"provider": "whisper-local", "model": "base.en", "language": stt_cfg.get("language", "en")}
