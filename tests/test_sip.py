"""Built-in SIP trunk: messages, digest auth, SDP, and full calls against a fake PBX."""

from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role
from xplogent.voip.audio import mulaw_to_alaw
from xplogent.voip.service import VoipService
from xplogent.voip.telephony import sip
from xplogent.voip.telephony.sip import (
    SipMessage,
    SipSettings,
    SipTrunk,
    build_sdp,
    choose_codec,
    digest_authorization,
    header_param,
    parse_challenge,
    parse_message,
    parse_sdp,
    user_of,
)


def test_parse_and_encode_round_trip():
    raw = (b"INVITE sip:2000@pbx.local SIP/2.0\r\nv: SIP/2.0/UDP 10.0.0.2:5060;branch=z9hG4bKabc;rport\r\n"
           b"f: \"Ana\" <sip:+15550101111@pbx.local>;tag=xyz\r\nt: <sip:2000@pbx.local>\r\ni: abc@pbx\r\n"
           b"CSeq: 7 INVITE\r\nSubject: long\r\n  folded value\r\nl: 4\r\n\r\nbodyEXTRA")
    msg = parse_message(raw)
    assert msg.method == "INVITE" and msg.call_id == "abc@pbx" and msg.cseq == (7, "INVITE")
    assert header_param(msg.get("From"), "tag") == "xyz" and user_of(msg.get("From")) == "+15550101111"
    assert msg.get("subject") == "long folded value" and msg.body == b"body"
    again = parse_message(msg.encode())
    assert again.get("Via") == msg.get("Via") and again.body == b"body"
    resp = parse_message(b"SIP/2.0 401 Unauthorized\r\nCSeq: 1 REGISTER\r\n\r\n")
    assert resp.status == 401 and resp.reason == "Unauthorized" and not resp.is_request


