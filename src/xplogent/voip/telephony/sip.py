"""Built-in SIP trunk client: register with a PBX or carrier and handle calls over RTP.

A small SIP user agent (RFC 3261 subset) for one trunk, so the phone agent can work
with FreePBX/Asterisk, Telnyx SIP connections and other providers without the
Telnyx Voice API or a public HTTPS address:

* **REGISTER** with digest authentication (401/407), refreshed before it expires;
* **inbound calls**: answers an INVITE (100 Trying, 180 Ringing, 200 OK with an
  SDP answer), then ACK/BYE/CANCEL; OPTIONS keep-alives are answered;
* **outbound calls**: INVITE (with digest auth), ringing timeout → CANCEL;
* **transfer** with REFER (the PBX/carrier connects the caller to the number);
* **audio**: RTP over UDP, G.711 μ-law (PCMU) or A-law (PCMA), 20 ms frames,
  symmetric RTP (we reply to where the far end actually sends from, which helps
  behind NAT). DTMF and comfort-noise packets are ignored.

Each call is wired to the same :class:`~xplogent.voip.engine.CallSession` the
Telnyx path uses (8 kHz μ-law in and out), and registered for live control.

Limits: UDP only (no TCP/TLS/SRTP), G.711 only, one trunk.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import random
import re
import secrets
import socket
import struct
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from xplogent.core.logging import get_logger
from xplogent.voip.audio import alaw_to_mulaw, mulaw_to_alaw

_log = get_logger("voip.sip")

USER_AGENT = "Xplogent-SIP/1.0"
ALLOW = "INVITE, ACK, BYE, CANCEL, OPTIONS, NOTIFY, INFO, REFER"
T1 = 0.5

# ── messages ─────────────────────────────────────────────────────────────────
_COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
            "l": "content-length", "c": "content-type", "k": "supported", "s": "subject",
            "e": "content-encoding", "o": "event", "r": "refer-to", "b": "referred-by"}


def _norm(name: str) -> str:
    n = name.strip().lower()
    return _COMPACT.get(n, n)


@dataclass
class SipMessage:
    method: str = ""            # request method; "" for a response
    uri: str = ""
    status: int = 0
    reason: str = ""
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    @property
    def is_request(self) -> bool:
        return bool(self.method)

    def get(self, name: str, default: str = "") -> str:
        key = _norm(name)
        for k, v in self.headers:
            if _norm(k) == key:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        key = _norm(name)
        return [v for k, v in self.headers if _norm(k) == key]

    def set(self, name: str, value: str) -> None:
        key = _norm(name)
        for i, (k, _v) in enumerate(self.headers):
            if _norm(k) == key:
                self.headers[i] = (name, value)
                return
        self.headers.append((name, value))

    def remove(self, name: str) -> None:
        key = _norm(name)
        self.headers = [(k, v) for k, v in self.headers if _norm(k) != key]

    @property
    def cseq(self) -> tuple[int, str]:
        num, _, method = self.get("CSeq").strip().partition(" ")
        try:
            return int(num), method.strip().upper()
        except ValueError:
            return 0, method.strip().upper()

    @property
    def call_id(self) -> str:
        return self.get("Call-ID").strip()

    def encode(self) -> bytes:
        start = (f"{self.method} {self.uri} SIP/2.0" if self.is_request
                 else f"SIP/2.0 {self.status} {self.reason}")
        lines = [start] + [f"{k}: {v}" for k, v in self.headers if _norm(k) != "content-length"]
        lines.append(f"Content-Length: {len(self.body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + self.body


def parse_message(data: bytes) -> SipMessage:
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        head, _, body = data.partition(b"\n\n")
    lines = head.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
    start = lines[0].strip()
    msg = SipMessage()
    if start.startswith("SIP/2.0 "):
        _, code, *rest = start.split(" ", 2)
        msg.status, msg.reason = int(code), (rest[0] if rest else "")
    else:
        parts = start.split(" ")
        if len(parts) < 3 or not parts[2].startswith("SIP/"):
            raise ValueError("not a SIP message")
        msg.method, msg.uri = parts[0].upper(), parts[1]
    for line in lines[1:]:
        if not line.strip():
            continue
        if line[0] in " \t" and msg.headers:        # folded header line
            k, v = msg.headers[-1]
            msg.headers[-1] = (k, f"{v} {line.strip()}")
            continue
        name, _, value = line.partition(":")
        msg.headers.append((name.strip(), value.strip()))
    length = msg.get("Content-Length")
    msg.body = body[: int(length)] if length.strip().isdigit() else body
    return msg


def header_param(value: str, name: str) -> str:
    m = re.search(rf";\s*{re.escape(name)}\s*=\s*\"?([^;\",>\s]+)", value, re.I)
    return m.group(1) if m else ""


def uri_of(value: str) -> str:
    """``"Bob" <sip:bob@host>;tag=1`` → ``sip:bob@host``."""
    m = re.search(r"<([^>]+)>", value)
    return (m.group(1) if m else value.split(";")[0]).strip()


def user_of(value: str) -> str:
    uri = uri_of(value)
    m = re.match(r"(?:sips?|tel):([^@;>]+)", uri, re.I)
    return m.group(1) if m else ""


def host_port(uri: str, default_port: int = 5060) -> tuple[str, int]:
    uri = uri_of(uri)
    rest = re.sub(r"^(sips?):", "", uri, flags=re.I)
    rest = rest.split("@", 1)[-1].split(";", 1)[0].split("?", 1)[0]
    host, _, port = rest.partition(":")
    return host, int(port) if port.isdigit() else default_port


def new_branch() -> str:
    return "z9hG4bK" + secrets.token_hex(8)


def new_tag() -> str:
    return secrets.token_hex(5)


# ── digest authentication (RFC 2617 / 7616) ─────────────────────────────────
def parse_challenge(value: str) -> dict[str, str]:
    scheme, _, rest = value.strip().partition(" ")
    out = {"scheme": scheme}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', rest):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def digest_authorization(method: str, uri: str, username: str, password: str,
                         challenge: dict[str, str], *, cnonce: str | None = None,
                         nc: int = 1) -> str:
    algorithm = challenge.get("algorithm", "MD5")
    base = algorithm.upper().removesuffix("-SESS")
    hname = {"MD5": "md5", "SHA-256": "sha256", "SHA-512-256": "sha512_256"}.get(base, "md5")

    def h(s: str) -> str:
        return hashlib.new(hname, s.encode()).hexdigest()

    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    cnonce = cnonce or secrets.token_hex(8)
    ha1 = h(f"{username}:{realm}:{password}")
    if algorithm.upper().endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")
    ha2 = h(f"{method}:{uri}")
    qops = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in qops:
        ncs = f"{nc:08x}"
        resp = h(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
        parts += [f'response="{resp}"', f"algorithm={algorithm}", "qop=auth", f"nc={ncs}",
                  f'cnonce="{cnonce}"']
    else:
        resp = h(f"{ha1}:{nonce}:{ha2}")
        parts += [f'response="{resp}"', f"algorithm={algorithm}"]
    if challenge.get("opaque"):
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


# ── SDP ──────────────────────────────────────────────────────────────────────
PCMU, PCMA, DTMF_PT = 0, 8, 101
CODEC_NAMES = {PCMU: "PCMU", PCMA: "PCMA"}


def build_sdp(ip: str, port: int, payloads: tuple[int, ...] = (PCMU, PCMA),
              session_id: int | None = None) -> bytes:
    sid = session_id or random.randint(10**6, 10**9)
    fmt = " ".join(str(p) for p in (*payloads, DTMF_PT))
    lines = ["v=0", f"o=xplogent {sid} {sid} IN IP4 {ip}", "s=Xplogent", f"c=IN IP4 {ip}", "t=0 0",
             f"m=audio {port} RTP/AVP {fmt}"]
    lines += [f"a=rtpmap:{p} {CODEC_NAMES[p]}/8000" for p in payloads]
    lines += [f"a=rtpmap:{DTMF_PT} telephone-event/8000", f"a=fmtp:{DTMF_PT} 0-16", "a=ptime:20",
              "a=sendrecv"]
    return ("\r\n".join(lines) + "\r\n").encode()


def parse_sdp(body: bytes) -> dict[str, Any]:
    ip, port, payloads, names = "", 0, [], {}
    media_ip = ""
    in_audio = False
    for raw in body.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if line.startswith("m="):
            parts = line[2:].split()
            in_audio = parts[0] == "audio"
            if in_audio and not port:
                port = int(parts[1])
                payloads = [int(p) for p in parts[3:] if p.isdigit()]
        elif line.startswith("c=") and "IN IP4" in line:
            addr = line.split()[-1].split("/")[0]
            if in_audio:
                media_ip = addr
            elif not ip:
                ip = addr
        elif line.startswith("a=rtpmap:") and in_audio:
            pt, _, enc = line[9:].partition(" ")
            if pt.isdigit():
                names[int(pt)] = enc.split("/")[0].upper()
    return {"ip": media_ip or ip, "port": port, "payloads": payloads, "names": names}


def choose_codec(sdp: dict[str, Any]) -> int | None:
    """First of the far end's codecs we speak (keeps their preference order)."""
    for p in sdp.get("payloads", []):
        name = sdp.get("names", {}).get(p) or CODEC_NAMES.get(p, "")
        if name == "PCMU":
            return PCMU
        if name == "PCMA":
            return PCMA
    return None


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

    def __init__(self, payload_type: int = PCMU, on_audio: Callable[[bytes], Awaitable[None]] | None = None) -> None:
        self.pt = payload_type
        self.on_audio = on_audio
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
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
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
        if pt not in (PCMU, PCMA):
            return                                # DTMF / comfort noise
        cc = data[0] & 0x0F
        offset = 12 + cc * 4
        if data[0] & 0x10 and len(data) >= offset + 4:      # header extension
            offset += 4 + struct.unpack("!H", data[offset + 2:offset + 4])[0] * 4
        payload = data[offset:]
        if data[0] & 0x20 and payload:                       # padding
            payload = payload[: -payload[-1]]
        self.received += 1
        self._inbox.put_nowait(alaw_to_mulaw(payload) if pt == PCMA else payload)

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


