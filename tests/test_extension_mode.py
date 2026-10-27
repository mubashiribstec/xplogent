"""Testing with only a FreePBX extension: no Telnyx, test callers while the agent is off."""

from __future__ import annotations

import asyncio

import pytest

from tests.test_sip import _register, _until, rig  # noqa: F401 - rig is a fixture
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip.readiness import checklist
from xplogent.voip.telephony.sip import SipSettings, wants_sip
from xplogent.voip.telephony.sip_proto import SipMessage

ENV = {"SIP_EXTENSION_PASSWORD": "x", "OPENAI_API_KEY": "k"}


def _ids(v, ext_status=None):
    return {i["id"]: i for i in checklist(v, "openai:x", env=ENV, sip_extension_status=ext_status, gpu=True)}


def test_extension_mode_needs_no_telnyx_and_no_trunk():
    v = {"telephony": "extension", "sip_extension": {"enabled": True, "server": "voip.example.com", "username": "505"}}
    items = _ids(v, {"registered": True})
    assert not {"public_url", "webhook_secret", "telnyx_key", "sip_account", "sip_registered"} & set(items)
    assert items["sip_extension_account"]["ok"] and items["sip_extension_registered"]["ok"]
    assert not wants_sip(v)                                       # the trunk never starts in this mode


def test_extension_mode_with_the_extension_off_says_so():
    items = _ids({"telephony": "extension", "sip_extension": {"enabled": False}})
    assert not items["sip_extension_account"]["ok"] and items["sip_extension_account"]["required"]
    assert "switch it on" in items["sip_extension_account"]["detail"]


def test_other_modes_are_unchanged():
    items = _ids({"telephony": "telnyx"})
    assert {"public_url", "webhook_secret", "telnyx_key"} <= set(items)
    assert "sip_extension_account" not in items


def test_test_callers_parse():
    s = SipSettings.extension_from_config({"sip_extension": {"test_callers": "201, 202"}})
    assert s.test_callers == ["201", "202"]


def test_settings_and_dial_default_to_the_extension(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app
    from xplogent.voip.service import get_service

    c = TestClient(create_app(), base_url="http://127.0.0.1")
    assert c.post("/voip/sip/settings", json={"telephony": "extension"}).json()["ok"]
    assert (load_config().voip or {})["telephony"] == "extension"
    bad = c.post("/voip/sip/extension/settings", json={"test_callers": "201, nope"}).json()
    assert not bad["ok"] and "test callers" in bad["error"]
    seen = []

    async def fake_dial(number, **kw):
        seen.append((number, kw["via"]))
        return {"ok": True}

    monkeypatch.setattr(get_service(), "dial", fake_dial)
    assert c.post("/voip/dial", json={"number": "201", "test": True}).json()["ok"]
    assert seen == [("201", "extension")]


def _invite(call_id, caller, rtp_port):
    sdp = (f"v=0\r\no=pbx 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
           f"m=audio {rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n").encode()
    return SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", f"SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK{call_id};rport"), ("Max-Forwards", "70"),
        ("From", f"<sip:{caller}@127.0.0.1>;tag=c{call_id}"), ("To", "<sip:2000@127.0.0.1>"),
        ("Call-ID", call_id), ("CSeq", "1 INVITE"), ("Contact", f"<sip:{caller}@127.0.0.1>"),
        ("Content-Type", "application/sdp")], body=sdp)


async def test_agent_off_refuses_strangers_but_answers_test_callers(rig):  # noqa: F811
    trunk, pbx, rtp, rtp_port, service = rig
    save_user_config({"voip": {"enabled": False}})
    trunk.s.test_callers = ["201"]
    ua = await _register(trunk, pbx)

    pbx.send(_invite("stranger-1", "305", rtp_port), ua)
    while True:
        resp, _ = await pbx.expect(lambda m: m.call_id == "stranger-1")
        if resp.status >= 200:
            break
    assert resp.status == 480                                     # agent off: not answered

    invite = _invite("tester-1", "201", rtp_port)
    pbx.send(invite, ua)
    while True:
        resp, _ = await pbx.expect(lambda m: m.call_id == "tester-1")
        if resp.status >= 200:
            break
    assert resp.status == 200                                     # a listed test extension is answered
    pbx.send(SipMessage(method="ACK", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKt2"), ("From", invite.get("From")),
        ("To", resp.get("To")), ("Call-ID", "tester-1"), ("CSeq", "1 ACK")]), ua)
    await _until(lambda: service.active)
    live = next(iter(service.active.values()))
    with Store(load_config().db_path) as store:
        assert store.get_call(live.call_id)["test"] == 1              # not counted, never sent to the CRM
    await live.hangup()
    await asyncio.sleep(0.05)


def test_test_callers_is_a_known_setting():
    from xplogent.voip.config_check import check_voip

    problems = check_voip({"sip_extension": {"test_callers": ["201"]}})
    assert not any("test_callers" in p for p in problems)