def test_digest_matches_rfc2617_example():
    challenge = parse_challenge('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                                'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
                                'opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    auth = digest_authorization("GET", "/dir/index.html", "Mufasa", "Circle Of Life", challenge,
                                cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in auth
    assert "qop=auth" in auth and "nc=00000001" in auth and 'opaque="5ccc069c' in auth


def test_sdp_offer_answer():
    sdp = parse_sdp(build_sdp("192.0.2.5", 10002, (8, 0)))
    assert sdp == {"ip": "192.0.2.5", "port": 10002, "payloads": [8, 0, 101],
                   "names": {8: "PCMA", 0: "PCMU", 101: "TELEPHONE-EVENT"}, "dtmf_pt": 101}
    assert choose_codec(sdp) == 8
    assert choose_codec(parse_sdp(b"v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 4000 RTP/AVP 18 0\r\n")) == 0
    assert choose_codec(parse_sdp(b"m=audio 4000 RTP/AVP 18\r\n")) is None


# ── a scripted PBX over real UDP ─────────────────────────────────────────────
def _free_port(kind=socket.SOCK_DGRAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakePBX(asyncio.DatagramProtocol):
    def __init__(self, password="secret"):
        self.password = password
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.strip():
            self.inbox.put_nowait((parse_message(data), addr))

    def send(self, msg: SipMessage | bytes, addr):
        self.transport.sendto(msg if isinstance(msg, bytes) else msg.encode(), addr)

    async def expect(self, pred, timeout=5.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            msg, addr = await asyncio.wait_for(self.inbox.get(), timeout=max(0.01, end - loop.time()))
            if pred(msg):
                return msg, addr

    @staticmethod
    def reply(req: SipMessage, status: int, reason: str, *, to_tag="", extra=(), body=b""):
        resp = SipMessage(status=status, reason=reason)
        to = req.get("To") + (f";tag={to_tag}" if to_tag and "tag=" not in req.get("To") else "")
        resp.headers = [("Via", v) for v in req.get_all("Via")] + [
            ("From", req.get("From")), ("To", to), ("Call-ID", req.call_id), ("CSeq", req.get("CSeq")),
            *extra]
        if body:
            resp.headers.append(("Content-Type", "application/sdp"))
            resp.body = body
        return resp

    def check_auth(self, req: SipMessage, header: str) -> bool:
        got = parse_challenge(req.get(header))
        expected = digest_authorization(req.method, got.get("uri", ""), got.get("username", ""),
                                        self.password, {"realm": "pbx", "nonce": "n0nce", "qop": "auth"},
                                        cnonce=got.get("cnonce"), nc=int(got.get("nc", "1"), 16))
        return f'response="{got.get("response")}"' in expected


class RtpPeer(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets: list[bytes] = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.packets.append(data)

    def send_frames(self, addr, payload_frames, pt):
        for i, frame in enumerate(payload_frames):
            self.transport.sendto(struct.pack("!BBHII", 0x80, pt, i, i * 160, 1234) + frame, addr)


async def _until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


@pytest.fixture
async def rig():
    loop = asyncio.get_running_loop()
    pbx = FakePBX()
    pbx_port = _free_port()
    await loop.create_datagram_endpoint(lambda: pbx, local_addr=("127.0.0.1", pbx_port))
    rtp = RtpPeer()
    rtp_port = _free_port()
    await loop.create_datagram_endpoint(lambda: rtp, local_addr=("127.0.0.1", rtp_port))
    save_user_config({"voip": {"enabled": True, "greeting": "Hello from Acme", "transfer_number": "+15550100000"}})
    service = VoipService()

    async def no_knowledge(q):
        return []

    base = _free_port()
    settings = SipSettings(server="127.0.0.1", port=pbx_port, username="2000", password="secret",
                           public_ip="127.0.0.1", local_port=_free_port(), rtp_start=base, rtp_end=base + 40,
                           ring_timeout=5)
    provider = ScriptedProvider([Message(role=Role.ASSISTANT, content="Sure, a quote is free.")])
    trunk = SipTrunk(settings, service=service, session_kw=dict(
        stt=FakeSTT("I want a quote"), tts=FakeTTS(), provider=provider, retrieve=no_knowledge))
    yield trunk, pbx, rtp, rtp_port, service
    await trunk.stop()
    pbx.transport.close()
    rtp.transport.close()


async def _register(trunk, pbx):
    await trunk.start()
    req, addr = await pbx.expect(lambda m: m.method == "REGISTER")
    assert "Authorization" not in [k for k, _ in req.headers]
    pbx.send(pbx.reply(req, 401, "Unauthorized", extra=[
        ("WWW-Authenticate", 'Digest realm="pbx", nonce="n0nce", qop="auth", algorithm=MD5')]), addr)
    req2, addr = await pbx.expect(lambda m: m.method == "REGISTER")
    assert req2.cseq[0] == req.cseq[0] + 1 and pbx.check_auth(req2, "Authorization")
    pbx.send(pbx.reply(req2, 200, "OK", extra=[
        ("Contact", f"<sip:2000@127.0.0.1:{trunk.s.local_port}>;expires=120")]), addr)
    assert await trunk.wait_registered(3)
    assert trunk.status()["registered"] and trunk.reg_expires == 120
    return addr


async def test_register_inbound_call_audio_and_bye(rig):
    trunk, pbx, rtp, rtp_port, service = rig
    ua = await _register(trunk, pbx)

    # OPTIONS keep-alive from the PBX.
    opts = SipMessage(method="OPTIONS", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKopt"), ("From", "<sip:pbx>;tag=p"),
        ("To", "<sip:2000@x>"), ("Call-ID", "opt1"), ("CSeq", "1 OPTIONS")])
    pbx.send(opts, ua)
    assert (await pbx.expect(lambda m: m.call_id == "opt1"))[0].status == 200

    sdp = (f"v=0\r\no=pbx 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
           f"m=audio {rtp_port} RTP/AVP 8 0 101\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\n").encode()
    invite = SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKinv1;rport"), ("Max-Forwards", "70"),
        ("From", '"Ana" <sip:+15550101111@127.0.0.1>;tag=caller'), ("To", "<sip:+15559990000@127.0.0.1>"),
        ("Call-ID", "call-in-1"), ("CSeq", "1 INVITE"), ("Contact", "<sip:+15550101111@127.0.0.1>"),
        ("Content-Type", "application/sdp")], body=sdp)
    pbx.send(invite, ua)
    codes = []
    while 200 not in codes:
        resp, _ = await pbx.expect(lambda m: m.call_id == "call-in-1")
        codes.append(resp.status)
    assert codes == [100, 180, 200]
    assert ";rport=" in resp.get("Via") and header_param(resp.get("To"), "tag")
    answer = parse_sdp(resp.body)
    assert answer["payloads"][0] == 8 and answer["ip"] == "127.0.0.1"
    ack = SipMessage(method="ACK", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKack1"), ("From", invite.get("From")),
        ("To", resp.get("To")), ("Call-ID", "call-in-1"), ("CSeq", "1 ACK")])
    pbx.send(ack, ua)

    await _until(lambda: service.active)                          # the call is live and controllable
    live = next(iter(service.active.values()))
    assert live.transport == "sip" and live.number == "+15550101111" and live.transfer_to is not None
    await _until(lambda: len(rtp.packets) > 20)                   # greeting audio, A-law, 20 ms frames
    assert all(p[1] & 0x7F == 8 and len(p) == 172 for p in rtp.packets[:20])

    # The caller speaks (A-law tone), then is silent → STT → reply.
    import math
    tone = bytes(int(127 + 100 * math.sin(i / 3)) & 0xFF for i in range(160))
    speech = [mulaw_to_alaw(tone)] * 25 + [mulaw_to_alaw(b"\xff" * 160)] * 60
    for i in range(0, len(speech), 5):
        rtp.send_frames(("127.0.0.1", answer["port"]), speech[i:i + 5], 8)
        await asyncio.sleep(0.1)
    row = live.call_id
    with Store(load_config().db_path) as s:
        await _until(lambda: any(t["text"] == "Sure, a quote is free." for t in s.get_call(row)["turns"]),
                     timeout=8)
        assert ("caller", "I want a quote") in [(t["speaker"], t["text"]) for t in s.get_call(row)["turns"]]

    bye = SipMessage(method="BYE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKbye1"), ("From", invite.get("From")),
        ("To", resp.get("To")), ("Call-ID", "call-in-1"), ("CSeq", "2 BYE")])
    pbx.send(bye, ua)
    assert (await pbx.expect(lambda m: m.cseq[1] == "BYE"))[0].status == 200
    await _until(lambda: not trunk.calls and not service.active)
    with Store(load_config().db_path) as s:
        await _until(lambda: s.get_call(row)["status"] == "ended")
        assert s.get_call(row)["transport"] == "sip"


