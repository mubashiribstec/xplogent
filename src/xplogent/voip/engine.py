"""CallSession — the real-time loop for one phone call.

Carrier audio (8 kHz μ-law frames) goes in through :meth:`on_audio`; the agent's spoken
reply goes out through the ``send_audio`` callback. Between the two:

    VAD  ->  STT  ->  CallBrain (streams sentences)  ->  TTS  ->  μ-law frames out

Barge-in: if the caller starts talking while the agent is speaking, speech is cancelled
immediately and the carrier's audio buffer is cleared, so the agent stops mid-sentence
like a person would. All telephony specifics live in the adapter that wires the callbacks,
so this class is provider-agnostic and fully testable with fake STT/TTS.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from xplogent.core.logging import get_logger
from xplogent.voip.audio import (
    VAD,
    Resampler,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample,
)
from xplogent.voip.brain import CallBrain

_log = get_logger("voip.engine")

FRAME_BYTES = 320  # 20 ms of 8 kHz 16-bit mono
MULAW_FRAME = 160  # 20 ms of 8 kHz μ-law
SendAudio = Callable[[bytes], Awaitable[None]]
Hook = Callable[..., Awaitable[None]]


class CallSession:
    def __init__(self, *, brain: CallBrain, stt: Any, tts: Any, send_audio: SendAudio,
                 clear_audio: Callable[[], Awaitable[None]] | None = None,
                 hangup: Callable[[], Awaitable[None]] | None = None,
                 transfer: Callable[[str], Awaitable[None]] | None = None,
                 on_turn: Callable[[str, str], None] | None = None,
                 greeting: str = "", vad: VAD | None = None,
                 idle_prompt_s: float = 12.0, idle_hangup_s: float = 30.0,
                 max_call_s: float = 900.0, realtime: bool = True,
                 lead_s: float = 0.2) -> None:
        self.brain, self.stt, self.tts = brain, stt, tts
        self.send_audio, self.clear_audio = send_audio, clear_audio
        self.hangup_cb, self.transfer_cb = hangup, transfer
        self.on_turn = on_turn
        self.greeting = greeting
        self.vad = vad or VAD()
        self.idle_prompt_s, self.idle_hangup_s, self.max_call_s = idle_prompt_s, idle_hangup_s, max_call_s
        # Send audio at playback speed (keeping only ``lead_s`` buffered at the carrier),
        # so "speaking" means the caller is actually hearing it and barge-in can stop it.
        self.realtime, self.lead_s = realtime, lead_s

        self.started = time.time()
        self.finished = asyncio.Event()
        self.latencies_ms: list[float] = []
        self._pending = bytearray()
        self._turn_task: asyncio.Task | None = None
        self._speaking = False
        self._last_activity = time.time()
        self._idle_prompted = False
        self._speech_end_at = 0.0
        self._watchdog: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        self._watchdog = asyncio.create_task(self._watch())
        if self.greeting:
            self._record("agent", self.greeting)
            self._turn_task = asyncio.create_task(self._say(self.greeting))

    async def stop(self) -> None:
        self.finished.set()
        for task in (self._turn_task, self._watchdog):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    @property
    def duration_s(self) -> float:
        return time.time() - self.started

    def _record(self, speaker: str, text: str) -> None:
        if self.on_turn and text.strip():
            try:
                self.on_turn(speaker, text.strip())
            except Exception:  # noqa: BLE001 - logging a turn must never break the call
                _log.exception("on_turn failed")

    # -- inbound audio -----------------------------------------------------------
    async def on_audio(self, mulaw: bytes) -> None:
        if self.finished.is_set():
            return
        self._pending.extend(mulaw_to_pcm16(mulaw))
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            event = self.vad.feed(frame)
            if event == "speech_start":
                self._last_activity = time.time()
                self._idle_prompted = False
                if self._speaking:
                    await self._barge_in()
            elif event == "speech_end":
                self._speech_end_at = time.time()
                await self._begin_turn(self.vad.take())

    async def _barge_in(self) -> None:
        _log.info("barge-in: caller interrupted the agent")
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self._speaking = False
        if self.clear_audio:
            with contextlib.suppress(Exception):
                await self.clear_audio()

    async def _begin_turn(self, pcm8k: bytes) -> None:
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self._turn_task = asyncio.create_task(self._turn(pcm8k))

    # -- one turn ----------------------------------------------------------------
    async def _turn(self, pcm8k: bytes) -> None:
        try:
            text = await self.stt.transcribe(resample(pcm8k, 8000, 16000))
            text = (text or "").strip()
            if len(text) < 2:
                return
            self._last_activity = time.time()
            self._record("caller", text)
            reply_parts: list[str] = []
            first = True

            async def sentences():
                nonlocal first
                async for sentence in self.brain.respond(text):
                    reply_parts.append(sentence)
                    yield sentence

            async for sentence in sentences():
                if first:
                    first = False
                    self.latencies_ms.append((time.time() - self._speech_end_at) * 1000)
                await self._say(sentence)
            if reply_parts:
                self._record("agent", " ".join(reply_parts))
            await self._after_turn()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad turn must not drop the call
            _log.exception("turn failed")
            await self._say("Sorry, I had a problem. Could you say that again?")

    async def _after_turn(self) -> None:
        st = self.brain.state
        if st.transfer and self.transfer_cb:
            await self.transfer_cb(st.transfer_reason)
            self.finished.set()
        elif st.end:
            await self._hangup()

    async def _hangup(self) -> None:
        self.finished.set()
        if self.hangup_cb:
            with contextlib.suppress(Exception):
                await self.hangup_cb()

    # -- speaking ----------------------------------------------------------------
    async def _say(self, text: str) -> None:
        """Synthesize ``text`` and stream it to the caller as 20 ms μ-law frames.

        Frames go out at playback speed. Pushing a whole reply at once (TTS is faster
        than real time) left ``_speaking`` False while the carrier was still playing
        seconds of buffered audio, so barge-in never triggered.
        """
        self._speaking = True
        loop = asyncio.get_running_loop()
        play_end = loop.time()  # when the carrier will have played everything sent

        async def push(frame: bytes) -> None:
            nonlocal play_end
            if self.realtime:
                ahead = play_end - loop.time()
                if ahead > self.lead_s:
                    await asyncio.sleep(ahead - self.lead_s)
            await self.send_audio(frame)
            play_end = max(play_end, loop.time()) + len(frame) / 8000.0

        try:
            carry = b""
            rs = Resampler(self.tts.sample_rate, 8000)  # one per utterance: no clicks between chunks
            async for pcm in self.tts.synthesize(text):
                carry += pcm16_to_mulaw(rs.feed(pcm))
                while len(carry) >= MULAW_FRAME:
                    await push(carry[:MULAW_FRAME])
                    carry = carry[MULAW_FRAME:]
            if carry:
                await push(carry)
            if self.realtime:
                # Still "speaking" until the caller has heard the last frame.
                remaining = play_end - loop.time()
                if remaining > 0:
                    await asyncio.sleep(remaining)
        finally:
            self._speaking = False
            self._last_activity = time.time()

    async def _prompt(self, text: str, *, interrupt: bool = False,
                      wait: bool = False) -> asyncio.Task | None:
        """Speak a watchdog line as the tracked turn, so barge-in can stop it and it
        never talks over (or races) a reply that is already in progress."""
        busy = self._turn_task is not None and not self._turn_task.done()
        if busy and not interrupt:
            return None
        if busy:
            self._turn_task.cancel()
            await asyncio.wait([self._turn_task])
        self._record("agent", text)
        task = asyncio.create_task(self._say(text))
        self._turn_task = task
        if wait:
            await asyncio.wait([task])
        return task

    # -- watchdog: silence + max duration -------------------------------------------
    async def _watch(self) -> None:
        while not self.finished.is_set():
            await asyncio.sleep(1.0)
            now = time.time()
            if now - self.started > self.max_call_s:
                await self._prompt("I'm sorry, we've reached the time limit for this call. "
                                   "Goodbye.", interrupt=True, wait=True)
                await self._hangup()
                return
            if self._speaking or self.vad.in_speech:
                continue
            if self._turn_task is not None and not self._turn_task.done():
                continue  # the agent is still working on a reply: not silence
            idle = now - self._last_activity
            if idle > self.idle_hangup_s:
                task = await self._prompt(
                    "I haven't heard anything, so I'll end the call. Goodbye.", wait=True)
                if task is not None and task.cancelled():
                    continue  # the caller spoke over the goodbye: they're still there
                await self._hangup()
                return
            if idle > self.idle_prompt_s and not self._idle_prompted:
                self._idle_prompted = True
                await self._prompt("Are you still there?")
