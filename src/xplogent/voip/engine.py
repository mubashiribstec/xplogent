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

TRANSFER_FAILED_LINE = ("I'm sorry, I couldn't connect you to a colleague just now. "
                        "I can keep helping you, or arrange a call back.")
NO_TRANSFER_LINE = ("I'm sorry, I can't transfer this call right now, but I can arrange "
                    "for someone to call you back.")
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
                 lead_s: float = 0.2,
                 on_events: Callable[[list[str]], Awaitable[None]] | None = None,
                 transfer_timeout_s: float = 60.0) -> None:
        self.brain, self.stt, self.tts = brain, stt, tts
        self.send_audio, self.clear_audio = send_audio, clear_audio
        self.hangup_cb, self.transfer_cb = hangup, transfer
        self.on_turn = on_turn
        self.on_events = on_events          # tool events after each reply (test calls)
        self.transfer_timeout_s = transfer_timeout_s
        self._events_seen = 0
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
        self._speak_gen = 0                 # which _say owns the _speaking flag
        self._last_activity = time.time()
        self.transfer_pending = False
        self._transfer_timer: asyncio.Task | None = None
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
        for task in (self._turn_task, self._watchdog, self._transfer_timer):
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
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad turn must not drop the call
            _log.exception("speech-to-text failed")
            await self._say("Sorry, I had a problem. Could you say that again?")
            return
        await self._reply((text or "").strip())

    async def on_text(self, text: str) -> None:
        """A caller turn that is already text (browser test calls)."""
        if self.finished.is_set() or not text.strip():
            return
        self._speech_end_at = time.time()
        if self._speaking:
            await self._barge_in()
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
        self._turn_task = asyncio.create_task(self._reply(text.strip()))

    async def _reply(self, text: str) -> None:
        try:
            if len(text) < 2:
                return
            self._last_activity = time.time()
            self._record("caller", text)
            if getattr(self.brain, "paused", False):
                return              # a person is handling the call
            reply_parts: list[str] = []
            first = True

            async def sentences():
                nonlocal first
                async for sentence in self.brain.respond(text):
                    reply_parts.append(sentence)
                    yield sentence

            try:
                async for sentence in sentences():
                    if first:
                        first = False
                        self.latencies_ms.append((time.time() - self._speech_end_at) * 1000)
                    await self._say(sentence)
            except asyncio.CancelledError:
                if reply_parts:     # interrupted: keep what the caller already heard
                    self._record("agent", " ".join(reply_parts) + " …")
                raise
            if reply_parts:
                self._record("agent", " ".join(reply_parts))
            if self.on_events:
                with contextlib.suppress(Exception):
                    await self.on_events(self.brain.state.events[self._events_seen:])
            self._events_seen = len(self.brain.state.events)
            await self._after_turn()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad turn must not drop the call
            _log.exception("turn failed")
            with contextlib.suppress(Exception):
                await self._say("Sorry, I had a problem. Could you say that again?")

    # -- supervisor controls ---------------------------------------------------------
    async def say(self, text: str) -> None:
        """Speak ``text`` now (interrupting the agent) and remember it was said."""
        if self.finished.is_set() or not text.strip():
            return
        if self.clear_audio and self._speaking:
            with contextlib.suppress(Exception):
                await self.clear_audio()
        if hasattr(self.brain, "said"):
            self.brain.said(text)
        await self._prompt(text.strip(), interrupt=True)

    async def hush(self) -> None:
        """Stop talking now: cancel the reply in progress and flush queued audio."""
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn_task
        self._speaking = False
        if self.clear_audio:
            with contextlib.suppress(Exception):
                await self.clear_audio()

    async def end(self) -> None:
        """Hang up now (supervisor)."""
        await self._hangup()

    async def _after_turn(self) -> None:
        st = self.brain.state
        if st.transfer and not self.transfer_pending:
            if self.transfer_cb is None:
                # Promised a hand-off we can't make: say so and keep helping.
                st.transfer = False
                if st.outcome == "transferred":
                    st.outcome = ""
                await self._say(NO_TRANSFER_LINE)
                self._record("agent", NO_TRANSFER_LINE)
                return
            with contextlib.suppress(Exception):   # failure is handled by start_transfer
                await self.start_transfer(lambda: self.transfer_cb(st.transfer_reason),
                                          st.transfer_reason)
        elif st.end:
            await self._hangup()

    # -- transfers -------------------------------------------------------------------
    async def start_transfer(self, attempt: Callable[[], Awaitable[None]], reason: str = "") -> None:
        """Hand the caller over. The agent stays quiet while it is pending; the
        transport then calls :meth:`transfer_done` (the caller left us) or
        :meth:`transfer_failed` (the caller is still here). No answer within
        ``transfer_timeout_s`` counts as failed. ``attempt`` raising = failed now."""
        st = self.brain.state
        self.brain.paused = True
        self.transfer_pending = True
        st.transfer, st.outcome = True, st.outcome or "transferred"
        st.transfer_reason = st.transfer_reason or reason
        try:
            await attempt()
        except Exception as exc:
            _log.warning("transfer failed: %s", exc)
            await self.transfer_failed(str(exc))
            raise
        if self.transfer_pending:
            self._transfer_timer = asyncio.create_task(self._transfer_watchdog())

    async def _transfer_watchdog(self) -> None:
        await asyncio.sleep(self.transfer_timeout_s)
        if self.transfer_pending and not self.finished.is_set():
            await self.transfer_failed("no answer from the transfer")

    def transfer_done(self) -> None:
        """The caller is now connected elsewhere: this session is over."""
        self.transfer_pending = False
        self.finished.set()

    async def transfer_failed(self, why: str = "") -> None:
        """The caller is still with us: resume the agent and say so."""
        if not self.transfer_pending or self.finished.is_set():
            return
        self.transfer_pending = False
        st = self.brain.state
        st.transfer, st.transfer_failed = False, True
        if st.outcome == "transferred":
            st.outcome = ""
        st.events.append(f"transfer failed: {why}" if why else "transfer failed")
        self.brain.paused = False
        if hasattr(self.brain, "said"):
            self.brain.said(TRANSFER_FAILED_LINE)
        if asyncio.current_task() is self._turn_task:   # called from the agent's own turn
            self._record("agent", TRANSFER_FAILED_LINE)
            await self._say(TRANSFER_FAILED_LINE)
        else:
            await self._prompt(TRANSFER_FAILED_LINE, interrupt=True)

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
        self._speak_gen += 1
        gen = self._speak_gen
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
            # A cancelled _say must not clear the flag of a newer one (missed barge-in).
            if self._speak_gen == gen:
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
            if self._speaking or self.vad.in_speech or getattr(self.brain, "paused", False):
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
