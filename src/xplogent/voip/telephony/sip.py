"""Built-in SIP client: register with a PBX or carrier and handle calls over RTP.

A small SIP user agent (RFC 3261 subset) per SIP account, so the phone agent can work
with FreePBX/Asterisk, Telnyx SIP connections and other providers without the
Telnyx Voice API or a public HTTPS address. Two accounts ("lines") can run at once:
the **trunk** (``voip.sip`` — e.g. a Telnyx SIP connection for external calls) and
the **extension** (``voip.sip_extension`` — a FreePBX extension for internal calls
and queue overflow), each with its own registration, ports and call set:

* **REGISTER** with digest authentication (401/407), refreshed before it expires;
* **inbound calls**: answers an INVITE (100 Trying, 180 Ringing, 200 OK with an
  SDP answer), then ACK/BYE/CANCEL; OPTIONS keep-alives are answered;
* **outbound calls**: INVITE (with digest auth), ringing timeout → CANCEL;
* **transfer** with REFER (the PBX/carrier connects the caller to the number);
* **audio**: RTP over UDP, G.711 μ-law (PCMU) or A-law (PCMA), 20 ms frames,
  symmetric RTP (we reply to where the far end actually sends from, which helps
  behind NAT). RFC 4733 DTMF telephone-events are decoded and can be sent;
  comfort-noise packets are ignored.

Each call is wired to the same :class:`~xplogent.voip.engine.CallSession` the
Telnyx path uses (8 kHz μ-law in and out), and registered for live control.
Calls are tagged with the line they came in on (``trunk`` | ``extension``).

Limits: UDP only (no TCP/TLS/SRTP), G.711 only, two SIP accounts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from xplogent.core.logging import get_logger
from xplogent.voip.telephony.rtp import (  # noqa: F401 - re-exported
    FRAME,
    SILENCE,
    PortPool,
    RtpStream,
)
from xplogent.voip.telephony.sip_proto import (  # noqa: F401 - re-exported
    ALLOW,
    CODEC_NAMES,
    DTMF_PT,
    PCMA,
    PCMU,
    T1,
    USER_AGENT,
    SipMessage,
    build_sdp,
    call_identity,
    choose_codec,
    digest_authorization,
    header_param,
    host_port,
    new_branch,
    new_tag,
    parse_challenge,
    parse_message,
    parse_sdp,
    record_routes,
    split_header_values,
    uri_of,
    user_of,
)

_log = get_logger("voip.sip")


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
    enabled: bool = False                # only the extension line uses this: off = fully inert
    queue_numbers: list[str] = field(default_factory=list)   # PBX queues that overflow to this line
    # Extensions still answered while the phone agent is switched off (marked test calls),
    # so it can be tried from a desk phone before it goes live.
    test_callers: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, v: dict[str, Any], block: str = "sip", *,
                    password_env: str = "SIP_PASSWORD",
                    local_port_default: int = 5060, rtp_lo: int = 10000, rtp_hi: int = 10100,
                    port_envs: tuple[str, str, str] = ("XPLOGENT_SIP_PORT", "XPLOGENT_RTP_START",
                                                       "XPLOGENT_RTP_END")) -> SipSettings:
        """Parse one SIP account block (``voip.sip`` for the trunk,
        ``voip.sip_extension`` for the extension line). The password never lives
        in the config file: it is the named secret, loaded into the environment
        from the encrypted secrets store."""
        s = dict(v.get(block) or {})

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
        queues = s.get("queue_numbers") or []
        if isinstance(queues, str):
            queues = [q.strip() for q in queues.split(",") if q.strip()]
        testers = s.get("test_callers") or []
        if isinstance(testers, str):
            testers = [t.strip() for t in testers.split(",") if t.strip()]
        return cls(server=str(s.get("server") or "").strip(), username=str(s.get("username") or "").strip(),
                   password=os.environ.get(password_env, ""), port=int(s.get("port") or 5060),
                   auth_username=str(s.get("auth_username") or "").strip(),
                   domain=str(s.get("domain") or "").strip(), caller_id=str(s.get("caller_id") or "").strip(),
                   public_ip=str(s.get("public_ip") or os.environ.get("XPLOGENT_HOST_IP") or "").strip(),
                   local_port=num("local_port", port_envs[0], local_port_default),
                   rtp_start=num("rtp_start", port_envs[1], rtp_lo),
                   rtp_end=num("rtp_end", port_envs[2], rtp_hi),
                   register=bool(s.get("register", True)), expires=int(s.get("expires") or 300),
                   allow_from=list(allow), max_calls=int(s.get("max_calls") or 4),
                   strict_source=bool(s.get("strict_source", False)),
                   ring_timeout=float(s.get("ring_timeout") or 45),
                   enabled=bool(s.get("enabled", False)),
                   queue_numbers=[str(q) for q in queues], test_callers=[str(t) for t in testers])

    @classmethod
    def extension_from_config(cls, v: dict[str, Any]) -> SipSettings:
        """The second line (``voip.sip_extension``): a PBX extension login whose
        default ports sit clear of the trunk's (5062, RTP 10200-10300) and whose
        password is the separate SIP_EXTENSION_PASSWORD secret."""
        return cls.from_config(v, "sip_extension", password_env="SIP_EXTENSION_PASSWORD",
                               local_port_default=5062, rtp_lo=10200, rtp_hi=10300,
                               port_envs=("XPLOGENT_SIP_EXT_PORT", "XPLOGENT_RTP_EXT_START",
                                         "XPLOGENT_RTP_EXT_END"))


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
                 session_kw: dict[str, Any] | None = None, cfg: Any = None,
                 line: str = "trunk") -> None:
        self.s = settings
        self.line = line                      # "trunk" | "extension": tags every call
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
        self.busy_at: list[float] = []      # when callers got "busy" because the line was full
        self.traces: dict[str, Any] = {}     # SIP Call-ID -> debug.SipTrace (calls only)
        self.started = False
        self._reg_changed = asyncio.Event()
        self.trusted: set[str] = set()
        self.ack_timeout = 64 * T1          # RFC 3261 §13.3.1.4: then confirm and BYE

    # -- lifecycle ---------------------------------------------------------------------
    @property
    def via_name(self) -> str:
        """How dialling through this line is named in results: the trunk keeps its
        historical ``sip`` name; the extension line is ``extension``."""
        return "sip" if self.line == "trunk" else self.line

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
        _log.info("SIP %s line on udp/%s → %s:%s as %s", self.line, self.s.local_port,
                  self.s.server, self.s.port, self.s.username)

    async def stop(self) -> None:
        # Hang up every call at once and unregister, but never block shutdown for long
        # on a server that stopped answering.
        hangups = [asyncio.create_task(self.hangup(c)) for c in list(self.calls.values())]
        if hangups:
            await asyncio.wait(hangups, timeout=3)
            for t in hangups:
                t.cancel()
            for c in list(self.calls.values()):
                await self._end(c)
        if self.registered:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._register(expires=0), timeout=2)
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.transport is not None:
            self.transport.close()
        self.started = False

    def status(self) -> dict[str, Any]:
        return {"line": self.line,
                "running": self.started, "registered": self.registered, "error": self.reg_error,
                "registered_at": self.reg_ok_at, "expires": self.reg_expires,
                "server": f"{self.s.server}:{self.s.port}", "username": self.s.username,
                "local_port": self.s.local_port, "contact_ip": self.contact_ip,
                "rtp": f"{self.s.rtp_start}-{self.s.rtp_end}", "calls": len(self.calls),
                "max_calls": self.s.max_calls, **self.busy_summary(),
                "last_error": self.last_error}

    def busy_summary(self) -> dict[str, Any]:
        """Callers turned away because the line was full, over the last 24 hours."""
        recent = [t for t in self.busy_at if t > time.time() - 86400]
        return {"busy_24h": len(recent), "last_busy_at": recent[-1] if recent else None}

    def clear_busy(self) -> None:
        self.busy_at = []

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
        self._trace("in", msg, addr)
        if msg.is_request:
            self._spawn(self._on_request(msg, addr))
        else:
            self._on_response(msg)

    def _send(self, msg: SipMessage | bytes, addr) -> None:
        if self.transport is not None:
            self.transport.sendto(msg if isinstance(msg, bytes) else msg.encode(), addr)
        if isinstance(msg, bytes):
            if not msg.strip():
                return                                       # CRLF keep-alive
            try:
                msg = parse_message(msg)
            except Exception:  # noqa: BLE001
                return
        self._trace("out", msg, addr)

    async def _crm_lookup(self, ident: dict[str, Any]) -> dict[str, Any] | None:
        """Pre-call CRM lookup for an inbound caller (None when the CRM isn't set up)."""
        if self.service is None:
            return None
        from xplogent.core.config import load_config
        from xplogent.voip import crm

        cfg = self.cfg or load_config()
        if not crm.lookup_enabled(cfg) or not ident.get("caller"):
            return None
        return await crm.lookup(cfg, ident["caller"], direction="inbound", line=self.line,
                                original_called_number=ident.get("original_called_number", ""))

    def _trace(self, direction: str, msg: SipMessage, addr) -> None:
        """Keep the SIP ladder of calls (not REGISTER/OPTIONS) for the debug view."""
        from xplogent.voip.debug import SipTrace

        cid = msg.call_id
        trace = self.traces.get(cid)
        if trace is None:
            if msg.cseq[1] != "INVITE" or not cid:
                return
            while len(self.traces) >= 50:                    # refused/abandoned calls: oldest go
                self.traces.pop(next(iter(self.traces)))
            trace = self.traces[cid] = SipTrace()
        trace.add(direction, msg, addr)

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
                                           (call.rtp.pt,) if call.rtp else (PCMU,),
                                           dtmf_pt=call.rtp.dtmf_pt if call.rtp else None))
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
        # Identity headers (PAI, Diversion, queue hints) count only when the PBX sent them.
        ident = call_identity(req, trusted=addr[0] in self.trusted, queue_numbers=self.s.queue_numbers)
        test_only = False
        if self.service is not None and not self._agent_enabled():
            if not (self.s.test_callers and str(ident["caller"]).lstrip("+") in
                    {t.lstrip("+") for t in self.s.test_callers}):
                _log.info("SIP %s: call from %s refused (480): the phone agent is switched off", self.line,
                          ident["caller"] or "?")
                self._reply(req, addr, 480, "Temporarily Unavailable")
                return
            test_only = True        # agent off, but one of the test extensions: answer as a test
        if len(self.calls) >= self.s.max_calls:
            self.busy_at = [t for t in self.busy_at if t > time.time() - 86400][-999:] + [time.time()]
            _log.warning("SIP %s line full (%s calls): caller %s got busy", self.line,
                         self.s.max_calls, user_of(req.get("From")) or "?")
            self._reply(req, addr, 486, "Busy Here")
            return
        self._reply(req, addr, 100, "Trying")
        try:
            sdp = parse_sdp(req.body)
        except Exception:  # noqa: BLE001 - malformed offer: answer, don't go silent
            self._reply(req, addr, 400, "Bad Request")
            return
        codec = choose_codec(sdp)
        if codec is None or not sdp.get("port"):
            self._reply(req, addr, 488, "Not Acceptable Here")
            return
        call = SipCall(call_id=req.call_id, local_tag=new_tag(), outbound=False,
                       local_uri=req.get("To"), remote_uri=req.get("From"),
                       remote_tag=header_param(req.get("From"), "tag"),
                       remote_target=uri_of(req.get("Contact") or req.get("From")),
                       route_set=record_routes(req), invite=req,
                       number=ident["caller"])
        try:
            call.rtp = await self._open_rtp(codec, sdp)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"RTP: {exc}"
            self._reply(req, addr, 503, "Service Unavailable")
            return
        self.calls[call.call_id] = call
        self._reply(req, addr, 180, "Ringing", to_tag=call.local_tag)
        crm_lookup = await self._crm_lookup(ident)            # while it rings; 1.5 s at most
        if call.state == "ended":                              # the caller gave up (CANCEL)
            return
        ok = self._reply(req, addr, 200, "OK", to_tag=call.local_tag,
                         body=build_sdp(self.contact_ip, call.rtp.port, (codec,), dtmf_pt=call.rtp.dtmf_pt))
        call.state = "confirmed"
        info = {"direction": "inbound", "from": call.number, "to": user_of(req.get("To")),
                "call_control_id": call.call_id, "transport": "sip", "line": self.line,
                "original_called_number": ident["original_called_number"],
                "diversion_reason": ident["diversion_reason"], "via_queue": ident["via_queue"],
                "queue_wait_s": ident["queue_wait_s"]}
        if crm_lookup is not None:
            info["crm_lookup"] = crm_lookup
        if test_only:
            info["test"] = True
        # Retransmit the 200 OK until the ACK arrives (UDP may drop it).
        data, wait = ok.encode(), T1
        deadline = time.monotonic() + self.ack_timeout
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
        rtp.dtmf_pt = sdp.get("dtmf_pt")          # keypad events only if the offer has them
        return rtp

    # -- outbound calls ------------------------------------------------------------------------
    async def dial(self, number: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call ``number`` through the trunk. Returns once it rings (or fails early)."""
        if len(self.calls) >= self.s.max_calls:
            return {"ok": False, "error": f"too many calls on the SIP {self.line} line right now"}
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
            return {"ok": True, "via": self.via_name, "call": {"sip_call_id": call.call_id},
                    "state": "calling"}

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
                settle({"ok": True, "via": self.via_name, "call": {"sip_call_id": call.call_id},
                        "state": "ringing"})

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
        call.route_set = list(reversed(record_routes(resp)))
        call.state = "confirmed"
        ack = self._request("ACK", call.remote_target, from_=f"{call.local_uri};tag={call.local_tag}",
                            to=call.remote_uri, call_id=call.call_id, cseq=call.cseq,
                            routes=call.route_set)
        self._acks[call.call_id] = (ack.encode(), self.server_addr)
        self._send(ack, self.server_addr)
        try:
            answer = parse_sdp(resp.body)
        except ValueError:
            answer = {}
        codec = choose_codec(answer)
        if codec is None:
            settle({"ok": False, "error": "the far end chose a codec we don't speak"})
            await self.hangup(call)
            return
        call.rtp.pt = codec
        call.rtp.dtmf_pt = answer.get("dtmf_pt")
        if answer.get("ip") and answer.get("port"):
            call.rtp.remote = (answer["ip"], int(answer["port"]))
        settle({"ok": True, "via": self.via_name, "call": {"sip_call_id": call.call_id},
                "state": "answered"})
        info = {"direction": "outbound", "from": self.s.caller_id or self.s.username, "to": call.number,
                "call_control_id": call.call_id, "transport": "sip", **call.meta, "line": self.line}
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
        # The result arrives as NOTIFY (see _transfer_progress); the session's transfer
        # watchdog resumes the agent if it never does.

    def _transfer_progress(self, call: SipCall, sipfrag: str) -> None:
        """NOTIFY after our REFER: 2xx = the caller is connected elsewhere (we leave the
        call); 4xx-6xx = it failed and the caller is still with us (the agent resumes)."""
        m = re.search(r"SIP/2\.0\s+(\d{3})\s*([^\r\n]*)", sipfrag)
        if not m or int(m.group(1)) < 200 or call.session is None:
            return
        code = int(m.group(1))
        if code < 300:
            call.session.transfer_done()        # supervise() then sends our BYE
            return
        self.last_error = f"transfer failed: {code} {m.group(2).strip()}"
        _log.warning("SIP %s; the agent resumes", self.last_error)
        self._spawn(call.session.transfer_failed(f"{code} {m.group(2).strip()}"))

    # -- session wiring -------------------------------------------------------------------------
    async def _start_session(self, call: SipCall, info: dict[str, Any]) -> None:
        from xplogent.core.config import load_config
        from xplogent.memory.store import Store
        from xplogent.voip.control import LiveCall, resolve_transfer_number
        from xplogent.voip.service import build_session, finalize_call

        cfg = self.cfg or load_config()
        v = cfg.voip or {}
        store = Store(cfg.db_path)

        async def hangup() -> None:
            # Don't wait on the BYE here: stopping the session would cancel it mid-way.
            self._spawn(self.hangup(call))

        async def transfer_to(number: str) -> None:
            target = resolve_transfer_number(v, number, transport="sip")
            if not target:
                raise ValueError("no valid transfer number or extension")
            await self.refer(call, target)

        async def ai_transfer(reason: str) -> None:     # failures: the session resumes
            await transfer_to("")

        async def send_dtmf(digits: str) -> None:
            call.rtp.queue_dtmf(digits)

        try:
            session, row_id, brain = build_session(
                cfg, store, info, send_audio=call.rtp.send_audio, clear_audio=call.rtp.clear,
                send_dtmf=send_dtmf if call.rtp.dtmf_pt is not None else None,
                hangup=hangup, transfer=ai_transfer, **self.session_kw)
        except Exception as exc:  # noqa: BLE001
            _log.exception("could not start the call session")
            self.last_error = f"session: {exc}"
            store.close()
            await self.hangup(call)
            return
        call.session, call.brain, call.row_id = session, brain, row_id
        trace = self.traces.get(call.call_id)
        session.debug_sources.append(lambda: {
            "line": self.line, "sip": trace.export() if trace is not None else {},
            "rtp": call.rtp.stats() if call.rtp is not None else {}})
        call.rtp.on_audio = session.on_audio
        call.rtp.on_dtmf = session.on_dtmf
        call.rtp.start()
        if self.service is not None:
            self.service.register(LiveCall(
                call_id=row_id, session=session, brain=brain, number=call.number,
                direction="outbound" if call.outbound else "inbound", transport="sip",
                test=bool(info.get("test")), contact_name=str((brain.contact or {}).get("name") or ""),
                transfer_to=transfer_to, ref=call.call_id, line=self.line,
                via_queue=bool(info.get("via_queue"))))
        await session.start()

        async def supervise() -> None:
            try:
                await session.finished.wait()
            finally:
                if self.service is not None:
                    self.service.unregister(row_id)
                await session.stop()
                # After a completed transfer too: the transferor leaves with a BYE.
                if not call.remote_ended and not call.bye_sent:
                    await self.hangup(call)
                await self._end(call)
                result = finalize_call(cfg, store, row_id, session, brain, call.number)
                _log.info("SIP call %s finished: %s", row_id, result)
                self.traces.pop(call.call_id, None)
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


# ── the running lines (trunk + extension, independently managed) ─────────────
_LINES: dict[str, SipTrunk | None] = {"trunk": None, "extension": None}
_LOCKS: dict[str, asyncio.Lock] = {}


def _line_lock(line: str) -> asyncio.Lock:
    return _LOCKS.setdefault(line, asyncio.Lock())


def current_trunk() -> SipTrunk | None:
    """The trunk line (``voip.sip``); existing callers keep this name."""
    return _LINES["trunk"]


def current_line(line: str = "trunk") -> SipTrunk | None:
    return _LINES.get(line)


def wants_sip(v: dict[str, Any]) -> bool:
    return str(v.get("telephony") or "telnyx") in ("sip", "both") and bool((v.get("sip") or {}).get("server"))


def wants_extension(v: dict[str, Any]) -> bool:
    """The extension line runs only when it is switched on and filled in; it is
    independent of the ``telephony`` mode that governs the trunk."""
    s = SipSettings.extension_from_config(v)
    return bool(s.enabled and s.server and s.username)


async def apply_settings(cfg: Any = None, line: str = "trunk") -> SipTrunk | None:
    """(Re)start or stop ONE line to match the configuration.

    Rebuilding a line never touches the other: the extension line can be saved
    and reconnected while trunk calls are live, and vice versa."""
    from xplogent.core.config import load_config
    from xplogent.voip.service import get_service

    if line not in _LINES:
        raise ValueError(f"unknown SIP line: {line!r}")
    async with _line_lock(line):
        cfg = cfg or load_config()
        v = cfg.voip or {}
        service = get_service()
        dialer_attr = "sip_dialer" if line == "trunk" else "sip_extension_dialer"
        if _LINES[line] is not None:
            await _LINES[line].stop()
            _LINES[line] = None
            setattr(service, dialer_attr, None)
        if line == "trunk":
            if not wants_sip(v):
                return None
            settings = SipSettings.from_config(v)
        else:
            settings = SipSettings.extension_from_config(v)
            if not (settings.enabled and settings.server and settings.username):
                return None
        trunk = SipTrunk(settings, service=service, line=line)
        try:
            await trunk.start()
        except Exception as exc:  # noqa: BLE001 - reported in the status
            trunk.reg_error = f"could not start: {exc}"
            _log.warning("SIP %s line not started: %s", line, exc)
            _LINES[line] = trunk
            return trunk
        _LINES[line] = trunk
        setattr(service, dialer_attr, trunk.dial)
        return trunk


async def shutdown() -> None:
    from xplogent.voip.service import get_service

    service = get_service()
    for line, trunk in list(_LINES.items()):
        if trunk is not None:
            await trunk.stop()
            _LINES[line] = None
    service.sip_dialer = None
    service.sip_extension_dialer = None
