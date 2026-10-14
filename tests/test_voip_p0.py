"""Regression tests for the phone-agent P0 fixes."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.config import save_user_config
from xplogent.voip import service as service_mod
from xplogent.voip.control import TRANSFER_FAILED_LINE, LiveCall
from xplogent.voip.telephony import telnyx_trunk
from xplogent.voip.telephony.sip import SipMessage, record_routes, split_header_values
from xplogent.voip.telephony.telnyx import clear_message, media_message, verify_signature


# ── 1. Telnyx frames: exactly the schema (additionalProperties: false) ────────────
def test_telnyx_outbound_frames_have_no_extra_keys():
    media = json.loads(media_message(b"\xff" * 160))
    assert set(media) == {"event", "media"} and set(media["media"]) == {"payload"}
    assert base64.b64decode(media["media"]["payload"]) == b"\xff" * 160
    assert json.loads(clear_message()) == {"event": "clear"}


# ── 2. failed dashboard transfer keeps the agent talking ─────────────────────────
async def test_failed_transfer_resumes_the_agent():
    """Real CallSession: a transfer that fails at once leaves the agent talking."""
    from tests.test_voip import FakeTTS, make_session, settle

    async def broken(number):
        raise RuntimeError("carrier said no")

    session, sent, cleared, turns, hung = make_session(ScriptedProvider([]), tts=FakeTTS(), realtime=False)
    live = LiveCall(call_id=1, session=session, brain=session.brain, transfer_to=broken)
    with pytest.raises(RuntimeError):
        await live.transfer("+15550100000")
    await settle(session)
    assert not session.brain.paused and not session.transfer_pending and not session.finished.is_set()
    assert turns[-1] == ("agent", TRANSFER_FAILED_LINE) and session.brain.state.transfer_failed

    moved = []

    async def ok(number):
        moved.append(number)

    live.transfer_to = ok
    await live.transfer("+15550100000")
    assert moved and session.transfer_pending and session.brain.paused     # waiting for the carrier
    session.transfer_done()
    assert session.finished.is_set() and session.brain.state.outcome == "transferred"
    await session.stop()


# ── 3. Record-Route: multi-value headers, RFC 3261 order ──────────────────────────
def test_record_route_values_are_split_in_order():
    msg = SipMessage(method="INVITE", uri="sip:x", headers=[
        ("Record-Route", '<sip:p1.example;lr>, "Proxy, Two" <sip:p2.example;lr>'),
        ("Record-Route", "<sip:p3.example;lr>")])
    assert record_routes(msg) == ['<sip:p1.example;lr>', '"Proxy, Two" <sip:p2.example;lr>',
                                  '<sip:p3.example;lr>']
    assert split_header_values(["<sip:a;x=1,2>"]) == ["<sip:a;x=1,2>"]


# ── 4. Telnyx webhook signatures ─────────────────────────────────────────────────
@pytest.fixture
def ed25519():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(body: bytes, ts: int):
        return base64.b64encode(priv.sign(f"{ts}|".encode() + body)).decode()

    return base64.b64encode(pub).decode(), sign


def test_verify_signature(ed25519):
    pub, sign = ed25519
    now = int(time.time())
    body = b'{"data":{}}'
    assert verify_signature(pub, sign(body, now), str(now), body) == ""
    assert verify_signature(pub, sign(body, now), str(now), b'{"data":{"x":1}}') == "invalid signature"
    assert "old" in verify_signature(pub, sign(body, now - 900), str(now - 900), body)
    assert verify_signature(pub, "", str(now), body) == "missing signature headers"
    assert "encoding" in verify_signature("not-a-key", sign(body, now), str(now), body)


def test_webhook_route_requires_signature_once_key_is_set(ed25519, monkeypatch):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    pub, sign = ed25519
    secret = "s" * 24
    save_user_config({"voip": {"enabled": True, "webhook_secret": secret}})
    seen = []

    async def handle(self, payload):
        seen.append(payload)
        return {"ok": True}

    monkeypatch.setattr(service_mod.VoipService, "handle_webhook", handle)
    c = TestClient(create_app())
    body = json.dumps({"data": {"event_type": "call.initiated", "payload": {}}}).encode()
    assert c.post(f"/telephony/webhook/{secret}", content=body).json() == {"ok": True}   # no key yet
    assert c.post("/telephony/webhook/wrong", content=body).status_code == 403
    save_user_config({"voip": {"telnyx_public_key": pub}})
    assert c.post(f"/telephony/webhook/{secret}", content=body).status_code == 403
    now = int(time.time())
    ok = c.post(f"/telephony/webhook/{secret}", content=body,
                headers={"telnyx-signature-ed25519": sign(body, now), "telnyx-timestamp": str(now)})
    assert ok.json() == {"ok": True} and len(seen) == 2
    forged = c.post(f"/telephony/webhook/{secret}", content=body,
                    headers={"telnyx-signature-ed25519": sign(b"other", now), "telnyx-timestamp": str(now)})
    assert forged.status_code == 403 and len(seen) == 2


# ── 5. media-socket setup failure still cleans up ────────────────────────────────
class _Socket:
    def __init__(self):
        self.closed = False

    async def accept(self):
        pass

    async def close(self, code=1000):
        self.closed = True


async def test_media_socket_cleans_up_when_setup_fails(monkeypatch):
    closed = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def aclose(self):
            closed.append("client")

    def boom(*a, **k):
        raise RuntimeError("no speech configured")

    monkeypatch.setattr(telnyx_trunk, "TelnyxClient", _Client)
    monkeypatch.setattr(service_mod, "build_session", boom)
    svc = service_mod.VoipService()
    svc.telnyx.pending["tok"] = {"call_control_id": "cc1", "from": "+1555", "to": "+1666"}
    ws = _Socket()
    await asyncio.wait_for(svc.run_media_socket(ws, "tok"), timeout=5)
    assert ws.closed and closed == ["client"] and not svc.active
