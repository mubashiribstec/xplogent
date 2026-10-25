"""Call Debug view: SIP ladder (never a digest), RTP stats, timeline, redacted bundle, API, CLI."""

from __future__ import annotations

import json

import pytest

from tests.test_sip import _register, _until, rig  # noqa: F401 - rig is a fixture
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip import debug
from xplogent.voip.telephony.sip_proto import SipMessage, parse_sdp


def _msg(method="INVITE", **extra):
    return SipMessage(method=method, uri="sip:2000@pbx", headers=[
        ("From", "<sip:201@pbx>;tag=a"), ("To", "<sip:2000@pbx>"), ("Call-ID", "c1"),
        ("CSeq", f"1 {method}"), *extra.items()])


def test_trace_never_keeps_credentials_and_is_bounded():
    trace = debug.SipTrace()
    trace.add("out", _msg(**{"Proxy-Authorization": 'Digest username="2000", response="abc123"',
                             "Authorization": 'Digest response="zzz"'}), ("10.0.0.1", 5060))
    dump = json.dumps(trace.export())
    assert "Digest" not in dump and "abc123" not in dump and "INVITE sip:2000@pbx" in dump
    for _ in range(300):
        trace.add("in", _msg("INFO"))
    out = trace.export()
    assert len(out["messages"]) <= debug.MAX_MESSAGES and out["dropped"] > 0


def test_bundle_redacts_config_and_scrubs_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setenv("TELNYX_API_KEY", "KEYsuperSecret0001")
    save_user_config({"voip": {"webhook_secret": "hooksecret123456", "company": "Acme",
                               "crm": {"api_token_env": "XPLOGENT_CRM_API_TOKEN"}}})
    cfg = load_config()
    with Store(cfg.db_path) as store:
        cid = store.start_call("inbound", "+15550001111", "2000", transport="sip")
        store.save_call_debug(cid, {"turns": [{"error": "auth failed with KEYsuperSecret0001"}],
                                    "timeline": [{"t": 0.0, "kind": "answered", "detail": ""}]})
        doc = debug.bundle(cfg, store, cid)
    text = json.dumps(doc)
    assert "hooksecret123456" not in text and "KEYsuperSecret0001" not in text
    assert doc["config"]["voip"]["webhook_secret"] == "[redacted]"
    assert doc["config"]["voip"]["crm"]["api_token_env"] == "XPLOGENT_CRM_API_TOKEN"   # names stay
    assert doc["config"]["voip"]["company"] == "Acme" and "recording" not in doc["call"]
    lines = debug.summary_lines(doc)
    assert lines[0].startswith(f"Call {cid}") and any("answered" in line for line in lines)


async def test_sip_call_ladder_rtp_and_timeline_are_saved(rig):  # noqa: F811
    trunk, pbx, rtp, rtp_port, service = rig
    ua = await _register(trunk, pbx)
    sdp = (f"v=0\r\no=pbx 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
           f"m=audio {rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n").encode()
    invite = SipMessage(method="INVITE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKd1;rport"), ("Max-Forwards", "70"),
        ("From", "<sip:201@127.0.0.1>;tag=caller"), ("To", "<sip:2000@127.0.0.1>"),
        ("Proxy-Authorization", 'Digest username="201", response="deadbeef"'),
        ("Call-ID", "dbg-1"), ("CSeq", "1 INVITE"), ("Contact", "<sip:201@127.0.0.1>"),
        ("Content-Type", "application/sdp")], body=sdp)
    pbx.send(invite, ua)
    while True:
        resp, _ = await pbx.expect(lambda m: m.call_id == "dbg-1")
        if resp.status == 200:
            break
    assert parse_sdp(resp.body)["port"]
    pbx.send(SipMessage(method="ACK", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKd2"), ("From", invite.get("From")),
        ("To", resp.get("To")), ("Call-ID", "dbg-1"), ("CSeq", "1 ACK")]), ua)
    await _until(lambda: service.active)
    live = next(iter(service.active.values()))
    await _until(lambda: len(rtp.packets) > 10)
    rtp.send_frames(("127.0.0.1", parse_sdp(resp.body)["port"]), [b"\xff" * 160] * 5, 0)
    await _until(lambda: live.session.debug_sources and debug.collect(live.session)["rtp"]["packets_in"] >= 5)
    pbx.send(SipMessage(method="BYE", uri="sip:2000@127.0.0.1", headers=[
        ("Via", "SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKd3"), ("From", invite.get("From")),
        ("To", resp.get("To")), ("Call-ID", "dbg-1"), ("CSeq", "2 BYE")]), ua)
    with Store(load_config().db_path) as store:
        await _until(lambda: store.get_call_debug(live.call_id) is not None, timeout=8)
        data = store.get_call_debug(live.call_id)
    lines = [m["line"].split(" ")[0] for m in data["sip"]["messages"]]
    assert lines[:4] == ["INVITE", "100", "180", "200"] and "ACK" in lines and "BYE" in lines
    assert lines.index("ACK") < lines.index("BYE")
    assert "deadbeef" not in json.dumps(data) and "Digest" not in json.dumps(data)
    assert data["rtp"]["codec"] == "PCMU" and data["rtp"]["packets_in"] >= 5 and data["rtp"]["packets_out"] > 10
    kinds = [e["kind"] for e in data["timeline"]]
    assert kinds[0] == "answered" and "greeting" in kinds and "ended" in kinds
    assert data["line"] == "trunk" and not trunk.traces          # trace handed over, then dropped


def test_rtp_quality_counts_loss_and_reordering():
    import struct

    from xplogent.voip.telephony.rtp import RtpStream

    s = RtpStream()
    for seq in (1, 2, 5, 4, 6):                                  # 3 lost, 4 late
        s.datagram_received(struct.pack("!BBHII", 0x80, 0, seq, seq * 160, 1) + b"\xff" * 160, ("1.2.3.4", 4000))
    st = s.stats()
    assert st["packets_in"] == 5 and st["lost"] == 1 and st["late"] == 1 and st["remote"] == "1.2.3.4:4000"


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    return TestClient(create_app())


def test_debug_api_and_bundle_download(client):
    with Store(load_config().db_path) as store:
        cid = store.start_call("inbound", "+15550001111", "2000", transport="sip")
        store.save_call_debug(cid, {"turns": [{"stt_ms": 120.0, "total_ms": 900.0, "fallbacks": ["tts:fallback"],
                                               "error": "tts: provider down"}]})
    got = client.get(f"/voip/calls/{cid}/debug").json()
    assert got["ok"] and got["kept"] and not got["live"] and got["debug"]["turns"][0]["error"] == "tts: provider down"
    resp = client.get(f"/voip/calls/{cid}/debug/bundle")
    assert resp.status_code == 200 and "attachment" in resp.headers["content-disposition"]
    assert resp.json()["bundle"] == "xplogent-call-debug/1"
    assert client.get("/voip/calls/99999/debug").status_code == 404
    with Store(load_config().db_path) as store:
        assert store.audit_rows(20, action="voip_debug_bundle")