async def test_outbound_call_with_proxy_auth_then_transfer(rig):
    trunk, pbx, rtp, rtp_port, service = rig
    await _register(trunk, pbx)
    dialing = asyncio.create_task(trunk.dial("+1 555 010 1111", {"test": True}))
    inv, addr = await pbx.expect(lambda m: m.method == "INVITE")
    assert inv.uri == "sip:+15550101111@127.0.0.1" and parse_sdp(inv.body)["payloads"][:2] == [0, 8]
    pbx.send(pbx.reply(inv, 407, "Proxy Authentication Required", to_tag="t407", extra=[
        ("Proxy-Authenticate", 'Digest realm="pbx", nonce="n0nce", qop="auth"')]), addr)
    ack, _ = await pbx.expect(lambda m: m.method == "ACK")
    assert ack.cseq == (inv.cseq[0], "ACK")
    inv2, addr = await pbx.expect(lambda m: m.method == "INVITE")
    assert inv2.cseq[0] == inv.cseq[0] + 1 and pbx.check_auth(inv2, "Proxy-Authorization")
    pbx.send(pbx.reply(inv2, 180, "Ringing", to_tag="far"), addr)
    res = await dialing
    assert res["ok"] and res["state"] == "ringing"
    sdp = (f"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n").encode()
    pbx.send(pbx.reply(inv2, 200, "OK", to_tag="far", body=sdp,
                       extra=[("Contact", "<sip:+15550101111@127.0.0.1>")]), addr)
    ack2, _ = await pbx.expect(lambda m: m.method == "ACK")
    assert header_param(ack2.get("To"), "tag") == "far" and ack2.cseq == (inv2.cseq[0], "ACK")
    await _until(lambda: service.active)
    live = next(iter(service.active.values()))
    assert live.test and live.direction == "outbound"
    await _until(lambda: len(rtp.packets) > 10)
    assert rtp.packets[0][1] & 0x7F == 0                          # PCMU chosen by the far end

    transfer = asyncio.create_task(live.transfer("+15550109999"))
    refer, addr = await pbx.expect(lambda m: m.method == "REFER")
    assert refer.get("Refer-To") == "<sip:+15550109999@127.0.0.1>"
    assert header_param(refer.get("To"), "tag") == "far" and refer.uri == "sip:+15550101111@127.0.0.1"
    pbx.send(pbx.reply(refer, 202, "Accepted"), addr)
    await transfer
    # The PBX reports the transfer connected: we leave the call with a BYE.
    notify = SipMessage(method="NOTIFY", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKntf1"), ("From", refer.get("To")),
        ("To", refer.get("From")), ("Call-ID", refer.call_id), ("CSeq", "2 NOTIFY"), ("Event", "refer"),
        ("Content-Type", "message/sipfrag;version=2.0")], body=b"SIP/2.0 200 OK\r\n")
    pbx.send(notify, addr)
    assert (await pbx.expect(lambda m: m.cseq[1] == "NOTIFY"))[0].status == 200
    bye, _ = await pbx.expect(lambda m: m.method == "BYE")
    assert bye.call_id == refer.call_id and bye.cseq[0] > refer.cseq[0]
    pbx.send(pbx.reply(bye, 200, "OK"), addr)
    await _until(lambda: not trunk.calls and not service.active)
    with Store(load_config().db_path) as s:
        call = s.list_calls(1)[0]
        assert call["test"] == 1 and call["outcome"] == "transferred" and call["transport"] == "sip"