# ── settings ─────────────────────────────────────────────────────────────────
@dataclass
class SipSettings:
    server: str
    username: str
    password: str = ""
    port: int = 5060
    auth_username: str = ""
    domain: str = ""
    caller_id: str = ""
    public_ip: str = ""
    local_port: int = 5060
    rtp_start: int = 10000
    rtp_end: int = 10100
    register: bool = True
    expires: int = 300
    allow_from: list[str] = field(default_factory=list)
    max_calls: int = 4
    ring_timeout: float = 45.0
    strict_source: bool = False          # accept calls only from the server / allow_from addresses

    @classmethod
    def from_config(cls, v: dict[str, Any]) -> SipSettings:
        s = dict(v.get("sip") or {})

        def num(key: str, env: str, default: int) -> int:
            # In Docker the published ports (env) are what is reachable: they win.
            raw = os.environ.get(env) or s.get(key) or default
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        allow = s.get("allow_from") or []
        if isinstance(allow, str):
            allow = [a.strip() for a in allow.split(",") if a.strip()]
        return cls(server=str(s.get("server") or "").strip(), username=str(s.get("username") or "").strip(),
                   password=os.environ.get("SIP_PASSWORD", ""), port=int(s.get("port") or 5060),
                   auth_username=str(s.get("auth_username") or "").strip(),
                   domain=str(s.get("domain") or "").strip(), caller_id=str(s.get("caller_id") or "").strip(),
                   public_ip=str(s.get("public_ip") or os.environ.get("XPLOGENT_HOST_IP") or "").strip(),
                   local_port=num("local_port", "XPLOGENT_SIP_PORT", 5060),
                   rtp_start=num("rtp_start", "XPLOGENT_RTP_START", 10000),
                   rtp_end=num("rtp_end", "XPLOGENT_RTP_END", 10100),
                   register=bool(s.get("register", True)), expires=int(s.get("expires") or 300),
                   allow_from=list(allow), max_calls=int(s.get("max_calls") or 4),
                   strict_source=bool(s.get("strict_source", False)))


