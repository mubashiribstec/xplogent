"""Audit P2/P1: in-dialog SIP requests and RTP must come from the call's own dialog/peer."""

from __future__ import annotations

import asyncio
import struct

from tests.test_sip import FakePBX, _register, _until, rig  # noqa: F401 - pytest fixture
from xplogent.voip.telephony.sip_proto import SipMessage, header_param, parse_sdp


async def _call(trunk, pbx, ua, rtp_port, call_id):
    sdp = (f"v=0\r\no=pbx 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
           f"m=audio {rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n").encode()
    invite = SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", f"SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK{call_id}"), ("Max-Forwards", "70"),
        ("From", '"Ana" <sip:+15550101111@127.0.0.1>;tag=caller'), ("To", "<sip:+15559990000@127.0.0.1>"),
        ("Call-ID", call_id), ("CSeq", "1 INVITE"), ("Contact", "<sip:+15550101111@127.0.0.1>"),
        ("Content-Type", "application/sdp")], body=sdp)
    pbx.send(invite, ua)
    while True:
        resp, _ = await pbx.expect(lambda m: m.call_id == call_id)
        if resp.status == 200:
            break
    pbx.send(SipMessage(method="ACK", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKack"), ("From", invite.get("From")),
        ("To", resp.get("To")), ("Call-ID", call_id), ("CSeq", "1 ACK")]), ua)
    return invite, resp


def _req(method, call_id, from_, to, branch, cseq=2):
    return SipMessage(method=method, uri="sip:2000@127.0.0.1", headers=[
        ("Via", f"SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK{branch}"), ("From", from_), ("To", to),
        ("Call-ID", call_id), ("CSeq", f"{cseq} {method}")])


async def test_spoofed_dialog_requests_and_rtp_are_refused(rig):  # noqa: F811
    trunk, pbx, rtp, rtp_port, service = rig
    ua = await _register(trunk, pbx)
    invite, ok = await _call(trunk, pbx, ua, rtp_port, "dlg-1")
    await _until(lambda: service.active)
    ours = header_param(ok.get("To"), "tag")

    # Same address, wrong tags (guessed Call-ID only): refused, the call stays up.
    pbx.send(_req("BYE", "dlg-1", "<sip:x@127.0.0.1>;tag=guess", ok.get("To"), "b1"), ua)
    assert (await pbx.expect(lambda m: m.cseq[1] == "BYE"))[0].status == 481
    pbx.send(_req("INVITE", "dlg-1", invite.get("From"), "<sip:+15559990000@127.0.0.1>;tag=nope", "r1"), ua)
    assert (await pbx.expect(lambda m: m.cseq[1] == "INVITE" and m.status >= 200))[0].status == 481
    assert "dlg-1" in trunk.calls

    # Right tags, but from a stranger's address.
    loop = asyncio.get_running_loop()
    stranger = FakePBX()
    await loop.create_datagram_endpoint(lambda: stranger, local_addr=("127.0.0.2", 0))
    stranger.send(_req("BYE", "dlg-1", invite.get("From"), ok.get("To"), "b2"), ua)
    assert (await stranger.expect(lambda m: m.cseq[1] == "BYE"))[0].status == 481
    stranger.send(_req("NOTIFY", "dlg-1", invite.get("From"), ok.get("To"), "n1"), ua)
    assert (await stranger.expect(lambda m: m.cseq[1] == "NOTIFY"))[0].status == 481
    assert "dlg-1" in trunk.calls

    # RTP: the caller's media is learnt from the PBX side; a stranger's packets are dropped.
    call = trunk.calls["dlg-1"]
    rtp.send_frames(("127.0.0.1", call.rtp.port), [b"\xff" * 160] * 3, 0)
    await _until(lambda: call.rtp.received >= 3)
    pkt = struct.pack("!BBHII", 0x80, 0, 99, 99 * 160, 66) + b"\x00" * 160
    stranger.transport.sendto(pkt, ("127.0.0.1", call.rtp.port))
    await _until(lambda: call.rtp.rejected >= 1)
    assert call.rtp.remote[0] == "127.0.0.1" and call.rtp.remote[1] == rtp_port
    stranger.transport.close()

    # The real BYE still works.
    pbx.send(_req("BYE", "dlg-1", invite.get("From"), ok.get("To"), "b3"), ua)
    assert (await pbx.expect(lambda m: m.cseq[1] == "BYE" and m.status == 200))[0].status == 200
    await _until(lambda: "dlg-1" not in trunk.calls)
    assert ours


async def test_reinvite_with_new_media_moves_the_audio(rig):  # noqa: F811
    trunk, pbx, rtp, rtp_port, service = rig
    ua = await _register(trunk, pbx)
    invite, ok = await _call(trunk, pbx, ua, rtp_port, "dlg-2")
    await _until(lambda: "dlg-2" in trunk.calls)
    sdp = (b"v=0\r\no=pbx 1 2 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
           b"m=audio 49170 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n")
    re = _req("INVITE", "dlg-2", invite.get("From"), ok.get("To"), "re2")
    re.headers.append(("Content-Type", "application/sdp"))
    re.body = sdp
    pbx.send(re, ua)
    resp, _ = await pbx.expect(lambda m: m.cseq[1] == "INVITE" and m.status >= 200 and m.cseq[0] == 2)
    assert resp.status == 200 and parse_sdp(resp.body)["port"] == trunk.calls["dlg-2"].rtp.port
    assert trunk.calls["dlg-2"].rtp.remote == ("127.0.0.1", 49170)
