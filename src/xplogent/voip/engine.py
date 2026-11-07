"""CallSession — the real-time loop for one phone call.

Carrier audio (8 kHz μ-law frames) goes in through :meth:`on_audio`; the agent's spoken
reply goes out through the ``send_audio`` callback. Between the two:

    VAD  ->  STT  ->  CallBrain (streams sentences)  ->  TTS  ->  μ-law frames out

Barge-in: if the caller starts talking while the agent is speaking, the engine waits
for the utterance to finish (speech_end + STT) and only then decides: a short
filler / backchannel ("hello?", "hmm", "yes") lets the reply finish playing, while
a real interruption (a question, "stop", a new request) cancels the reply at once
and is answered. The greeting's AI + recording disclosure is never cut off, but
once those words have played the caller may talk over the rest of it. All telephony
specifics live in the adapter that wires the callbacks, so this class is
provider-agnostic and fully testable with fake STT/TTS.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
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
from xplogent.voip.providers import label_of


def llm_label(provider: Any) -> str:
    """Which model answered: ``backup:<spec>`` when the backup model stepped in."""
    if getattr(provider, "used_backup", False):
        return f"backup:{getattr(provider, 'backup_spec', '')}"
    name, model = getattr(provider, "name", ""), getattr(provider, "model", "")
    return f"{name}:{model}" if name and model else str(model or name or type(provider).__name__)

_log = get_logger("voip.engine")

FRAME_BYTES = 320  # 20 ms of 8 kHz 16-bit mono
MULAW_FRAME = 160  # 20 ms of 8 kHz μ-law
SendAudio = Callable[[bytes], Awaitable[None]]

TRANSFER_FAILED_LINE = ("I'm sorry, I couldn't connect you to a colleague just now. "
                        "I can keep helping you, or arrange a call back.")
# Silence recovery (vs-Retell brief C5): at most ``idle_max_prompts`` short prompts,
# spaced between the first-prompt delay and the hangup delay, then a polite goodbye.
SILENCE_PROMPTS = ("Are you still there?", "I still can't hear you — are you there?")

# -- filler vs real interruption (latency / cancellation-loop fix, 2026-11-07) --------
# The 28 Oct call was 27 s of silence because every "Hello?" while the agent was
# replying cancelled the reply and started a fresh turn. A mid-reply utterance is
# therefore classified only once it is finished (speech_end + STT text), never on
# speech_start. The rule is deliberately asymmetric: swallowing a real question is
# worse than one restart, so anything that is not clearly a backchannel counts as
# a real interruption (the old behaviour).
FILLER_MAX_SPEECH_S = 1.0   # a slow 3-word utterance is a real turn, not a blip
_FILLER_WORDS = frozenset({
    # backchannels / acknowledgements / greetings — no content of their own
    "hello", "hi", "hey", "hiya", "hmm", "hm", "mm", "mm-hm", "uh-huh", "uh", "um", "huh",
    "yeah", "yes", "yep", "ok", "okay", "right", "sure", "sorry", "pardon",
    "go", "on", "please", "thanks", "thank", "you",
})
_INTERRUPT_FIRST_WORDS = frozenset({"stop", "wait", "hold"})   # "stop", "wait a minute", "hold on"


def _filler_words(text: str) -> list[str]:
    """The utterance as lowercase words (punctuation stripped, "what's" -> "whats")."""
    return [w.strip("'") for w in re.sub(r"[^a-z0-9' ]", " ", text.lower()).split()
            if w.strip("'")]


def is_filler_utterance(text: str, speech_s: float = 0.0) -> bool:
    """True when ``text`` is filler / backchannel that must NOT cancel the reply.

    Filler: empty or unintelligible; or every word is a backchannel word
    ("hello?", "hmm", "uh-huh", "yeah, go on"). Real interruption: starts with
    stop / wait / hold; 4+ words ("Sorry — what's the hourly pay?"); or 3+ words
    spoken slowly (>= ``FILLER_MAX_SPEECH_S``). Duration alone never makes a
    content word filler — a 0.5 s "pay?" is still a real question.
    """
    words = _filler_words(text)
    if not words:
        return True
    if words[0] in _INTERRUPT_FIRST_WORDS:
        return False
    if len(words) >= 4:
        return False
    if all(w in _FILLER_WORDS for w in words):
        return True
    if len(words) >= 3 and speech_s >= FILLER_MAX_SPEECH_S:
        return False
    return False


def split_greeting_disclosure(greeting: str) -> tuple[str, str]:
    """Split a greeting into (disclosure, rest) at the end of the disclosure sentence.

    The disclosure is the sentence carrying the AI disclosure ("AI assistant") and
    the sentence carrying the recording notice ("record"): the split falls after
    whichever of those two sentences ends later. If neither is found, or the
    disclosure is the whole greeting, the rest is empty and the greeting stays
    uninterruptible throughout — the safe default.
    """
    def _sentence_end_after(text: str, keyword: str) -> int:
        m = re.search(keyword, text, re.IGNORECASE)
        if not m:
            return -1
        end = re.search(r"[.?!](\s|$)", text[m.start():])
        return m.start() + end.end() if end else len(text)

    ends = [_sentence_end_after(greeting, r"ai assistant"), _sentence_end_after(greeting, r"record")]
    cut = max(ends)
    if cut <= 0:
        return greeting, ""
    disclosure, rest = greeting[:cut].strip(), greeting[cut:].strip()
    return (disclosure, rest) if rest else (greeting, "")
def closing_line(st: Any) -> str:
    """A goodbye for a call the agent ended without saying one (reads a booked time back)."""
    if getattr(st, "callback", "") and getattr(st, "callback_at", ""):
        from datetime import datetime

        from xplogent.voip.callbacks import spoken

        who = "a recruiter will" if getattr(st, "callback_who", "") == "recruiter" else "we'll"
        when = spoken(datetime.fromisoformat(st.callback_at))
        return f"Lovely, so {who} call you back on {when}. Thanks for your time, goodbye."
    if getattr(st, "outcome", "") == "wrong_person":
        return "Sorry to have bothered you. Goodbye."
    return "Thanks for your time. Goodbye."


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
                 idle_max_prompts: int = 2,
                 max_call_s: float = 900.0, realtime: bool = True,
                 lead_s: float = 0.2,
                 on_events: Callable[[list[str]], Awaitable[None]] | None = None,
                 transfer_timeout_s: float = 60.0,
                 send_dtmf: Callable[[str], Awaitable[None]] | None = None,
                 dtmf_keys: dict[str, str] | None = None, dtmf_gap_s: float = 2.0,
                 recorder: Any = None, stream_stt: Any = None,
                 listen_first: bool = False, hello_wait_s: float = 2.5, amd: Any = None,
                 wrap_up_s: float = 60.0, quick_close_s: float = 45.0, cost_cap: float = 0.0,
                 cost_rates: dict[str, float] | None = None) -> None:
        self.brain, self.stt, self.tts = brain, stt, tts
        # Limits: a wrap-up note ``wrap_up_s`` before the time limit; ``quick_close_s`` after
        # a closing outcome (not interested…); a polite end at ``cost_cap`` (0 = none).
        self.wrap_up_s, self.quick_close_s, self.cost_cap = wrap_up_s, quick_close_s, cost_cap
        self.cost_rates = dict(cost_rates or {})
        self.tts_chars = 0
        self._wrapped_up = False
        # Outbound: wait for the callee's "Hello?" (or ``hello_wait_s``) before the greeting,
        # so the agent doesn't talk over them and voicemail can be spotted first.
        self.listen_first, self.hello_wait_s = listen_first, hello_wait_s
        self._awaiting_hello = False
        self._hello_task: asyncio.Task | None = None
        self.amd = amd                      # amd.MachineDetector (outbound) or None
        self.send_audio, self.clear_audio = send_audio, clear_audio
        self.hangup_cb, self.transfer_cb = hangup, transfer
        self.on_turn = on_turn
        self.on_events = on_events          # tool events after each reply (test calls)
        self.transfer_timeout_s = transfer_timeout_s
        # Keypad: digits in (collected, then given to the agent) and out (press_keys).
        self.send_dtmf = send_dtmf
        self.dtmf_keys = {str(k): str(v) for k, v in (dtmf_keys or {}).items()}
        self.dtmf_gap_s = dtmf_gap_s
        self._dtmf_buf = ""
        self._dtmf_timer: asyncio.Task | None = None
        self._last_agent_line = ""
        self.dtmf_enabled = True
        self.recorder = recorder            # CallRecorder (voip.recording) or None
        # Streaming speech-to-text (providers.DeepgramStreamingSTT) or None: fed while the
        # caller speaks; the buffered utterance still goes to ``stt`` if the stream fails.
        self.stream_stt = stream_stt
        self._stream: Any = None
        self._turn_info: dict[str, Any] = {}
        self._say_first_frame_at = 0.0
        # Debug view (voip/debug.py): what happened when, plus transport-specific
        # sources (SIP ladder, RTP stats, carrier events) the line adds.
        self.timeline: list[dict[str, Any]] = []
        self.debug_sources: list[Callable[[], dict[str, Any]]] = []
        self._events_seen = 0
        self.greeting = greeting
        self.vad = vad or VAD()
        self.idle_prompt_s, self.idle_hangup_s, self.max_call_s = idle_prompt_s, idle_hangup_s, max_call_s
        self.idle_max_prompts = max(0, idle_max_prompts)
        # Send audio at playback speed (keeping only ``lead_s`` buffered at the carrier),
        # so "speaking" means the caller is actually hearing it and barge-in can stop it.
        self.realtime, self.lead_s = realtime, lead_s

        self.started = time.time()
        self.finished = asyncio.Event()
        self.latencies_ms: list[float] = []
        self.turn_timings: list[dict[str, Any]] = []  # per-turn stage timings + providers (debug/CRM)
        self._last_stt_ms = 0.0
        self._pending = bytearray()
        self._turn_task: asyncio.Task | None = None
        self._speaking = False
        self._speak_gen = 0                 # which _say owns the _speaking flag
        self._last_activity = time.time()
        self.transfer_pending = False
        self._transfer_timer: asyncio.Task | None = None
        self._idle_prompts = 0
        self._speech_end_at = 0.0
        self._watchdog: asyncio.Task | None = None
        # The greeting carries the AI and recording disclosure: it always plays to
        # the end of the disclosure. Speech over it is answered after it, never cuts
        # the disclosure off — but once the disclosure words have played, the caller
        # may talk over the rest of the greeting (see _greeting_disclosure_done).
        self._greeting_task: asyncio.Task | None = None
        self._greeting_disclosure_done = False
        # A caller utterance that started while the agent was speaking: decided at
        # speech_end + STT, never cancelled on speech_start (the 28 Oct restart loop).
        self._barge_in_pending = False
        self._barge_in_started_at = 0.0
        # Why a turn task was cancelled, by task id: "barge_in" | "superseded" |
        # "cancelled". Read by _reply's CancelledError handler for turn_timings.
        self._cancel_reasons: dict[int, str] = {}

    # -- lifecycle ---------------------------------------------------------------
    def event(self, kind: str, detail: str = "") -> None:
        """Add a timeline entry for the debug view (seconds since the call started)."""
        if len(self.timeline) < 500:
            self.timeline.append({"t": round(time.time() - self.started, 2), "kind": kind,
                                  "detail": str(detail)[:200]})

    async def start(self) -> None:
        self._watchdog = asyncio.create_task(self._watch())
        self.event("answered")
        if self.amd is not None:
            self.amd.start(time.time())
        if self.listen_first and self.greeting:
            self._awaiting_hello = True
            self.event("listening", "waiting for the callee to speak first")
            self._hello_task = asyncio.create_task(self._hello_timeout())
            return
        self._play_greeting()

    def _play_greeting(self) -> None:
        self._awaiting_hello = False
        if not self.greeting or self.finished.is_set():
            return
        self.event("greeting")
        self._record("agent", self.greeting)
        self._greeting_disclosure_done = False
        self._greeting_task = self._turn_task = asyncio.create_task(self._greet())
        self._greeting_task.add_done_callback(
            lambda t: self.event("greeting_end", "stopped" if t.cancelled() else ""))

    async def _greet(self) -> None:
        """Speak the greeting in two parts so the disclosure is never cut off but
        the rest is interruptible: the disclosure sentence(s) first, then the
        remainder. ``_greeting_disclosure_done`` flips only once the disclosure
        has actually been spoken."""
        disclosure, rest = split_greeting_disclosure(self.greeting)
        await self._say(disclosure)
        self._greeting_disclosure_done = True
        if rest:
            await self._say(rest)

    def _barge_in_blocked(self) -> bool:
        """True while the greeting's disclosure is still playing: it is never cut off."""
        return self._greeting_playing() and not self._greeting_disclosure_done

    async def _hello_timeout(self) -> None:
        """Nobody spoke: greet after ``hello_wait_s`` (or once they stop talking)."""
        await asyncio.sleep(self.hello_wait_s)
        limit = time.time() + 6.0
        while self.vad.in_speech and time.time() < limit and not self.finished.is_set():
            await asyncio.sleep(0.05)          # they're talking: let them finish first
        if self._awaiting_hello and not self.finished.is_set():
            self._play_greeting()

    async def _hello_heard(self, pcm8k: bytes, stream: Any = None) -> None:
        """The callee's first words ("Hello?"): greet straight away, then transcribe them
        for the record and for the voicemail check."""
        self._play_greeting()
        text = ""
        try:
            text = await stream.finish() if stream is not None else ""
        except Exception:  # noqa: BLE001 - the batch path below
            text = ""
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.aclose()
        if not text:
            with contextlib.suppress(Exception):
                text = await self.stt.transcribe(resample(pcm8k, 8000, 16000))
        text = (text or "").strip()
        if text:
            self._record("caller", text)
            if hasattr(self.brain, "heard"):
                self.brain.heard(text)
        if self.amd is not None and text and self.amd.text(text, time.time()):
            await self._machine(self.amd.reason)

    async def _machine(self, reason: str) -> None:
        """An answering machine: hang up without leaving a message (outcome voicemail)."""
        if self.finished.is_set():
            return
        self.event("voicemail", reason)
        _log.info("answering machine detected (%s): hanging up", reason)
        st = self.brain.state
        st.outcome = "voicemail"
        st.summary = st.summary or f"Answering machine ({reason}); no message left."
        self._awaiting_hello = False
        for task in (self._hello_task, self._greeting_task, self._turn_task):
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._speaking = False
        if self.clear_audio:
            with contextlib.suppress(Exception):
                await self.clear_audio()
        await self._hangup("answering machine")

    async def machine_detected(self, reason: str) -> None:
        """The carrier's own detection (Telnyx) said machine."""
        await self._machine(reason)

    async def stop(self) -> None:
        if not self.finished.is_set() or not any(e["kind"] == "ended" for e in self.timeline):
            self.event("ended")
        self.finished.set()
        for task in (self._turn_task, self._watchdog, self._transfer_timer, self._dtmf_timer,
                     self._hello_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    @property
    def turn_in_progress(self) -> bool:
        """The agent is working on (or speaking) a reply."""
        return self._turn_task is not None and not self._turn_task.done()

    async def wait_idle(self, timeout: float = 20.0) -> None:
        """Wait (bounded) until the current reply has been fully spoken."""
        task = self._turn_task
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    @property
    def duration_s(self) -> float:
        return time.time() - self.started

    def cost_estimate(self) -> float:
        """Live estimate (dollars): minutes × per-minute rate + voice characters + model tokens."""
        r = self.cost_rates
        tokens = getattr(self.brain, "tokens", None) or {}
        return round(self.duration_s / 60 * float(r.get("per_minute", 0))
                     + self.tts_chars / 1000 * float(r.get("tts_per_1k_chars", 0))
                     + int(tokens.get("input", 0)) / 1e6 * float(r.get("llm_input_per_m", 0))
                     + int(tokens.get("output", 0)) / 1e6 * float(r.get("llm_output_per_m", 0)), 4)

    def _record(self, speaker: str, text: str) -> None:
        if text.strip():
            self.event("caller" if speaker == "caller" else "agent", text.strip()[:80])
        if speaker == "agent" and text.strip():
            self._last_agent_line = text.strip()
        if self.on_turn and text.strip():
            try:
                self.on_turn(speaker, text.strip())
            except Exception:  # noqa: BLE001 - logging a turn must never break the call
                _log.exception("on_turn failed")

    # -- inbound audio -----------------------------------------------------------
    async def on_audio(self, mulaw: bytes) -> None:
        if self.finished.is_set():
            return
        if self.recorder is not None:
            self.recorder.caller(mulaw)
        self._pending.extend(mulaw_to_pcm16(mulaw))
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            event = self.vad.feed(frame)
            if self.amd is not None and not self.amd.done and \
                    self.amd.frame(frame, self.vad.in_speech):
                await self._machine(self.amd.reason)
                return
            if self._stream is not None and event != "speech_start":
                if self.vad.in_speech:
                    self._stream.feed(frame)
                else:                                  # a click or cough, not speech
                    self._drop_stream()
            if event == "speech_start":
                self._last_activity = time.time()
                self._idle_prompts = 0
                self._open_stream()
                if (self._speaking or self.turn_in_progress) and not self._barge_in_blocked():
                    # Do NOT cancel yet: wait for speech_end + STT, then decide filler
                    # vs real interruption in _decide_barge_in. Cancelling here is
                    # the 28 Oct restart loop (every "Hello?" rebuilt the reply).
                    self._barge_in_pending = True
                    self._barge_in_started_at = time.time()
            elif event == "speech_end":
                self._speech_end_at = time.time()
                stream, self._stream = self._stream, None
                if self._awaiting_hello:          # "Hello?": our cue to greet, not a question
                    self._awaiting_hello = False
                    if self._hello_task is not None and not self._hello_task.done():
                        self._hello_task.cancel()
                    self._hello_task = asyncio.create_task(self._hello_heard(self.vad.take(), stream))
                    continue
                pcm8k = self.vad.take()
                if self._barge_in_pending:
                    self._barge_in_pending = False
                    if not self._speaking and not self.turn_in_progress:
                        # The reply finished while the caller spoke: a normal turn.
                        await self._begin_turn(pcm8k, stream)
                    else:
                        await self._decide_barge_in(pcm8k, stream)
                    continue
                await self._begin_turn(pcm8k, stream)

    def _open_stream(self) -> None:
        """Start streaming this utterance (with the audio the VAD already holds)."""
        self._drop_stream()
        if self.stream_stt is None:
            return
        try:
            self._stream = self.stream_stt.new_stream()
            self._stream.start()
            self._stream.feed(bytes(self.vad._buf))
        except Exception:  # noqa: BLE001 - the batch path still has the audio
            _log.exception("could not open the speech-to-text stream")
            self._stream = None

    def _drop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            asyncio.create_task(stream.aclose())

    async def _barge_in(self) -> None:
        """Cancel the reply in progress for a REAL interruption (never the greeting
        disclosure — see _barge_in_blocked). Only called once the utterance has
        been classified in _decide_barge_in, never on speech_start."""
        if self._barge_in_blocked():
            return          # the disclosure is never cut off; the caller is answered after it
        _log.info("barge-in: caller interrupted the agent")
        self.event("barge_in")
        if self._turn_task and not self._turn_task.done():
            self._cancel_reasons[id(self._turn_task)] = "barge_in"
            self._turn_task.cancel()
        self._speaking = False
        if self.clear_audio:
            with contextlib.suppress(Exception):
                await self.clear_audio()

    def _greeting_playing(self) -> bool:
        return self._greeting_task is not None and not self._greeting_task.done()

    def _cancel_turn(self) -> None:
        """Stop the reply in progress (but never the greeting)."""
        if self._turn_task and not self._turn_task.done() and self._turn_task is not self._greeting_task:
            # Keep an earlier "barge_in" reason: the reply was killed by the caller,
            # not merely superseded by the next turn.
            self._cancel_reasons.setdefault(id(self._turn_task), "superseded")
            self._turn_task.cancel()

    async def _decide_barge_in(self, pcm8k: bytes, stream: Any = None) -> None:
        """The caller spoke while the agent was replying. Transcribe the finished
        utterance, then: filler -> let the reply finish, no new turn; real
        interruption -> cancel the reply (as before) and answer the new utterance
        with the text already transcribed, so STT is not run twice."""
        speech_s = len(pcm8k) / 16000.0
        try:
            text, info = await self._transcribe(pcm8k, stream)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - undecidable: never cancel on a guess
            _log.warning("barge-in undecided (speech-to-text failed): letting the reply finish")
            self.event("barge_in_undecided")
            return
        text = (text or "").strip()
        if self.amd is not None and not self.amd.done and self.amd.text(text, time.time()):
            await self._machine(self.amd.reason)
            return
        if is_filler_utterance(text, speech_s):
            _log.info("filler ignored (%.2fs %r): reply keeps playing", speech_s, text)
            self.event("filler_ignored", f"{speech_s:.2f}s {text}"[:200])
            if text:
                self._record("caller", text)
            return
        _log.info("real interruption (%.2fs %r): cancelling the reply", speech_s, text)
        await self._barge_in()
        await self._begin_turn(pcm8k, None, text=text, info=info)

    async def _transcribe(self, pcm8k: bytes, stream: Any = None) -> tuple[str, dict[str, Any]]:
        """Transcribe one utterance: streaming STT first, batch STT as fallback.
        Returns (text, info). Raises on batch failure (the caller decides how to
        recover); a streaming failure falls back to the batch path."""
        info: dict[str, Any] = {"stt": label_of(self.stt), "fallbacks": [], "error": ""}
        text: str | None = None
        if stream is not None:
            try:
                text = await stream.finish()
                info["stt"] = label_of(self.stream_stt)
            except asyncio.CancelledError:
                await stream.aclose()
                raise
            except Exception as exc:  # noqa: BLE001 - fall back to the buffered audio below
                _log.warning("streaming speech-to-text failed (%s): transcribing the recording", exc)
                info["fallbacks"].append("stt:stream->batch")
                info["error"] = f"stt stream: {exc}"[:300]
            await stream.aclose()
        if text is None:
            text = await self.stt.transcribe(resample(pcm8k, 8000, 16000))
            last = getattr(self.stt, "last", None) or {}
            if last:
                info["stt"] = last.get("provider") or info["stt"]
                if last.get("fallback"):
                    info["fallbacks"].append("stt:fallback")
                if last.get("error") and not info["error"]:
                    info["error"] = f"stt: {last['error']}"
        return text or "", info

    async def _after_greeting(self) -> None:
        if self._greeting_playing():
            try:
                await asyncio.shield(self._greeting_task)
            except asyncio.CancelledError:
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise   # this turn itself is being cancelled: propagate
                # The greeting was interrupted after its disclosure: proceed.
            self._speech_end_at = time.time()   # latency counts from when the agent may speak

    async def _then(self, coro) -> None:
        """Run a caller-triggered reply once the greeting has finished."""
        try:
            await self._after_greeting()
        except BaseException:
            coro.close()
            raise
        await coro

    async def _begin_turn(self, pcm8k: bytes, stream: Any = None, text: str | None = None,
                          info: dict[str, Any] | None = None) -> None:
        self._cancel_turn()
        self._turn_task = asyncio.create_task(self._turn(pcm8k, stream, text=text, info=info))

    # -- one turn ----------------------------------------------------------------
    async def _turn(self, pcm8k: bytes, stream: Any = None, text: str | None = None,
                    info: dict[str, Any] | None = None) -> None:
        _t0 = time.time()
        if text is None:
            # Fresh utterance: transcribe it (streaming first, batch fallback).
            try:
                text, info = await self._transcribe(pcm8k, stream)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad turn must not drop the call
                _log.exception("speech-to-text failed")
                self.turn_timings.append({"stt_ms": round((time.time() - _t0) * 1000, 1),
                                          "stt": label_of(self.stt), "fallbacks": [],
                                          "error": f"stt: {type(exc).__name__}: {exc}"[:300]})
                await self._after_greeting()
                self._record("agent", "Sorry, I had a problem. Could you say that again?")
                await self._say("Sorry, I had a problem. Could you say that again?")
                return
            self._last_stt_ms = (time.time() - _t0) * 1000
            self._turn_info = info or {}
        else:
            # Already transcribed (real interruption decided in _decide_barge_in):
            # do not run STT twice.
            self._last_stt_ms = 0.0
            self._turn_info = info or {"stt": "text", "fallbacks": [], "error": ""}
        info = self._turn_info
        if self.amd is not None and not self.amd.done and self.amd.text(text or "", time.time()):
            await self._machine(self.amd.reason)
            return
        await self._after_greeting()
        await self._reply((text or "").strip())

    async def on_text(self, text: str) -> None:
        """A caller turn that is already text (browser test calls)."""
        if self.finished.is_set() or not text.strip():
            return
        self._speech_end_at = time.time()
        if self._awaiting_hello:                 # typed before the opening line: say it first
            if self._hello_task is not None and not self._hello_task.done():
                self._hello_task.cancel()
            self._play_greeting()
        if self._speaking:
            await self._barge_in()
        self._run_turn(self._reply(text.strip()))

    async def _reply(self, text: str, record_as: str | None = None) -> None:
        reply_parts: list[str] = []
        recorded = False
        try:
            if len(text) < 2 and record_as is None:
                return
            self._last_activity = time.time()
            self._record("caller", record_as or text)
            if self.amd is not None:
                self.amd.conversation()          # a person is talking with the agent
            if getattr(self.brain, "paused", False):
                return              # a person is handling the call
            first = True
            first_ms = 0.0          # this turn's own first-word delay (0 = it didn't speak)
            info, self._turn_info = self._turn_info, {}
            reply_start = time.time()
            # Snapshot: a mid-reply filler overwrites _speech_end_at while this
            # sentence is still playing, which would corrupt total_ms below.
            speech_end_at = self._speech_end_at
            stage = {"llm_first_sentence_ms": 0.0, "tts_first_audio_ms": 0.0, "total_ms": 0.0}

            async def sentences():
                nonlocal first
                async for sentence in self.brain.respond(text):
                    reply_parts.append(sentence)
                    yield sentence

            try:
                async for sentence in sentences():
                    if first:
                        first = False
                        sentence_at = time.time()
                        first_ms = (sentence_at - self._speech_end_at) * 1000
                        self.latencies_ms.append(first_ms)
                        stage["llm_first_sentence_ms"] = (sentence_at - reply_start) * 1000
                        self._say_first_frame_at = 0.0
                        await self._say(sentence)
                        if self._say_first_frame_at:
                            stage["tts_first_audio_ms"] = (self._say_first_frame_at - sentence_at) * 1000
                            stage["total_ms"] = (self._say_first_frame_at - speech_end_at) * 1000
                        continue
                    await self._say(sentence)
            except asyncio.CancelledError:
                if reply_parts:     # interrupted: keep what the caller already heard
                    self._record("agent", " ".join(reply_parts) + " …")
                # Record the interruption so the fix can be measured: a turn killed
                # by a real barge-in vs one superseded by the next turn vs other.
                reason = self._cancel_reasons.pop(id(asyncio.current_task()), "cancelled")
                self.turn_timings.append({
                    "stt_ms": round(self._last_stt_ms, 1),
                    **{k: round(v, 1) for k, v in stage.items()},
                    "llm_first_ms": round(first_ms, 1),
                    "stt": info.get("stt", ""),
                    "llm": llm_label(self.brain.provider),
                    "tts": label_of(self.tts),
                    "fallbacks": [], "error": "",
                    "cancelled_by": reason,
                })
                raise
            if reply_parts:
                self._record("agent", " ".join(reply_parts))
                recorded = True
            tts_last = getattr(self.tts, "last", None) or {}
            fallbacks = list(info.get("fallbacks") or [])
            if tts_last.get("fallback"):
                fallbacks.append("tts:fallback")
            if getattr(self.brain.provider, "used_backup", False):
                fallbacks.append("llm:backup")
            error = info.get("error") or (f"tts: {tts_last['error']}" if tts_last.get("error") else "")
            self.turn_timings.append({
                "stt_ms": round(self._last_stt_ms, 1),
                **{k: round(v, 1) for k, v in stage.items()},
                "llm_first_ms": round(first_ms, 1),      # end of speech -> first sentence (older name)
                "stt": info.get("stt", "") or ("text" if not self._last_stt_ms else ""),
                "llm": llm_label(self.brain.provider),
                "tts": tts_last.get("provider") or label_of(self.tts),
                "fallbacks": fallbacks, "error": error,
            })
            self._last_stt_ms = 0.0
            st = self.brain.state
            if getattr(st, "dtmf_out", "") and self.send_dtmf is not None:
                digits, st.dtmf_out = st.dtmf_out, ""
                self.event("dtmf_out", digits)
                try:
                    await self.send_dtmf(digits)
                except Exception:  # noqa: BLE001 - a failed key press must not drop the call
                    _log.exception("sending DTMF %s failed", digits)
            if self.on_events:
                with contextlib.suppress(Exception):
                    await self.on_events(self.brain.state.events[self._events_seen:])
            self._events_seen = len(self.brain.state.events)
            if st.end and not reply_parts and not st.transfer:
                # The model ended the call without a word: never hang up on someone silently.
                line = closing_line(st)
                self._record("agent", line)
                await self._say(line)
            await self._after_turn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad turn must not drop the call
            _log.exception("turn failed")
            if reply_parts and not recorded:     # e.g. the voice failed: keep what the agent said
                self._record("agent", " ".join(reply_parts))
            self.turn_timings.append({"stt_ms": round(self._last_stt_ms, 1),
                                      "llm": llm_label(self.brain.provider),
                                      "tts": label_of(self.tts),
                                      "error": f"{type(exc).__name__}: {exc}"[:300]})
            self._last_stt_ms = 0.0
            line = "Sorry, I had a problem. Could you say that again?"
            st = self.brain.state
            if getattr(st, "callback_at", "") and not reply_parts and "booked" not in self._last_agent_line:
                # The booking went through before the failure: confirm it rather than ask again.
                from datetime import datetime

                from xplogent.voip.callbacks import spoken

                line = f"I've booked your callback for {spoken(datetime.fromisoformat(st.callback_at))}."
            self._record("agent", line)
            with contextlib.suppress(Exception):
                await self._say(line)

    # -- keypad (DTMF) ------------------------------------------------------------------
    async def on_dtmf(self, digit: str) -> None:
        """A key the caller pressed. Digits are collected until ``#`` or a pause of
        ``dtmf_gap_s``, then given to the agent as ``[keypad: 123]``. Keys listed in
        ``dtmf_keys`` act at once (``transfer``, ``repeat``)."""
        digit = str(digit).strip().upper()[:1]
        if not self.dtmf_enabled or self.finished.is_set() or not digit or digit not in "0123456789*#ABCD":
            return
        self._last_activity = time.time()
        self._idle_prompts = 0
        self.event("dtmf_in", digit)
        if self._speaking:
            await self._barge_in()
        action = self.dtmf_keys.get(digit) if not self._dtmf_buf else None
        if action:
            self._run_turn(self._quick_key(digit, action))
            return
        if digit == "#":
            self._flush_dtmf()
            return
        self._dtmf_buf += digit
        if self._dtmf_timer is not None and not self._dtmf_timer.done():
            self._dtmf_timer.cancel()
        self._dtmf_timer = asyncio.create_task(self._dtmf_after_gap())

    async def _dtmf_after_gap(self) -> None:
        await asyncio.sleep(self.dtmf_gap_s)
        self._flush_dtmf()

    def _flush_dtmf(self) -> None:
        digits, self._dtmf_buf = self._dtmf_buf, ""
        if self._dtmf_timer is not None and not self._dtmf_timer.done() \
                and self._dtmf_timer is not asyncio.current_task():
            self._dtmf_timer.cancel()
        if digits:
            self._speech_end_at = time.time()
            self._run_turn(self._reply(f"[keypad: {digits}]", record_as=f"(keypad: {' '.join(digits)})"))

    def _run_turn(self, coro) -> None:
        self._cancel_turn()
        self._turn_task = asyncio.create_task(self._then(coro))

    async def _quick_key(self, digit: str, action: str) -> None:
        self._record("caller", f"(keypad: {digit})")
        if action == "repeat" and self._last_agent_line:
            await self._say(self._last_agent_line)
        elif action == "transfer":
            st = self.brain.state
            st.transfer, st.transfer_reason = True, f"caller pressed {digit}"
            await self._after_turn()

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
        await self._hangup("hung up from the dashboard")

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
            await self._hangup("the agent ended the call")

    # -- transfers -------------------------------------------------------------------
    async def start_transfer(self, attempt: Callable[[], Awaitable[None]], reason: str = "") -> None:
        """Hand the caller over. The agent stays quiet while it is pending; the
        transport then calls :meth:`transfer_done` (the caller left us) or
        :meth:`transfer_failed` (the caller is still here). No answer within
        ``transfer_timeout_s`` counts as failed. ``attempt`` raising = failed now."""
        st = self.brain.state
        self.event("transfer", reason)
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
        self.event("transfer_done")
        self.transfer_pending = False
        self.finished.set()

    async def transfer_failed(self, why: str = "") -> None:
        """The caller is still with us: resume the agent and say so."""
        if not self.transfer_pending or self.finished.is_set():
            return
        self.event("transfer_failed", why)
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

    async def _hangup(self, reason: str = "") -> None:
        self.event("hangup", reason)
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
        self.tts_chars += len(text)
        loop = asyncio.get_running_loop()
        play_end = loop.time()  # when the carrier will have played everything sent

        async def push(frame: bytes) -> None:
            nonlocal play_end
            if self.realtime:
                ahead = play_end - loop.time()
                if ahead > self.lead_s:
                    await asyncio.sleep(ahead - self.lead_s)
            await self.send_audio(frame)
            if not self._say_first_frame_at:
                self._say_first_frame_at = time.time()
            if self.recorder is not None:
                self.recorder.agent(frame)
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
                await self._hangup("time limit reached")
                return
            if (not self._wrapped_up and self.max_call_s > 2 * self.wrap_up_s
                    and now - self.started > self.max_call_s - self.wrap_up_s):
                self._wrapped_up = True
                self.event("wrap_up", f"{int(self.wrap_up_s)} s left")
                self.brain.instruct("About one minute left on this call: wrap up now. Briefly sum up, "
                                    "offer a recruiter callback for anything still open, then say goodbye.")
            if self.cost_cap and self.cost_estimate() >= self.cost_cap:
                self.event("cost_cap", f"${self.cost_estimate():.2f} of ${self.cost_cap:.2f}")
                st = self.brain.state
                st.outcome = st.outcome or "screen_incomplete"
                st.exit_path = st.exit_path or "cost_cap"
                await self._prompt("I'm sorry, I need to end our call now. A recruiter will follow up "
                                   "with you. Goodbye.", interrupt=True, wait=True)
                await self._hangup("cost cap reached")
                return
            since = getattr(self.brain.state, "closing_since", 0.0)
            if since and now - since > self.quick_close_s and not self._speaking \
                    and not (self._turn_task is not None and not self._turn_task.done()):
                self.event("quick_close", f"{int(now - since)} s after the call's purpose ended")
                await self._prompt("Thanks for your time. Goodbye.", wait=True)
                await self._hangup("quick close")
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
                await self._hangup("caller silent")
                return
            if self._idle_prompts < self.idle_max_prompts:
                step = (self.idle_hangup_s - self.idle_prompt_s) / max(1, self.idle_max_prompts)
                if idle > self.idle_prompt_s + self._idle_prompts * step:
                    text = SILENCE_PROMPTS[min(self._idle_prompts, len(SILENCE_PROMPTS) - 1)]
                    self._idle_prompts += 1
                    await self._prompt(text)