async def test_failed_call_and_strangers_are_refused(rig):
    trunk, pbx, rtp, rtp_port, service = rig
    await _register(trunk, pbx)
    dialing = asyncio.create_task(trunk.dial("+15550102222"))
    inv, addr = await pbx.expect(lambda m: m.method == "INVITE")
    pbx.send(pbx.reply(inv, 486, "Busy Here", to_tag="b"), addr)
    await pbx.expect(lambda m: m.method == "ACK")
    res = await dialing
    assert not res["ok"] and "486" in res["error"] and not trunk.calls

    # An INVITE from an address that isn't the trunk server is refused.
    loop = asyncio.get_running_loop()
    stranger = FakePBX()
    await loop.create_datagram_endpoint(lambda: stranger, local_addr=("127.0.0.2", 0))
    evil = SipMessage(method="INVITE", uri="sip:100@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.2;branch=z9hG4bKevil"), ("From", "<sip:x@evil>;tag=e"),
        ("To", "<sip:2000@127.0.0.1>"), ("Call-ID", "evil"), ("CSeq", "1 INVITE")],
        body=build_sdp("127.0.0.2", 4000))
    stranger.send(evil, ("127.0.0.1", trunk.s.local_port))
    assert (await stranger.expect(lambda m: m.call_id == "evil"))[0].status == 403
    # From another address but addressed to our extension (Docker gateway, carrier proxy):
    # accepted — unless strict_source is on.
    ours = SipMessage(method="INVITE", uri=f"sip:2000@127.0.0.1:{trunk.s.local_port}", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.2;branch=z9hG4bKgw1"), ("From", "<sip:+1555@gw>;tag=g"),
        ("To", "<sip:2000@127.0.0.1>"), ("Call-ID", "gw1"), ("CSeq", "1 INVITE")],
        body=build_sdp("127.0.0.2", 4000))
    trunk.s.strict_source = True
    stranger.send(ours, ("127.0.0.1", trunk.s.local_port))
    assert (await stranger.expect(lambda m: m.call_id == "gw1"))[0].status == 403
    trunk.s.strict_source = False
    ours.set("Via", "SIP/2.0/UDP 127.0.0.2;branch=z9hG4bKgw2")
    stranger.send(ours, ("127.0.0.1", trunk.s.local_port))
    assert (await stranger.expect(lambda m: m.call_id == "gw1"))[0].status == 100
    stranger.transport.close()


