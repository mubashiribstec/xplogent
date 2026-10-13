"""Telephony audio helpers (pure Python, no hard dependencies).

Phone carriers stream 8 kHz G.711 μ-law. Speech models want 16 kHz 16-bit PCM, and
TTS usually returns 16-24 kHz PCM. These helpers convert between them and provide a
small energy-based voice-activity detector (VAD) for end-of-utterance detection.
``audioop`` is used when available (removed in Python 3.13); otherwise a table-based
fallback keeps everything working.
"""

from __future__ import annotations

import struct
import warnings
from array import array

try:  # Python <= 3.12, or the audioop-lts backport
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop as _audioop
except ImportError:  # pragma: no cover - exercised only on 3.13 without audioop-lts
    try:
        import audioop_lts as _audioop  # type: ignore
    except ImportError:
        _audioop = None

_BIAS = 0x84
_CLIP = 32635


def _build_decode_table() -> list[int]:
    table = []
    for byte in range(256):
        b = ~byte & 0xFF
        sign = b & 0x80
        exponent = (b >> 4) & 0x07
        mantissa = b & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        table.append(-sample if sign else sample)
    return table


_DECODE = _build_decode_table()


def mulaw_to_pcm16(data: bytes) -> bytes:
    """Decode G.711 μ-law bytes to little-endian 16-bit PCM."""
    if _audioop is not None:
        return _audioop.ulaw2lin(data, 2)
    return struct.pack(f"<{len(data)}h", *(_DECODE[b] for b in data))


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def pcm16_to_mulaw(data: bytes) -> bytes:
    """Encode little-endian 16-bit PCM to G.711 μ-law bytes."""
    if _audioop is not None:
        return _audioop.lin2ulaw(data, 2)
    samples = array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    return bytes(_encode_sample(s) for s in samples)


def resample(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample mono 16-bit PCM between rates (linear interpolation fallback)."""
    if src_rate == dst_rate or not data:
        return data
    if _audioop is not None:
        out, _ = _audioop.ratecv(data, 2, 1, src_rate, dst_rate, None)
        return out
    src = array("h")
    src.frombytes(data[: len(data) - (len(data) % 2)])
    n_out = int(len(src) * dst_rate / src_rate)
    out = array("h")
    ratio = src_rate / dst_rate
    for i in range(n_out):
        pos = i * ratio
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j + 1 < len(src) else a
        out.append(int(a + (b - a) * frac))
    return out.tobytes()


class Resampler:
    """Streaming resampler for mono 16-bit PCM.

    TTS engines deliver audio in chunks; resampling each chunk on its own resets the
    filter/interpolation state at every boundary, which is audible as clicks. This
    keeps that state (and any odd trailing byte) across ``feed`` calls.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate, self.dst_rate = src_rate, dst_rate
        self._state = None          # audioop.ratecv state
        self._odd = b""             # half a sample left over from the last chunk
        self._prev: int | None = None  # fallback: last source sample
        self._t = 0.0               # fallback: next output position in source samples

    def feed(self, data: bytes) -> bytes:
        data = self._odd + data
        cut = len(data) - (len(data) % 2)
        data, self._odd = data[:cut], data[cut:]
        if not data or self.src_rate == self.dst_rate:
            return data
        if _audioop is not None:
            out, self._state = _audioop.ratecv(data, 2, 1, self.src_rate, self.dst_rate,
                                               self._state)
            return out
        buf = array("h")
        if self._prev is not None:
            buf.append(self._prev)
        buf.frombytes(data)
        step = self.src_rate / self.dst_rate
        out = array("h")
        t = self._t
        while t + 1 < len(buf):
            j = int(t)
            frac = t - j
            out.append(int(buf[j] + (buf[j + 1] - buf[j]) * frac))
            t += step
        # The last sample becomes index 0 of the next buffer.
        self._t = t - (len(buf) - 1)
        self._prev = buf[-1]
        return out.tobytes()


def rms(pcm16: bytes) -> float:
    """Root-mean-square level of 16-bit PCM (0..32768)."""
    if not pcm16:
        return 0.0
    if _audioop is not None:
        return float(_audioop.rms(pcm16, 2))
    samples = array("h")
    samples.frombytes(pcm16[: len(pcm16) - (len(pcm16) % 2)])
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


class VAD:
    """Energy-based utterance detector for 8 kHz/16-bit PCM frames.

    Feed fixed-size frames with :meth:`feed`. It reports ``"speech_start"`` when the
    caller begins talking (used for barge-in), and ``"speech_end"`` after a run of
    silence following speech, returning the whole utterance's audio via :meth:`take`.
    Simple on purpose: robust enough for phone audio, no ML dependency. Swap in
    Silero/webrtcvad later behind the same interface if you need more accuracy.
    """

    def __init__(self, threshold: float = 500.0, silence_ms: int = 700,
                 min_speech_ms: int = 200, frame_ms: int = 20, max_utterance_ms: int = 15000) -> None:
        self.threshold = threshold
        self.silence_frames = max(1, silence_ms // frame_ms)
        self.min_speech_frames = max(1, min_speech_ms // frame_ms)
        self.max_frames = max(1, max_utterance_ms // frame_ms)
        self._buf = bytearray()
        self._speech = 0
        self._silence = 0
        self._active = False
        self._announced = False

    @property
    def in_speech(self) -> bool:
        return self._active

    def feed(self, frame: bytes) -> str | None:
        loud = rms(frame) >= self.threshold
        if loud:
            self._buf.extend(frame)
            self._speech += 1
            self._silence = 0
            self._active = True
            if self._speech >= self.min_speech_frames and not self._announced:
                self._announced = True
                return "speech_start"
            if self._speech >= self.max_frames:
                return "speech_end"
            return None
        if not self._active:
            return None
        self._buf.extend(frame)  # keep trailing silence for natural word endings
        self._silence += 1
        if self._silence >= self.silence_frames:
            if self._speech >= self.min_speech_frames:
                return "speech_end"
            self.reset()  # a click or cough, not speech
        return None

    def take(self) -> bytes:
        """Return and clear the buffered utterance audio."""
        audio = bytes(self._buf)
        self.reset()
        return audio

    def reset(self) -> None:
        self._buf.clear()
        self._speech = self._silence = 0
        self._active = self._announced = False
