"""SIP protocol pieces: message codec, digest authentication (RFC 2617/7616), SDP.

Pure functions and a message class, no I/O: the trunk in :mod:`.sip` uses them.
"""

from __future__ import annotations

import hashlib
import random
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

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


def split_header_values(values: list[str]) -> list[str]:
    """Split comma-separated header values, ignoring commas inside ``<…>`` or quotes."""
    out: list[str] = []
    for value in values:
        depth, quoted, start = 0, False, 0
        for i, ch in enumerate(value):
            if ch == '"':
                quoted = not quoted
            elif not quoted and ch == "<":
                depth += 1
            elif not quoted and ch == ">":
                depth -= 1
            elif ch == "," and depth == 0 and not quoted:
                out.append(value[start:i].strip())
                start = i + 1
        if value[start:].strip():
            out.append(value[start:].strip())
    return out


def record_routes(msg: SipMessage) -> list[str]:
    """Record-Route entries in message order (one header may carry several).

    RFC 3261: the UAS uses them in this order (§12.1.1), the UAC reversed (§12.1.2).
    """
    return split_header_values(msg.get_all("Record-Route"))


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
              session_id: int | None = None, dtmf_pt: int | None = DTMF_PT) -> bytes:
    """An audio offer/answer. ``dtmf_pt`` None leaves out keypad events (telephone-event)."""
    sid = session_id or random.randint(10**6, 10**9)
    fmt = " ".join(str(p) for p in (*payloads, *([dtmf_pt] if dtmf_pt is not None else [])))
    lines = ["v=0", f"o=xplogent {sid} {sid} IN IP4 {ip}", "s=Xplogent", f"c=IN IP4 {ip}", "t=0 0",
             f"m=audio {port} RTP/AVP {fmt}"]
    lines += [f"a=rtpmap:{p} {CODEC_NAMES[p]}/8000" for p in payloads]
    if dtmf_pt is not None:
        lines += [f"a=rtpmap:{dtmf_pt} telephone-event/8000", f"a=fmtp:{dtmf_pt} 0-16"]
    lines += ["a=ptime:20", "a=sendrecv"]
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
                if len(parts) < 2 or not parts[1].isdigit():
                    raise ValueError(f"bad m= line: {line}")
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
    dtmf = next((p for p in payloads if names.get(p) == "TELEPHONE-EVENT"), None)
    return {"ip": media_ip or ip, "port": port, "payloads": payloads, "names": names, "dtmf_pt": dtmf}


def choose_codec(sdp: dict[str, Any]) -> int | None:
    """First of the far end's codecs we speak (keeps their preference order)."""
    for p in sdp.get("payloads", []):
        name = sdp.get("names", {}).get(p) or CODEC_NAMES.get(p, "")
        if name == "PCMU":
            return PCMU
        if name == "PCMA":
            return PCMA
    return None