async def test_sip_routes_save_and_status(monkeypatch):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    started = []

    async def fake_apply(cfg=None):
        started.append(1)
        return None

    monkeypatch.setattr(sip, "apply_settings", fake_apply)
    c = TestClient(create_app())
    bad = c.post("/voip/sip/settings", json={"server": "sip:pbx.local:5060"}).json()
    assert not bad["ok"] and "host name" in bad["error"]
    assert not c.post("/voip/sip/settings", json={"rtp_start": 20000, "rtp_end": 10000}).json()["ok"]
    res = c.post("/voip/sip/settings", json={"telephony": "both", "server": "pbx.local", "username": "2000",
                                              "password": "pw", "port": "5060", "allow_from": "10.0.0.9, 10.0.0.10"}).json()
    assert res["ok"] and started
    st = res["status"]
    assert st["telephony"] == "both" and st["configured"] and st["password_set"]
    v = load_config().voip
    assert v["sip"]["server"] == "pbx.local" and v["sip"]["port"] == 5060
    assert v["sip"]["allow_from"] == ["10.0.0.9", "10.0.0.10"]
    import os
    assert os.environ["SIP_PASSWORD"] == "pw"
    assert "pw" not in str(c.get("/config/full").json())
    items = {i["id"]: i for i in c.get("/voip/readiness").json()["items"]}
    assert "sip_account" in items and items["sip_account"]["ok"] and not items["sip_registered"]["ok"]


async def test_rtp_gaps_are_filled_with_silence():
    """PBXs stop sending RTP in silence: the gap must still reach the VAD as silence."""
    from xplogent.voip.telephony.sip import RtpStream

    got: list[bytes] = []

    async def on_audio(chunk):
        got.append(chunk)

    rtp = RtpStream(0, on_audio)
    await rtp.open(_free_port())
    rtp.start()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.sendto(struct.pack("!BBHII", 0x80, 0, 1, 160, 9) + b"\x10" * 160, ("127.0.0.1", rtp.port))
        await asyncio.sleep(0.45)
    await rtp.close()
    audio = b"".join(got)
    assert audio.startswith(b"\x10" * 160)
    assert audio[160:].count(b"\xff") >= 160 * 15            # ≥ 300 ms of silence filled in


async def test_sip_dial_accepts_pbx_extensions():
    svc = VoipService()
    dialed = []

    async def dialer(number, meta):
        dialed.append((number, meta))
        return {"ok": True}

    svc.sip_dialer = dialer
    assert (await svc.dial("100", via="sip", test=True))["ok"]
    assert (await svc.dial("*43", via="sip", test=True))["ok"]
    assert not (await svc.dial("100", via="telnyx", test=True))["ok"]     # not a phone number
    assert [d[0] for d in dialed] == ["100", "*43"] and dialed[0][1]["transport"] == "sip"


async def test_record_route_order_follows_rfc3261(rig):
    """Two proxies in the path: in-dialog requests must carry the right Route set."""
    trunk, pbx, rtp, rtp_port, service = rig
    ua = await _register(trunk, pbx)
    sdp = (f"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {rtp_port} RTP/AVP 0\r\n").encode()
    # Inbound (we are the UAS): Route = Record-Route in the order received (§12.1.1).
    invite = SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKrr1"), ("Record-Route", "<sip:p1.test;lr>, <sip:p2.test;lr>"),
        ("From", "<sip:+15550101111@127.0.0.1>;tag=c"), ("To", "<sip:2000@127.0.0.1>"),
        ("Call-ID", "rr-in"), ("CSeq", "1 INVITE"), ("Contact", "<sip:+15550101111@10.9.9.9>")], body=sdp)
    pbx.send(invite, ua)
    ok, _ = await pbx.expect(lambda m: m.call_id == "rr-in" and m.status == 200)
    pbx.send(SipMessage(method="ACK", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKrr2"), ("From", invite.get("From")),
        ("To", ok.get("To")), ("Call-ID", "rr-in"), ("CSeq", "1 ACK")]), ua)
    await _until(lambda: "rr-in" in trunk.calls and trunk.calls["rr-in"].acked.is_set())
    hang = asyncio.create_task(trunk.hangup(trunk.calls["rr-in"]))
    bye, addr = await pbx.expect(lambda m: m.method == "BYE" and m.call_id == "rr-in")
    assert bye.get_all("Route") == ["<sip:p1.test;lr>", "<sip:p2.test;lr>"]
    assert bye.uri == "sip:+15550101111@10.9.9.9"                 # the caller's Contact
    pbx.send(pbx.reply(bye, 200, "OK"), addr)
    await hang

    # Outbound (we are the UAC): Route = Record-Route reversed (§12.1.2).
    dialing = asyncio.create_task(trunk.dial("+15550102222"))
    inv, addr = await pbx.expect(lambda m: m.method == "INVITE")
    pbx.send(pbx.reply(inv, 200, "OK", to_tag="far", body=sdp, extra=[
        ("Record-Route", "<sip:p2.test;lr>"), ("Record-Route", "<sip:p1.test;lr>"),
        ("Contact", "<sip:+15550102222@10.8.8.8>")]), addr)
    ack, _ = await pbx.expect(lambda m: m.method == "ACK")
    assert ack.get_all("Route") == ["<sip:p1.test;lr>", "<sip:p2.test;lr>"]
    assert ack.uri == "sip:+15550102222@10.8.8.8"
    assert (await dialing)["ok"]