# ── calls ────────────────────────────────────────────────────────────────────
@dataclass
class SipCall:
    call_id: str
    local_tag: str
    outbound: bool
    local_uri: str                      # our From/To value without tag
    remote_uri: str
    remote_tag: str = ""
    remote_target: str = ""             # the far end's Contact URI
    route_set: list[str] = field(default_factory=list)
    cseq: int = 1
    invite: SipMessage | None = None    # inbound: the INVITE we answered
    invite_branch: str = ""             # outbound: for CANCEL
    rtp: RtpStream | None = None
    session: Any = None
    brain: Any = None
    row_id: int = 0
    number: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    state: str = "early"                # early | confirmed | ended
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    bye_sent: bool = False
    remote_ended: bool = False
    supervisor: asyncio.Task | None = None


SessionFactory = Callable[..., Any]


class SipTrunk(asyncio.DatagramProtocol):
    """One SIP account: registration, calls and their media."""

    def __init__(self, settings: SipSettings, *, service: Any = None,
                 session_kw: dict[str, Any] | None = None, cfg: Any = None) -> None:
        self.s = settings
        self.service = service
        self.session_kw = session_kw or {}      # tests inject fake STT/TTS/LLM here
        self.cfg = cfg
        self.transport: asyncio.DatagramTransport | None = None
        self.server_addr: tuple[str, int] | None = None
        self.local_ip = ""
        self.ports = PortPool(settings.rtp_start, settings.rtp_end)
        self.calls: dict[str, SipCall] = {}
        self._tx: dict[tuple[str, str], dict[str, Any]] = {}
        self._srv_tx: dict[tuple[str, int, str], tuple[bytes, Any, float]] = {}
        self._acks: dict[str, tuple[bytes, Any]] = {}          # call-id → ACK for 2xx retransmits
        self._reg_call_id = f"{secrets.token_hex(8)}@xplogent"
        self._reg_cseq = 1
        self._reg_tag = new_tag()
        self._tasks: set[asyncio.Task] = set()
        self.registered = False
        self.reg_error = ""
        self.reg_ok_at = 0.0
        self.reg_expires = 0
        self.last_error = ""
        self.started = False
        self._reg_changed = asyncio.Event()
        self.trusted: set[str] = set()

    # -- lifecycle ---------------------------------------------------------------------
    @property
    def domain(self) -> str:
        return self.s.domain or self.s.server

    @property
    def contact_ip(self) -> str:
        return self.s.public_ip or self.local_ip

    async def start(self) -> None:
        if not (self.s.server and self.s.username):
            raise ValueError("SIP server and username are required")
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(self.s.server, self.s.port, family=socket.AF_INET,
                                       type=socket.SOCK_DGRAM)
        self.server_addr = (infos[0][4][0], self.s.port)
        self.local_ip = _route_ip(self.server_addr[0])
        await asyncio.to_thread(self._resolve_trusted)
        await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", self.s.local_port))
        self.started = True
        if self.s.register:
            self._spawn(self._register_loop())
        self._spawn(self._keepalive_loop())
        _log.info("SIP trunk on udp/%s → %s:%s as %s", self.s.local_port, self.s.server, self.s.port,
                  self.s.username)

    async def stop(self) -> None:
        for call in list(self.calls.values()):
            with contextlib.suppress(Exception):
                await self.hangup(call)
        if self.registered:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._register(expires=0), timeout=3)
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.transport is not None:
            self.transport.close()
        self.started = False

    def status(self) -> dict[str, Any]:
        return {"running": self.started, "registered": self.registered, "error": self.reg_error,
                "registered_at": self.reg_ok_at, "expires": self.reg_expires,
                "server": f"{self.s.server}:{self.s.port}", "username": self.s.username,
                "local_port": self.s.local_port, "contact_ip": self.contact_ip,
                "rtp": f"{self.s.rtp_start}-{self.s.rtp_end}", "calls": len(self.calls),
                "last_error": self.last_error}

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- transport ---------------------------------------------------------------------
    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not data.strip():
            return                                          # CRLF keep-alive
        try:
            msg = parse_message(data)
        except Exception:  # noqa: BLE001 - junk on the port
            return
        if msg.is_request:
            self._spawn(self._on_request(msg, addr))
        else:
            self._on_response(msg)

    def _send(self, msg: SipMessage | bytes, addr) -> None:
        if self.transport is not None:
            self.transport.sendto(msg if isinstance(msg, bytes) else msg.encode(), addr)

    # -- building requests ---------------------------------------------------------------
    def _via(self, branch: str) -> str:
        return f"SIP/2.0/UDP {self.contact_ip}:{self.s.local_port};branch={branch};rport"

    def _contact(self) -> str:
        return f"<sip:{self.s.username}@{self.contact_ip}:{self.s.local_port}>"

    def _request(self, method: str, uri: str, *, from_: str, to: str, call_id: str, cseq: int,
                 branch: str | None = None, routes: list[str] | None = None,
                 body: bytes = b"", ctype: str = "") -> SipMessage:
        msg = SipMessage(method=method, uri=uri)
        msg.headers = [("Via", self._via(branch or new_branch())), ("Max-Forwards", "70"),
                       ("From", from_), ("To", to), ("Call-ID", call_id), ("CSeq", f"{cseq} {method}")]
        for r in routes or []:
            msg.headers.append(("Route", r))
        if method not in ("ACK", "CANCEL", "BYE"):
            msg.headers.append(("Contact", self._contact()))
            msg.headers.append(("Allow", ALLOW))
        msg.headers.append(("User-Agent", USER_AGENT))
        if body:
            msg.headers.append(("Content-Type", ctype or "application/sdp"))
            msg.body = body
        return msg

    def _response(self, req: SipMessage, status: int, reason: str, *, to_tag: str = "",
                  body: bytes = b"", extra: list[tuple[str, str]] | None = None,
                  addr: Any = None) -> SipMessage:
        resp = SipMessage(status=status, reason=reason)
        vias = req.get_all("Via")
        if vias and addr is not None and re.search(r";\s*rport(?=;|$)", vias[0]):
            vias[0] = re.sub(r";\s*rport(?=;|$)", f";rport={addr[1]};received={addr[0]}", vias[0])
        resp.headers = [("Via", v) for v in vias]
        to = req.get("To")
        if to_tag and not header_param(to, "tag"):
            to = f"{to};tag={to_tag}"
        resp.headers += [("From", req.get("From")), ("To", to), ("Call-ID", req.call_id),
                         ("CSeq", req.get("CSeq"))]
        if status < 300 and req.method in ("INVITE", "OPTIONS"):
            resp.headers += [("Contact", self._contact()), ("Allow", ALLOW)]
        resp.headers += list(extra or [])
        resp.headers.append(("User-Agent", USER_AGENT))
        if body:
            resp.headers.append(("Content-Type", "application/sdp"))
            resp.body = body
        return resp

    def _reply(self, req: SipMessage, addr, status: int, reason: str, **kw) -> SipMessage:
        resp = self._response(req, status, reason, addr=addr, **kw)
        data = resp.encode()
        branch = header_param(req.get("Via"), "branch")
        self._srv_tx[(branch, req.cseq[0], req.method)] = (data, addr, time.monotonic())
        self._send(data, addr)
        return resp

    # -- client transactions ---------------------------------------------------------------
    async def _transact(self, msg: SipMessage, addr=None, *, timeout: float = 32.0,
                        on_provisional: Callable[[SipMessage], None] | None = None) -> SipMessage:
        """Send a request (retransmitting over UDP) and return its final response."""
        addr = addr or self.server_addr
        key = (header_param(msg.get("Via"), "branch"), msg.cseq[1])
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._tx[key] = {"future": fut, "provisional": on_provisional, "heard": False}
        data = msg.encode()
        is_invite = msg.method == "INVITE"
        deadline = time.monotonic() + timeout
        interval = T1
        try:
            self._send(data, addr)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no answer to {msg.method} from {addr[0]}:{addr[1]}")
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), timeout=min(interval, remaining))
                except TimeoutError:
                    pass
                tx = self._tx.get(key) or {}
                if not (is_invite and tx.get("heard")):
                    self._send(data, addr)       # retransmit until heard (INVITE) / answered
                interval = min(interval * 2, 4.0) if not is_invite else interval * 2
        finally:
            self._tx.pop(key, None)

    def _on_response(self, resp: SipMessage) -> None:
        key = (header_param(resp.get("Via"), "branch"), resp.cseq[1])
        tx = self._tx.get(key)
        if tx is None:
            # A retransmitted 2xx to an INVITE whose ACK got lost: ACK it again.
            if resp.cseq[1] == "INVITE" and 200 <= resp.status < 300 and resp.call_id in self._acks:
                data, addr = self._acks[resp.call_id]
                self._send(data, addr)
            return
        tx["heard"] = True
        if resp.status < 200:
            if tx["provisional"] is not None:
                with contextlib.suppress(Exception):
                    tx["provisional"](resp)
            return
        if not tx["future"].done():
            tx["future"].set_result(resp)

    async def _with_auth(self, build: Callable[[int, str | None], SipMessage], cseq: int, *,
                         timeout: float = 32.0, on_provisional=None,
                         on_error_ack: Callable[[SipMessage, SipMessage], None] | None = None
                         ) -> tuple[SipMessage, SipMessage, int]:
        """Send; answer one 401/407 challenge with digest credentials. → (request, response, cseq)."""
        req = build(cseq, None)
        resp = await self._transact(req, timeout=timeout, on_provisional=on_provisional)
        if resp.status in (401, 407):
            if on_error_ack:
                on_error_ack(req, resp)
            header = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
            challenge = parse_challenge(resp.get(header))
            auth = digest_authorization(req.method, req.uri, self.s.auth_username or self.s.username,
                                        self.s.password, challenge)
            cseq += 1
            req = build(cseq, auth if resp.status == 401 else None)
            if resp.status == 407:
                req.headers.insert(-1, ("Proxy-Authorization", auth))
            resp = await self._transact(req, timeout=timeout, on_provisional=on_provisional)
        return req, resp, cseq

    # -- registration ---------------------------------------------------------------------
    async def _register(self, expires: int) -> SipMessage:
        aor = f"<sip:{self.s.username}@{self.domain}>"

        def build(cseq: int, auth: str | None) -> SipMessage:
            msg = self._request("REGISTER", f"sip:{self.domain}", from_=f"{aor};tag={self._reg_tag}",
                                to=aor, call_id=self._reg_call_id, cseq=cseq)
            msg.set("Contact", f"{self._contact()};expires={expires}")
            msg.headers.insert(-1, ("Expires", str(expires)))
            if auth:
                msg.headers.insert(-1, ("Authorization", auth))
            return msg

        _req, resp, cseq = await self._with_auth(build, self._reg_cseq, timeout=16.0)
        self._reg_cseq = cseq + 1
        return resp

    async def _register_loop(self) -> None:
        failures = 0
        while True:
            try:
                resp = await self._register(self.s.expires)
            except Exception as exc:  # noqa: BLE001 - shown in the dashboard
                resp, err = None, f"{exc}"
            else:
                err = ""
            if resp is not None and 200 <= resp.status < 300:
                expires = self._granted_expires(resp)
                self.registered, self.reg_error = True, ""
                self.reg_ok_at, self.reg_expires = time.time(), expires
                failures = 0
                self._reg_changed.set()
                await asyncio.sleep(max(20.0, expires * 0.8))
                continue
            failures += 1
            if resp is not None:
                err = f"{resp.status} {resp.reason}"
                if resp.status in (401, 403, 407):
                    err += " (check the username/password)"
            self.registered, self.reg_error = False, err or "no answer"
            self._reg_changed.set()
            _log.warning("SIP registration failed: %s", self.reg_error)
            await asyncio.sleep(min(30.0 * failures, 300.0))

    def _granted_expires(self, resp: SipMessage) -> int:
        for contact in resp.get_all("Contact"):
            for one in contact.split(","):
                if f"@{self.contact_ip}:{self.s.local_port}" in one:
                    val = header_param(one, "expires")
                    if val.isdigit():
                        return int(val)
        val = resp.get("Expires").strip()
        return int(val) if val.isdigit() else self.s.expires

    async def wait_registered(self, timeout: float = 8.0) -> bool:
        """Wait for the first registration attempt to finish (Test button)."""
        self._reg_changed.clear()
        if self.registered:
            return True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._reg_changed.wait(), timeout)
        return self.registered

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(25)
            if self.server_addr:
                self._send(b"\r\n\r\n", self.server_addr)      # keeps the NAT mapping open
            now = time.monotonic()
            for key in [k for k, (_d, _a, t) in self._srv_tx.items() if now - t > 64]:
                self._srv_tx.pop(key, None)

    # -- inbound requests ------------------------------------------------------------------
    def _allowed(self, addr, req: SipMessage) -> bool:
        """Who may ring us: the trunk server (any of its addresses), ``allow_from``, or —
        unless ``strict_source`` — a call addressed to our own extension/username.

        The address check alone is not enough: in Docker a call can arrive from the
        bridge gateway, and carriers send calls from several proxy addresses.
        """
        if addr[0] in self.trusted:
            return True
        if self.s.strict_source:
            return False
        user = user_of(req.uri)
        return bool(user) and user in {self.s.username, self.s.auth_username}

    def _resolve_trusted(self) -> None:
        trusted = {self.server_addr[0]} if self.server_addr else set()
        for host in [self.s.server, *self.s.allow_from]:
            with contextlib.suppress(OSError):
                trusted.update(i[4][0] for i in socket.getaddrinfo(host, None, socket.AF_INET))
        self.trusted = trusted

    async def _on_request(self, req: SipMessage, addr) -> None:
        branch = header_param(req.get("Via"), "branch")
        cached = self._srv_tx.get((branch, req.cseq[0], req.method))
        if cached and req.method != "ACK":
            self._send(cached[0], cached[1])                 # retransmitted request
            return
        method = req.method
        call = self.calls.get(req.call_id)
        if method == "OPTIONS":
            self._reply(req, addr, 200, "OK")
        elif method == "INVITE":
            if call is not None:                              # re-INVITE (hold, codec refresh…)
                self._reply(req, addr, 200, "OK", to_tag=call.local_tag,
                            body=build_sdp(self.contact_ip, call.rtp.port if call.rtp else 0,
                                           (call.rtp.pt,) if call.rtp else (PCMU,)))
            else:
                await self._inbound_invite(req, addr)
        elif method == "ACK":
            if call is not None:
                call.acked.set()
        elif method == "BYE":
            self._reply(req, addr, 200, "OK")
            if call is not None:
                call.remote_ended = True
                await self._end(call)
        elif method == "CANCEL":
            self._reply(req, addr, 200, "OK")
            if call is not None and call.state == "early" and call.invite is not None:
                self._reply(call.invite, addr, 487, "Request Terminated", to_tag=call.local_tag)
                call.remote_ended = True
                await self._end(call)
        elif method == "NOTIFY":
            self._reply(req, addr, 200, "OK")
            if call is not None and req.get("Event").lower().startswith("refer"):
                self._transfer_progress(call, req.body.decode("utf-8", "replace"))
        elif method in ("INFO", "UPDATE", "PRACK", "MESSAGE"):
            self._reply(req, addr, 200, "OK")
        else:
            self._reply(req, addr, 405, "Method Not Allowed", extra=[("Allow", ALLOW)])

    async def _inbound_invite(self, req: SipMessage, addr) -> None:
        if not self._allowed(addr, req):
            _log.warning("SIP INVITE from %s to %s refused (not the trunk server; add it under "
                         "'Also accept calls from' if it is yours)", addr[0], user_of(req.uri) or "?")
            self._reply(req, addr, 403, "Forbidden")
            return
        if self.service is not None and not self._agent_enabled():
            self._reply(req, addr, 480, "Temporarily Unavailable")
            return
        if len(self.calls) >= self.s.max_calls:
            self._reply(req, addr, 486, "Busy Here")
            return
        self._reply(req, addr, 100, "Trying")
        sdp = parse_sdp(req.body)
        codec = choose_codec(sdp)
        if codec is None or not sdp.get("port"):
            self._reply(req, addr, 488, "Not Acceptable Here")
            return
        call = SipCall(call_id=req.call_id, local_tag=new_tag(), outbound=False,
                       local_uri=req.get("To"), remote_uri=req.get("From"),
                       remote_tag=header_param(req.get("From"), "tag"),
                       remote_target=uri_of(req.get("Contact") or req.get("From")),
                       route_set=list(req.get_all("Record-Route")), invite=req,
                       number=user_of(req.get("From")))
        try:
            call.rtp = await self._open_rtp(codec, sdp)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"RTP: {exc}"
            self._reply(req, addr, 503, "Service Unavailable")
            return
        self.calls[call.call_id] = call
        self._reply(req, addr, 180, "Ringing", to_tag=call.local_tag)
        ok = self._reply(req, addr, 200, "OK", to_tag=call.local_tag,
                         body=build_sdp(self.contact_ip, call.rtp.port, (codec,)))
        call.state = "confirmed"
        info = {"direction": "inbound", "from": call.number, "to": user_of(req.get("To")),
                "call_control_id": call.call_id, "transport": "sip"}
        # Retransmit the 200 OK until the ACK arrives (UDP may drop it).
        data, wait = ok.encode(), T1
        deadline = time.monotonic() + 32
        while not call.acked.is_set() and time.monotonic() < deadline and call.state != "ended":
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(call.acked.wait(), timeout=wait)
            if not call.acked.is_set():
                self._send(data, addr)
                wait = min(wait * 2, 4.0)
        if not call.acked.is_set():
            _log.warning("no ACK for call %s: hanging up", call.call_id)
            await self.hangup(call)
            return
        await self._start_session(call, info)

    def _agent_enabled(self) -> bool:
        from xplogent.core.config import load_config

        return bool((load_config().voip or {}).get("enabled"))

    async def _open_rtp(self, codec: int, sdp: dict[str, Any]) -> RtpStream:
        rtp = RtpStream(codec)
        last: Exception | None = None
        for _ in range(8):
            port = self.ports.take()
            try:
                await rtp.open(port)
                break
            except OSError as exc:       # in use by something else: try the next one
                last = exc
                self.ports.give(port)
        else:
            raise RuntimeError(f"no RTP port available ({last})")
        if sdp.get("ip") and sdp.get("port"):
            rtp.remote = (sdp["ip"], int(sdp["port"]))
        return rtp

    # -- outbound calls ------------------------------------------------------------------------
    async def dial(self, number: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call ``number`` through the trunk. Returns once it rings (or fails early)."""
        if len(self.calls) >= self.s.max_calls:
            return {"ok": False, "error": "too many calls on the SIP trunk right now"}
        target = re.sub(r"[^\d+*#]", "", number)
        caller = self.s.caller_id or self.s.username
        call = SipCall(call_id=f"{secrets.token_hex(10)}@xplogent", local_tag=new_tag(), outbound=True,
                       local_uri=f'"{caller}" <sip:{self.s.username}@{self.domain}>',
                       remote_uri=f"<sip:{target}@{self.domain}>", number=target, meta=dict(meta or {}))
        try:
            call.rtp = await self._open_rtp(PCMU, {})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"RTP: {exc}"}
        self.calls[call.call_id] = call
        early: asyncio.Future = asyncio.get_running_loop().create_future()
        self._spawn(self._outbound(call, early))
        try:
            return await asyncio.wait_for(asyncio.shield(early), timeout=6.0)
        except TimeoutError:
            return {"ok": True, "via": "sip", "call": {"sip_call_id": call.call_id}, "state": "calling"}

    async def _outbound(self, call: SipCall, early: asyncio.Future) -> None:
        uri = f"sip:{call.number}@{self.domain}"
        sdp = build_sdp(self.contact_ip, call.rtp.port, (PCMU, PCMA))

        def settle(result: dict[str, Any]) -> None:
            if not early.done():
                early.set_result(result)

        def build(cseq: int, auth: str | None) -> SipMessage:
            call.invite_branch = new_branch()
            msg = self._request("INVITE", uri, from_=f"{call.local_uri};tag={call.local_tag}",
                                to=call.remote_uri, call_id=call.call_id, cseq=cseq,
                                branch=call.invite_branch, body=sdp)
            if self.s.caller_id and self.s.caller_id != self.s.username:
                msg.headers.insert(-2, ("P-Asserted-Identity", f"<sip:{self.s.caller_id}@{self.domain}>"))
            if auth:
                msg.headers.insert(-2, ("Authorization", auth))
            call.invite = msg
            return msg

        def provisional(resp: SipMessage) -> None:
            if resp.status in (180, 183):
                call.remote_tag = header_param(resp.get("To"), "tag") or call.remote_tag
                settle({"ok": True, "via": "sip", "call": {"sip_call_id": call.call_id}, "state": "ringing"})

        def ack_error(req: SipMessage, resp: SipMessage) -> None:
            ack = SipMessage(method="ACK", uri=req.uri)
            ack.headers = [("Via", req.get("Via")), ("Max-Forwards", "70"), ("From", req.get("From")),
                           ("To", resp.get("To")), ("Call-ID", req.call_id),
                           ("CSeq", f"{req.cseq[0]} ACK"), ("User-Agent", USER_AGENT)]
            self._send(ack, self.server_addr)

        timer = self._spawn(self._ring_timeout(call))
        try:
            req, resp, call.cseq = await self._with_auth(build, 1, timeout=self.s.ring_timeout + 10,
                                                          on_provisional=provisional,
                                                          on_error_ack=ack_error)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"call to {call.number}: {exc}"
            settle({"ok": False, "error": self.last_error})
            await self._end(call)
            return
        finally:
            timer.cancel()
        if not 200 <= resp.status < 300:
            ack_error(req, resp)
            self.last_error = f"call to {call.number}: {resp.status} {resp.reason}"
            settle({"ok": False, "error": self.last_error})
            call.remote_ended = True
            await self._end(call)
            return
        call.remote_tag = header_param(resp.get("To"), "tag")
        call.remote_uri = resp.get("To")
        call.remote_target = uri_of(resp.get("Contact")) or uri
        call.route_set = list(reversed(resp.get_all("Record-Route")))
        call.state = "confirmed"
        ack = self._request("ACK", call.remote_target, from_=f"{call.local_uri};tag={call.local_tag}",
                            to=call.remote_uri, call_id=call.call_id, cseq=call.cseq,
                            routes=call.route_set)
        self._acks[call.call_id] = (ack.encode(), self.server_addr)
        self._send(ack, self.server_addr)
        answer = parse_sdp(resp.body)
        codec = choose_codec(answer)
        if codec is None:
            settle({"ok": False, "error": "the far end chose a codec we don't speak"})
            await self.hangup(call)
            return
        call.rtp.pt = codec
        if answer.get("ip") and answer.get("port"):
            call.rtp.remote = (answer["ip"], int(answer["port"]))
        settle({"ok": True, "via": "sip", "call": {"sip_call_id": call.call_id}, "state": "answered"})
        info = {"direction": "outbound", "from": self.s.caller_id or self.s.username, "to": call.number,
                "call_control_id": call.call_id, "transport": "sip", **call.meta}
        await self._start_session(call, info)

    async def _ring_timeout(self, call: SipCall) -> None:
        await asyncio.sleep(self.s.ring_timeout)
        if call.state == "early" and call.invite is not None:
            await self._cancel(call)

    async def _cancel(self, call: SipCall) -> None:
        inv = call.invite
        cancel = SipMessage(method="CANCEL", uri=inv.uri)
        cancel.headers = [("Via", inv.get("Via")), ("Max-Forwards", "70"), ("From", inv.get("From")),
                          ("To", inv.get("To")), ("Call-ID", inv.call_id), ("CSeq", f"{inv.cseq[0]} CANCEL"),
                          ("User-Agent", USER_AGENT)]
        with contextlib.suppress(Exception):
            await self._transact(cancel, timeout=8)

    # -- in-dialog requests --------------------------------------------------------------------
    def _in_dialog(self, call: SipCall, method: str, extra: list[tuple[str, str]] | None = None) -> SipMessage:
        call.cseq += 1
        local = f"{call.local_uri};tag={call.local_tag}" if not header_param(call.local_uri, "tag") \
            else call.local_uri
        remote = call.remote_uri
        if call.remote_tag and not header_param(remote, "tag"):
            remote = f"{remote};tag={call.remote_tag}"
        msg = self._request(method, call.remote_target or uri_of(remote), from_=local, to=remote,
                            call_id=call.call_id, cseq=call.cseq, routes=call.route_set)
        for k, v in extra or []:
            msg.headers.insert(-1, (k, v))
        return msg

    async def hangup(self, call: SipCall) -> None:
        if call.state == "ended" or call.bye_sent:
            return
        if call.state == "early" and call.outbound and call.invite is not None:
            await self._cancel(call)
        elif not call.remote_ended:
            call.bye_sent = True
            with contextlib.suppress(Exception):
                await self._transact(self._in_dialog(call, "BYE"), timeout=8)
        await self._end(call)

    async def refer(self, call: SipCall, number: str) -> None:
        """Ask the PBX/carrier to connect the caller to ``number`` (blind transfer)."""
        target = number if number.lower().startswith("sip:") else \
            f"sip:{re.sub(r'[^0-9+*#]', '', number)}@{self.domain}"
        resp = await self._transact(self._in_dialog(call, "REFER", [
            ("Refer-To", f"<{target}>"), ("Referred-By", f"<sip:{self.s.username}@{self.domain}>")]),
            timeout=8)
        if resp.status not in (200, 202):
            raise RuntimeError(f"transfer refused: {resp.status} {resp.reason}")
        # The far end normally hangs up on us once the transfer connects.
        self._spawn(self._bye_later(call, 30))

    def _transfer_progress(self, call: SipCall, sipfrag: str) -> None:
        """NOTIFY after our REFER: ``SIP/2.0 200 OK`` = connected, 4xx-6xx = failed."""
        m = re.search(r"SIP/2\.0\s+(\d{3})\s*([^\r\n]*)", sipfrag)
        if not m or int(m.group(1)) < 200:
            return
        code = int(m.group(1))
        if code >= 300:
            self.last_error = f"transfer to the PBX reported {code} {m.group(2).strip()}"
            _log.warning("SIP %s", self.last_error)
        self._spawn(self.hangup(call))          # done either way: leave the call

    async def _bye_later(self, call: SipCall, delay: float) -> None:
        await asyncio.sleep(delay)
        if call.state != "ended":
            await self.hangup(call)

    # -- session wiring -------------------------------------------------------------------------
    async def _start_session(self, call: SipCall, info: dict[str, Any]) -> None:
        from xplogent.core.config import load_config
        from xplogent.memory.store import Store
        from xplogent.voip.control import LiveCall
        from xplogent.voip.service import build_session, finalize_call

        cfg = self.cfg or load_config()
        v = cfg.voip or {}
        store = Store(cfg.db_path)

        async def hangup() -> None:
            # Don't wait on the BYE here: stopping the session would cancel it mid-way.
            self._spawn(self.hangup(call))

        async def ai_transfer(reason: str) -> None:
            number = str(v.get("transfer_number", ""))
            try:
                if not number:
                    raise RuntimeError("no hand-off number")
                await self.refer(call, number)
            except Exception:  # noqa: BLE001
                _log.exception("SIP transfer failed; hanging up")
                await self.hangup(call)

        try:
            session, row_id, brain = build_session(
                cfg, store, info, send_audio=call.rtp.send_audio, clear_audio=call.rtp.clear,
                hangup=hangup, transfer=ai_transfer, **self.session_kw)
        except Exception as exc:  # noqa: BLE001
            _log.exception("could not start the call session")
            self.last_error = f"session: {exc}"
            store.close()
            await self.hangup(call)
            return
        call.session, call.brain, call.row_id = session, brain, row_id
        call.rtp.on_audio = session.on_audio
        call.rtp.start()
        if self.service is not None:
            self.service.register(LiveCall(
                call_id=row_id, session=session, brain=brain, number=call.number,
                direction="outbound" if call.outbound else "inbound", transport="sip",
                test=bool(info.get("test")), contact_name=str((brain.contact or {}).get("name") or ""),
                transfer_to=lambda number: self.refer(call, number)))
        await session.start()

        async def supervise() -> None:
            try:
                await session.finished.wait()
            finally:
                if self.service is not None:
                    self.service.unregister(row_id)
                await session.stop()
                if not brain.state.transfer:        # after a transfer the far end hangs up
                    if not call.remote_ended and not call.bye_sent:
                        await self.hangup(call)
                    await self._end(call)
                result = finalize_call(cfg, store, row_id, session, brain, call.number)
                _log.info("SIP call %s finished: %s", row_id, result)
                closers = [session.stt.aclose, session.tts.aclose, brain.provider.aclose]
                if hasattr(brain.retrieve, "aclose"):
                    closers.append(brain.retrieve.aclose)
                for closer in closers:
                    with contextlib.suppress(Exception):
                        await closer()
                store.close()

        call.supervisor = self._spawn(supervise())

    async def _end(self, call: SipCall) -> None:
        if call.state == "ended":
            return
        call.state = "ended"
        self.calls.pop(call.call_id, None)
        self._acks.pop(call.call_id, None)
        if call.session is not None:
            call.session.finished.set()
        if call.rtp is not None:
            await call.rtp.close()
            self.ports.give(call.rtp.port)


def _route_ip(dest: str) -> str:
    """Our address on the route to ``dest`` (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((dest, 9))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# ── the running trunk (one per server) ───────────────────────────────────────
_TRUNK: SipTrunk | None = None
_LOCK: asyncio.Lock | None = None


def current_trunk() -> SipTrunk | None:
    return _TRUNK


def wants_sip(v: dict[str, Any]) -> bool:
    return str(v.get("telephony") or "telnyx") in ("sip", "both") and bool((v.get("sip") or {}).get("server"))


async def apply_settings(cfg: Any = None) -> SipTrunk | None:
    """(Re)start or stop the trunk to match the configuration."""
    global _TRUNK, _LOCK
    from xplogent.core.config import load_config
    from xplogent.voip.service import get_service

    _LOCK = _LOCK or asyncio.Lock()
    async with _LOCK:
        cfg = cfg or load_config()
        v = cfg.voip or {}
        service = get_service()
        if _TRUNK is not None:
            await _TRUNK.stop()
            _TRUNK = None
            service.sip_dialer = None
        if not wants_sip(v):
            return None
        trunk = SipTrunk(SipSettings.from_config(v), service=service)
        try:
            await trunk.start()
        except Exception as exc:  # noqa: BLE001 - reported in the status
            trunk.reg_error = f"could not start: {exc}"
            _log.warning("SIP trunk not started: %s", exc)
            _TRUNK = trunk
            return trunk
        _TRUNK = trunk
        service.sip_dialer = trunk.dial
        return trunk


async def shutdown() -> None:
    global _TRUNK
    if _TRUNK is not None:
        await _TRUNK.stop()
        _TRUNK = None
