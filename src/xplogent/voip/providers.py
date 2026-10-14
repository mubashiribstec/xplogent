"""Speech-to-text / text-to-speech adapters for the calling agent.

Everything speaks the same tiny interface so you can start on a cheap cloud API and
move to your own GPU box (or back) by changing config only:

* **STT**: ``transcribe(pcm16_16k) -> str``
* **TTS**: ``synthesize(text) -> AsyncIterator[pcm16 bytes]`` at ``sample_rate`` Hz

The HTTP adapters use the OpenAI-compatible audio endpoints (``/audio/transcriptions``,
``/audio/speech``), which are also what self-hosted servers such as faster-whisper-server
and Kokoro-FastAPI expose — so a local GPU deployment needs no extra code.
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
        self.model_name, self.device = model, device
        self.compute_type, self.language = compute_type, language

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


def build_stt(cfg: dict[str, Any]) -> STT:
    kind = str(cfg.get("provider", "whisper-local"))
    if kind == "whisper-local":
        return WhisperLocalSTT(model=cfg.get("model", "small.en"), device=cfg.get("device", "auto"),
                               language=cfg.get("language", "en"))
    key = os.environ.get(str(cfg.get("api_key_env", "OPENAI_API_KEY")), "")
    return OpenAICompatSTT(base_url=cfg.get("base_url") or "https://api.openai.com/v1",
                           model=cfg.get("model") or "whisper-1", api_key=key,
                           language=cfg.get("language", "en"))


def build_tts(cfg: dict[str, Any]) -> TTS:
    key = os.environ.get(str(cfg.get("api_key_env", "OPENAI_API_KEY")), "")
    return OpenAICompatTTS(base_url=cfg.get("base_url") or "https://api.openai.com/v1",
                           model=cfg.get("model") or "tts-1", voice=cfg.get("voice") or "alloy",
                           api_key=key, sample_rate=int(cfg.get("sample_rate", 24000)))


# ── resilience: one retry, then an optional fallback provider ────────────────────
class ResilientSTT:
    """Retry a failed transcription once, then use the fallback (if any)."""

    def __init__(self, primary: STT, fallback: STT | None = None, retry_delay: float = 0.3) -> None:
        self.primary, self.fallback, self.retry_delay = primary, fallback, retry_delay
        self.used_fallback = 0

    async def transcribe(self, pcm16_16k: bytes) -> str:
        try:
            return await self.primary.transcribe(pcm16_16k)
        except Exception:  # noqa: BLE001 - retried below
            await asyncio.sleep(self.retry_delay)
        try:
            return await self.primary.transcribe(pcm16_16k)
        except Exception as exc:
            if self.fallback is None:
                raise
            _log.warning("speech-to-text failed twice (%s); using the fallback", exc)
            self.used_fallback += 1
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

    async def _attempt(self, tts: TTS, text: str, started: list[bool]) -> AsyncIterator[bytes]:
        from xplogent.voip.audio import Resampler

        rs = Resampler(tts.sample_rate, self.sample_rate) if tts.sample_rate != self.sample_rate else None
        async for chunk in tts.synthesize(text):
            started[0] = True
            yield rs.feed(chunk) if rs else chunk

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        started = [False]
        chain = [self.primary, self.primary] + ([self.fallback] if self.fallback else [])
        for n, tts in enumerate(chain):
            try:
                async for chunk in self._attempt(tts, text, started):
                    yield chunk
                return
            except Exception as exc:
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
