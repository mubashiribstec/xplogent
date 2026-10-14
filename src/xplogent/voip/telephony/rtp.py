"""RTP media for one SIP call: 20 ms G.711 frames out on a clock, frames in to a callback."""

from __future__ import annotations

import asyncio
import contextlib
import random
import struct
from collections import deque
from collections.abc import Awaitable, Callable

from xplogent.core.logging import get_logger
from xplogent.voip.audio import alaw_to_mulaw, mulaw_to_alaw
from xplogent.voip.telephony.sip_proto import PCMA, PCMU

DTMF_CODES = {**{str(i): i for i in range(10)}, "*": 10, "#": 11, "A": 12, "B": 13, "C": 14, "D": 15}
DTMF_EVENTS = {code: key for key, code in DTMF_CODES.items()}

_log = get_logger("voip.rtp")


# ── RTP ──────────────────────────────────────────────────────────────────────
FRAME = 160          # 20 ms of 8 kHz G.711
SILENCE = {PCMU: b"\xff" * FRAME, PCMA: b"\xd5" * FRAME}


class PortPool:
    """Even RTP ports from a range (RTCP would use the odd one above)."""

    def __init__(self, start: int, end: int) -> None:
        self.ports = deque(range(start + (start % 2), end + 1, 2))
        self.used: set[int] = set()

    def take(self) -> int:
        for _ in range(len(self.ports)):
            port = self.ports.popleft()
            self.ports.append(port)
            if port not in self.used:
                self.used.add(port)
                return port
        raise RuntimeError("no free RTP port (raise the RTP port range)")

    def give(self, port: int) -> None:
        self.used.discard(port)


