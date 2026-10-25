"""Capacity safety: warn before a SIP line is full, and count callers who got busy."""

from __future__ import annotations

from tests.test_sip import _register, rig  # noqa: F401 - rig is a fixture
from xplogent.voip.readiness import checklist
from xplogent.voip.telephony.sip_proto import SipMessage

V = {"telephony": "sip", "sip": {"server": "pbx", "username": "2000"}}


def _items(st):
    return {i["id"]: i for i in checklist(V, "ollama:x", env={"SIP_PASSWORD": "x"}, sip_status=st)}


def test_capacity_warns_near_full_and_after_busy():
    base = {"running": True, "registered": True, "max_calls": 4}
    assert _items({**base, "calls": 1})["sip_capacity"]["ok"]
    near = _items({**base, "calls": 3})["sip_capacity"]
    assert not near["ok"] and not near["required"] and "3 of 4" in near["detail"]
    busy = _items({**base, "calls": 0, "busy_24h": 2})["sip_capacity"]
    assert not busy["ok"] and "2 callers got busy" in busy["detail"]
    assert "sip_capacity" not in _items({"running": False})        # line not running: nothing to say


async def test_full_line_counts_the_busy_caller(rig):  # noqa: F811
    trunk, pbx, _rtp, rtp_port, _service = rig
    ua = await _register(trunk, pbx)
    trunk.s.max_calls = 0                                          # already full
    invite = SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKbusy;rport"), ("From", "<sip:201@127.0.0.1>;tag=b"),
        ("To", "<sip:2000@127.0.0.1>"), ("Call-ID", "busy-1"), ("CSeq", "1 INVITE"),
        ("Content-Type", "application/sdp")], body=b"v=0\r\n")
    pbx.send(invite, ua)
    resp, _ = await pbx.expect(lambda m: m.call_id == "busy-1")
    assert resp.status == 486
    st = trunk.status()
    assert st["busy_24h"] == 1 and st["last_busy_at"] and st["max_calls"] == 0
    trunk.clear_busy()
    assert trunk.status()["busy_24h"] == 0
