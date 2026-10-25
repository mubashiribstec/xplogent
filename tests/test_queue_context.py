"""FreePBX queue context: who called, what they dialled, and whether a queue sent them."""

from __future__ import annotations

import asyncio

from tests.test_sip import _register, _until, rig  # noqa: F401 - rig is a fixture
from xplogent.core.config import load_config
from xplogent.memory.store import Store
from xplogent.voip.telephony.sip_proto import SipMessage, call_identity, header_param, parse_sdp


def _invite(*extra, frm='"Ana" <sip:201@pbx>;tag=a', to="<sip:2000@pbx>"):
    return SipMessage(method="INVITE", uri="sip:2000@pbx", headers=[
        ("From", frm), ("To", to), ("Call-ID", "q1"), ("CSeq", "1 INVITE"), *extra])


def test_caller_and_original_number_are_kept_apart():
    msg = _invite(("P-Asserted-Identity", '"Ana Ruiz" <sip:+447700900123@pbx>'),
                  ("Diversion", "<sip:600@pbx>;reason=no-answer;counter=1"),
                  ("Diversion", "<sip:+442071234567@pbx>;reason=unconditional"))
    ident = call_identity(msg, trusted=True, queue_numbers=["600"])
    assert ident["caller"] == "+447700900123"                  # PAI wins over From
    assert ident["original_called_number"] == "+442071234567"  # oldest Diversion = what they dialled
    assert ident["diversion_reason"] == "unconditional"
    assert ident["via_queue"] is True                          # 600 is a configured queue


def test_remote_party_id_then_from():
    rpid = call_identity(_invite(("Remote-Party-ID", "<sip:+15550001111@pbx>;party=calling")),
                         trusted=True)
    assert rpid["caller"] == "+15550001111"
    assert call_identity(_invite(), trusted=True)["caller"] == "201"


def test_bare_diversion_is_forwarding_not_a_queue():
    ident = call_identity(_invite(("Diversion", "<sip:101@pbx>;reason=no-answer")), trusted=True,
                          queue_numbers=["600"])
    assert ident["original_called_number"] == "101" and ident["via_queue"] is False


def test_freepbx_queue_headers_and_history_info():
    ident = call_identity(_invite(("History-Info", "<sip:+442071234567@pbx>;index=1"),
                                  ("History-Info", "<sip:600@pbx>;index=1.1"),
                                  ("X-Xplogent-Via", "queue"), ("X-Xplogent-Queue-Wait", "42")),
                          trusted=True)
    assert ident["original_called_number"] == "+442071234567"
    assert ident["via_queue"] is True and ident["queue_wait_s"] == 42


def test_headers_from_a_stranger_are_ignored():
    msg = _invite(("P-Asserted-Identity", "<sip:+15551234567@evil>"),
                  ("Diversion", "<sip:600@evil>"), ("X-Xplogent-Via", "queue"))
    ident = call_identity(msg, trusted=False, queue_numbers=["600"])
    assert ident["caller"] == "201"                            # spoofed caller ID ignored
    assert ident["via_queue"] is False and ident["original_called_number"] == ""


async def test_queue_call_reaches_record_brain_and_crm(rig):  # noqa: F811
    from xplogent.voip import crm

    trunk, pbx, rtp, rtp_port, service = rig
    trunk.s.queue_numbers = ["600"]
    ua = await _register(trunk, pbx)
    sdp = (f"v=0\r\no=pbx 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
           f"m=audio {rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n").encode()
    invite = SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKq1;rport"), ("Max-Forwards", "70"),
        ("From", '"Ana" <sip:201@127.0.0.1>;tag=caller'), ("To", "<sip:2000@127.0.0.1>"),
        ("P-Asserted-Identity", "<sip:+447700900123@127.0.0.1>"),
        ("Diversion", "<sip:600@127.0.0.1>;reason=no-answer"),
        ("Diversion", "<sip:+442071234567@127.0.0.1>;reason=unconditional"),
        ("X-Xplogent-Queue-Wait", "35"),
        ("Call-ID", "queue-1"), ("CSeq", "1 INVITE"), ("Contact", "<sip:201@127.0.0.1>"),
        ("Content-Type", "application/sdp")], body=sdp)
    pbx.send(invite, ua)
    while True:
        resp, _ = await pbx.expect(lambda m: m.call_id == "queue-1")
        if resp.status == 200:
            break
    assert parse_sdp(resp.body)["port"] and header_param(resp.get("To"), "tag")
    pbx.send(SipMessage(method="ACK", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKq2"), ("From", invite.get("From")),
        ("To", resp.get("To")), ("Call-ID", "queue-1"), ("CSeq", "1 ACK")]), ua)
    await _until(lambda: service.active)
    live = next(iter(service.active.values()))
    assert live.number == "+447700900123" and live.via_queue and live.summary()["via_queue"]
    assert "waited in the office phone queue for about 35 seconds" in live.brain.persona
    assert "dialled +442071234567" in live.brain.persona
    with Store(load_config().db_path) as store:
        row = store.get_call(live.call_id)
        assert (row["from_number"], row["original_called_number"], row["via_queue"],
                row["diversion_reason"], row["queue_wait_s"]) == (
            "+447700900123", "+442071234567", 1, "unconditional", 35)
        payload = crm.build_payload(load_config(), store, live.call_id, {"outcome": "x"},
                                    live.brain, live.session)
    assert payload["via_queue"] is True and payload["original_called_number"] == "+442071234567"
    assert payload["diversion_reason"] == "unconditional" and payload["queue_wait_s"] == 35
    await live.hangup()
    await asyncio.sleep(0.05)