class RtpStream(asyncio.DatagramProtocol):
    """One call's RTP: a 20 ms sender clock and a receiver feeding μ-law to a callback."""

    def __init__(self, payload_type: int = PCMU, on_audio: Callable[[bytes], Awaitable[None]] | None = None,
                 dtmf_pt: int | None = None) -> None:
        self.pt = payload_type
        self.on_audio = on_audio
        # RFC 4733 telephone-events: payload type agreed in SDP (None = not supported).
        self.dtmf_pt = dtmf_pt
        self.on_dtmf: Callable[[str], Awaitable[None]] | None = None
        self._dtmf_seen: deque[int] = deque(maxlen=16)      # event timestamps already reported
        self._dtmf_out: deque[tuple[int, bool, int, bytes]] = deque()   # (pt, marker, ts, payload)
        self.remote: tuple[str, int] | None = None
        self.latched = False
        self.transport: asyncio.DatagramTransport | None = None
        self.port = 0
        self.out: deque[bytes] = deque()
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self.received = 0
        self.sent = 0
        self._inbox: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._marker = True

    async def open(self, port: int, host: str = "0.0.0.0") -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, local_addr=(host, port))
        self.port = port

    def connection_made(self, transport) -> None:
        self.transport = transport

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._send_loop()), asyncio.create_task(self._recv_loop())]

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 12 or data[0] >> 6 != 2:
            return
        if not self.latched:                     # symmetric RTP: answer where it comes from
            self.remote, self.latched = (addr[0], addr[1]), True
        pt = data[1] & 0x7F
        if self.dtmf_pt is not None and pt == self.dtmf_pt:
            self._telephone_event(data)
            return
        if pt not in (PCMU, PCMA):
            return                                # comfort noise / unknown
        cc = data[0] & 0x0F
        offset = 12 + cc * 4
        if data[0] & 0x10 and len(data) >= offset + 4:      # header extension
            offset += 4 + struct.unpack("!H", data[offset + 2:offset + 4])[0] * 4
        payload = data[offset:]
        if data[0] & 0x20 and payload:                       # padding
            payload = payload[: -payload[-1]]
        self.received += 1
        self._inbox.put_nowait(alaw_to_mulaw(payload) if pt == PCMA else payload)

    def _telephone_event(self, data: bytes) -> None:
        """RFC 4733: report each key once, when its end packet arrives (sent 3x)."""
        cc = data[0] & 0x0F
        payload = data[12 + cc * 4:]
        if len(payload) < 4 or not payload[1] & 0x80:
            return
        ts = struct.unpack("!I", data[4:8])[0]
        if ts in self._dtmf_seen:
            return
        self._dtmf_seen.append(ts)
        digit = DTMF_EVENTS.get(payload[0])
        if digit:
            self._inbox.put_nowait(digit)

    def queue_dtmf(self, digits: str, duration_ms: int = 160, gap_ms: int = 80) -> None:
        """Send keys as RFC 4733 events (in place of audio frames while they play)."""
        if self.dtmf_pt is None:
            raise RuntimeError("the other side did not agree to keypad events (telephone-event)")
        steps = max(1, duration_ms // 20)
        for d in digits:
            code = DTMF_CODES.get(d.upper())
            if code is None:
                continue
            for i in range(1, steps + 1):
                body = struct.pack("!BBH", code, 10, i * FRAME)
                self._dtmf_out.append((self.dtmf_pt, i == 1, -1, body))
            end = struct.pack("!BBH", code, 0x80 | 10, steps * FRAME)
            for _ in range(3):
                self._dtmf_out.append((self.dtmf_pt, False, -1, end))
            self._dtmf_out.append((-1, False, steps * FRAME, b""))      # advance the clock
            for _ in range(max(1, gap_ms // 20)):
                self._dtmf_out.append((self.pt, False, 0, b""))         # a little silence

    async def _recv_loop(self) -> None:
        # Many PBXs/carriers stop sending RTP during silence (Asterisk while it waits,
        # silence suppression, comfort noise). Fill those gaps with silence at the
        # 20 ms clock, otherwise the end of the caller's sentence is never detected.
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            try:
                chunk = await asyncio.wait_for(self._inbox.get(), timeout=0.1)
            except TimeoutError:
                if not self.latched:
                    continue
                gap = loop.time() - last
                frames = int(gap / 0.02)
                if frames <= 0:
                    continue
                chunk = SILENCE[PCMU] * min(frames, 50)
                last += frames * 0.02
            else:
                if chunk is None:
                    return
                if isinstance(chunk, str):                       # a keypad key
                    if self.on_dtmf is not None:
                        try:
                            await self.on_dtmf(chunk)
                        except Exception:  # noqa: BLE001
                            _log.exception("on_dtmf failed")
                    continue
                last = max(last, loop.time() - 0.02)
                last += len(chunk) / 8000.0
            if self.on_audio is not None:
                try:
                    await self.on_audio(chunk)
                except Exception:  # noqa: BLE001 - never stop the media loop
                    _log.exception("on_audio failed")

    async def _send_loop(self) -> None:
        loop = asyncio.get_running_loop()
        nxt = loop.time()
        while True:
            nxt += 0.02
            await asyncio.sleep(max(0.0, nxt - loop.time()))
            if loop.time() - nxt > 0.2:            # fell far behind (busy CPU): resync
                nxt = loop.time()
            if self._dtmf_out:                      # keypad events replace audio meanwhile
                pt, marker, val, body = self._dtmf_out.popleft()
                if pt == -1:                        # event over: move the clock past it
                    self.ts = (self.ts + val) & 0xFFFFFFFF
                    pt, marker, val, body = self._dtmf_out.popleft() if self._dtmf_out else (self.pt, False, 0, b"")
                if body:
                    self._send_event(pt, marker, body)
                else:
                    self._send(SILENCE[self.pt])
                continue
            frame = self.out.popleft() if self.out else None
            payload = SILENCE[self.pt] if frame is None else (
                mulaw_to_alaw(frame) if self.pt == PCMA else frame)
            if len(payload) < FRAME:
                payload += SILENCE[self.pt][: FRAME - len(payload)]
            self._send(payload, marker=self._marker and frame is not None)
            if frame is not None:
                self._marker = False
            elif not self.out:
                self._marker = True

    def _send_event(self, pt: int, marker: bool, payload: bytes) -> None:
        """A telephone-event packet: all packets of one key share its start timestamp."""
        if self.transport is None or self.remote is None:
            return
        header = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | pt, self.seq, self.ts, self.ssrc)
        self.transport.sendto(header + payload, self.remote)
        self.seq = (self.seq + 1) & 0xFFFF
        self.sent += 1

    def _send(self, payload: bytes, marker: bool = False) -> None:
        if self.transport is None or self.remote is None:
            return
        header = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | self.pt, self.seq,
                             self.ts, self.ssrc)
        self.transport.sendto(header + payload, self.remote)
        self.seq = (self.seq + 1) & 0xFFFF
        self.ts = (self.ts + FRAME) & 0xFFFFFFFF
        self.sent += 1

    async def send_audio(self, mulaw: bytes) -> None:
        for i in range(0, len(mulaw), FRAME):
            self.out.append(mulaw[i:i + FRAME])

    async def clear(self) -> None:
        self.out.clear()

    async def close(self) -> None:
        self._inbox.put_nowait(None)
        for t in self._tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.transport is not None:
            self.transport.close()
