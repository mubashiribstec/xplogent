"""Call recording: one stereo 8 kHz WAV per call (caller left, agent right).

Both sides are placed on the call's clock, so the file plays back like the real
conversation: the caller's stream is appended as it arrives (gaps are padded), and
each agent frame is written where it was sent. Works for every line (Telnyx, SIP,
browser test) because it hooks the call engine, not the carrier.

Files live in ``data/voip/recordings/<call_id>.wav``; ``voip.recording.retention_days``
old ones are deleted when a later call ends.
"""

from __future__ import annotations

import contextlib
import time
import wave
from array import array
from pathlib import Path

from xplogent.core.config import data_dir
from xplogent.core.logging import get_logger
from xplogent.voip.audio import mulaw_to_pcm16

_log = get_logger("voip.recording")
RATE = 8000


def recordings_dir() -> Path:
    path = data_dir() / "voip" / "recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


class CallRecorder:
    def __init__(self, path: Path, max_seconds: float = 3600.0, clock=time.monotonic) -> None:
        self.path = path
        self.max_samples = int(max_seconds * RATE)
        self.clock = clock
        self.start = clock()
        self.left = array("h")      # caller
        self.right = array("h")     # agent
        self.closed = False

    def _now(self) -> int:
        return int((self.clock() - self.start) * RATE)

    def _place(self, track: array, pcm16: bytes, at: int) -> None:
        samples = array("h")
        samples.frombytes(pcm16[: len(pcm16) - len(pcm16) % 2])
        at = max(0, min(at, self.max_samples))
        room = self.max_samples - at
        if room <= 0 or not samples:
            return
        if len(samples) > room:
            samples = samples[:room]
        end = at + len(samples)
        if len(track) < end:                       # silence up to here, then the new audio
            track.extend(array("h", bytes(2 * (end - len(track)))))
        track[at:end] = samples

    def caller(self, mulaw: bytes) -> None:
        """Audio from the caller (continuous stream; pad if it fell behind the clock)."""
        if self.closed or not mulaw:
            return
        at = len(self.left)
        if self._now() - at > RATE // 2:          # stream stalled: realign with the clock
            at = self._now()
        self._place(self.left, mulaw_to_pcm16(mulaw), at)

    def agent(self, mulaw: bytes) -> None:
        """A frame the agent sent, placed at the moment it was sent."""
        if self.closed or not mulaw:
            return
        self._place(self.right, mulaw_to_pcm16(mulaw), max(len(self.right), self._now()))

    def close(self) -> Path | None:
        """Write the WAV; returns its path (None if nothing was heard or said)."""
        if self.closed:
            return None
        self.closed = True
        n = max(len(self.left), len(self.right))
        if n == 0:
            return None
        for track in (self.left, self.right):
            if len(track) < n:
                track.extend(array("h", bytes(2 * (n - len(track)))))
        stereo = array("h", bytes(4 * n))
        stereo[0::2], stereo[1::2] = self.left, self.right
        try:
            with wave.open(str(self.path), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(stereo.tobytes())
        except OSError as exc:
            _log.warning("could not save the recording %s: %s", self.path, exc)
            return None
        return self.path


def prune(retention_days: float) -> int:
    """Delete recordings older than ``retention_days`` (0 = keep forever)."""
    if not retention_days or retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for f in recordings_dir().glob("*.wav"):
        with contextlib.suppress(OSError):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
    return removed