async def test_failed_refer_resumes_the_agent_and_bad_sdp_gets_400(rig):
    trunk, pbx, rtp, rtp_port, service = rig
    save_user_config({"voip": {"transfer_number": "+15550109999"}})
    await _register(trunk, pbx)
    # A malformed offer is answered with 400, not silence.
    bad = SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKbad1"), ("From", "<sip:+1555@127.0.0.1>;tag=b"),
        ("To", "<sip:2000@127.0.0.1>"), ("Call-ID", "bad-sdp"), ("CSeq", "1 INVITE")],
        body=b"v=0\r\nm=audio notaport RTP/AVP 0\r\n")
    pbx.send(bad, ("127.0.0.1", trunk.s.local_port))
    codes = []
    while not codes or codes[-1] < 200:
        codes.append((await pbx.expect(lambda m: m.call_id == "bad-sdp"))[0].status)
    assert codes[-1] == 400 and "bad-sdp" not in trunk.calls

    # Outbound call, transfer accepted (202) but the PBX reports it failed (NOTIFY 486).
    dialing = asyncio.create_task(trunk.dial("+15550101111"))
    inv, addr = await pbx.expect(lambda m: m.method == "INVITE")
    sdp = (f"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {rtp_port} RTP/AVP 0\r\n").encode()
    pbx.send(pbx.reply(inv, 200, "OK", to_tag="far", body=sdp,
                       extra=[("Contact", "<sip:+15550101111@127.0.0.1>")]), addr)
    await pbx.expect(lambda m: m.method == "ACK")
    await dialing
    await _until(lambda: service.active)
    live = next(iter(service.active.values()))
    transfer = asyncio.create_task(live.transfer("+15550109999"))
    refer, addr = await pbx.expect(lambda m: m.method == "REFER")
    pbx.send(pbx.reply(refer, 202, "Accepted"), addr)
    await transfer
    assert live.session.transfer_pending and live.brain.paused
    notify = SipMessage(method="NOTIFY", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKntf9"), ("From", refer.get("To")),
        ("To", refer.get("From")), ("Call-ID", refer.call_id), ("CSeq", "2 NOTIFY"), ("Event", "refer")],
        body=b"SIP/2.0 486 Busy Here\r\n")
    pbx.send(notify, addr)
    await _until(lambda: not live.session.transfer_pending)
    assert not live.brain.paused and refer.call_id in trunk.calls      # still on the call
    assert "486" in trunk.last_error
    with pytest.raises(asyncio.TimeoutError):
        await pbx.expect(lambda m: m.method == "BYE", timeout=0.6)    # we did not hang up


async def test_unacknowledged_answer_is_ended_with_bye(rig):
    """RFC 3261 §13.3.1.4: no ACK → the dialog is confirmed and the session ends with BYE."""
    trunk, pbx, rtp, rtp_port, service = rig
    trunk.ack_timeout = 1.0
    await _register(trunk, pbx)
    sdp = (f"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {rtp_port} RTP/AVP 0\r\n").encode()
    pbx.send(SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKna1"), ("From", "<sip:+1555@127.0.0.1>;tag=n"),
        ("To", "<sip:2000@127.0.0.1>"), ("Call-ID", "no-ack"), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:+1555@127.0.0.1>")], body=sdp), ("127.0.0.1", trunk.s.local_port))
    bye, addr = await pbx.expect(lambda m: m.method == "BYE" and m.call_id == "no-ack", timeout=5)
    pbx.send(pbx.reply(bye, 200, "OK"), addr)
    await _until(lambda: "no-ack" not in trunk.calls and not service.active)
