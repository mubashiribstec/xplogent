"""Answering-machine detection for outbound calls, done in the engine so it works on
every line (SIP trunk, PBX extension; Telnyx also has the carrier's own).

In the first seconds after answer, any of these means a machine:
- the callee speaks without a pause for longer than ``long_speech_s`` (people say
  "Hello?"; voicemail greetings run on);
- speech-to-text hears a voicemail phrase ("leave a message", "after the tone"…);
- a beep: a steady 800–1,200 Hz tone held for 250 ms (a Goertzel filter bank).

On a machine the call ends silently with outcome ``voicemail``: no message is left.
"""

from __future__ import annotations

import math
import re
import struct

VOICEMAIL_PHRASES = re.compile(
    r"\b(leave (me |us )?(a |your )?(message|name and number)|after the (tone|beep)|at the (tone|beep)"
    r"|voice ?mail|mailbox|you('ve| have) reached|can'?t (take|get to|come to) (your|the|my) (call|phone)"
    r"|(is|am|are) (not|un)available|unable to (take|answer)|record your message|not able to take your call"
    r"|the person you (are|have|were) call(ing|ed)|please try (again )?later|has not been answered)\b",
    re.I)


def is_voicemail_text(text: str) -> bool:
    return bool(VOICEMAIL_PHRASES.search(text or ""))


class BeepDetector:
    """A steady tone between 800 and 1,200 Hz, held for ``min_ms`` (8 kHz 16-bit PCM)."""

    def __init__(self, rate: int = 8000, min_ms: int = 250, ratio: float = 0.45,
                 min_rms: float = 250.0, freqs: tuple[int, ...] = tuple(range(800, 1201, 50))) -> None:
        self.rate, self.ratio, self.min_rms = rate, ratio, min_rms
        self.coeffs = [2 * math.cos(2 * math.pi * f / rate) for f in freqs]
        self.need_ms, self.held_ms = min_ms, 0.0

    def tone_ratio(self, samples: tuple[int, ...]) -> float:
        n = len(samples)
        energy = sum(s * s for s in samples)
        if not n or energy <= 0:
            return 0.0
        best = 0.0
        for coeff in self.coeffs:
            s1 = s2 = 0.0
            for x in samples:
                s1, s2 = x + coeff * s1 - s2, s1
            best = max(best, s1 * s1 + s2 * s2 - coeff * s1 * s2)
        return best / (energy * n / 2)

    def feed(self, pcm16: bytes) -> bool:
        samples = struct.unpack(f"<{len(pcm16) // 2}h", pcm16[: len(pcm16) // 2 * 2])
        if not samples:
            return False
        loud = math.sqrt(sum(s * s for s in samples) / len(samples)) >= self.min_rms
        if loud and self.tone_ratio(samples) >= self.ratio:
            self.held_ms += 1000 * len(samples) / self.rate
        else:
            self.held_ms = 0.0
        return self.held_ms >= self.need_ms


class MachineDetector:
    """Watches the start of an outbound call; ``frame``/``text`` return a reason or "".

    Speech length is measured in audio received (20 ms per frame), not wall-clock time,
    so a burst of buffered audio is judged exactly like the same audio in real time."""

    def __init__(self, window_s: float = 6.0, long_speech_s: float = 4.0, beep: bool = True,
                 beep_window_s: float = 30.0) -> None:
        self.window_s, self.long_speech_s = window_s, long_speech_s
        self.beep = BeepDetector() if beep else None
        self.beep_window_s = beep_window_s
        self.answered_at = 0.0             # wall clock, for the speech-to-text check
        self.audio_s = 0.0                 # audio received since answer
        self._speech_from: float | None = None
        self.done = False                  # a conversation is under way (or the window passed)
        self.reason = ""

    def start(self, now: float) -> None:
        self.answered_at = now

    def frame(self, pcm16: bytes, in_speech: bool) -> str:
        if self.done or self.reason:
            return ""
        self.audio_s += len(pcm16) / 16000
        if self.audio_s > max(self.beep_window_s if self.beep else 0, self.window_s + self.long_speech_s):
            self.done = True
            return ""
        if in_speech:
            if self._speech_from is None:
                self._speech_from = self.audio_s
            if self._speech_from < self.window_s and self.audio_s - self._speech_from >= self.long_speech_s:
                self.reason = f"greeting ran {self.audio_s - self._speech_from:.1f} s without a pause"
                return self.reason
        else:
            self._speech_from = None
        if self.beep is not None and self.audio_s <= self.beep_window_s and self.beep.feed(pcm16):
            self.reason = "voicemail beep"
            return self.reason
        return ""

    def text(self, text: str, now: float) -> str:
        if self.done or self.reason or now - self.answered_at > self.window_s + self.long_speech_s + 6:
            return ""
        if is_voicemail_text(text):
            self.reason = f'voicemail greeting ("{text[:60]}")'
            return self.reason
        return ""

    def conversation(self) -> None:
        """A person answered and the agent replied: stop watching."""
        self.done = True
