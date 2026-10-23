"""Second SIP line (PBX extension login): settings, registry, routing, routes.

The extension line reuses the trunk machinery but is independent of it: the
trunk stays reserved for the carrier (Telnyx SIP) while the agent also holds
a FreePBX extension for internal calls and queue overflow. Live registration
needs UDP, so the registry is exercised with a fake line class here.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS
from xplogent.core.config import load_config
from xplogent.memory.store import Store
from xplogent.voip.service import VoipService, build_session
from xplogent.voip.telephony import sip
from xplogent.voip.telephony.sip import SipSettings


# ── settings parsing ─────────────────────────────────────────────────────────
def test_extension_settings_defaults_and_flag(monkeypatch):
    for var in ("XPLOGENT_SIP_PORT", "XPLOGENT_RTP_START", "XPLOGENT_RTP_END",
                "XPLOGENT_SIP_EXT_PORT", "XPLOGENT_RTP_EXT_START", "XPLOGENT_RTP_EXT_END"):
        monkeypatch.delenv(var, raising=False)
    v = {"sip_extension": {"server": "pbx.local", "username": "2000"}}
    s = SipSettings.extension_from_config(v)
    assert (s.local_port, s.rtp_start, s.rtp_end) == (5062, 10200, 10300)
    assert s.enabled is False and s.register is True
    on = SipSettings.extension_from_config(
        {"sip_extension": {"enabled": True, "server": "pbx.local", "username": "2000",
                           "local_port": 5090, "rtp_start": 20000, "rtp_end": 20050}})
    assert on.enabled is True and (on.local_port, on.rtp_start, on.rtp_end) == (5090, 20000, 20050)
    # Inert by default: a missing block parses to an unconfigured, disabled line.
    empty = SipSettings.extension_from_config({})
    assert empty.enabled is False and empty.server == "" and not sip.wants_extension({})


def test_extension_password_comes_from_its_own_secret(monkeypatch):
    monkeypatch.setenv("SIP_PASSWORD", "trunkpw")
    monkeypatch.setenv("SIP_EXTENSION_PASSWORD", "extpw")
    v = {"sip": {"server": "sip.telnyx.com", "username": "carrier"},
         "sip_extension": {"server": "pbx.local", "username": "2000"}}
    assert SipSettings.from_config(v).password == "trunkpw"
    assert SipSettings.extension_from_config(v).password == "extpw"


def test_trunk_settings_unchanged():
    s = SipSettings.from_config({"sip": {"server": "pbx.local", "username": "2000"}})
    assert (s.local_port, s.rtp_start, s.rtp_end) == (5060, 10000, 10100)
    assert s.enabled is False


# ── line registry: the two lines live and die independently ──────────────────
class FakeLine:
    """Stands in for SipTrunk where UDP registration cannot run (sandbox)."""

    def __init__(self, settings, *, service=None, session_kw=None, cfg=None, line="trunk"):
        self.s = settings
        self.line = line
        self.service = service
        self.registered = False
        self.reg_error = ""
        self.started = False
        self.stops = 0

    async def start(self):
        self.started = True

    async def stop(self):
        self.stops += 1
        self.started = False

    async def dial(self, number, meta=None):
        return {"ok": True, "via": self.line, "number": number}

    def status(self):
        return {"line": self.line, "running": self.started, "registered": self.registered,
                "error": self.reg_error, "server": f"{self.s.server}:{self.s.port}",
                "username": self.s.username, "local_port": self.s.local_port,
                "contact_ip": "", "rtp": f"{self.s.rtp_start}-{self.s.rtp_end}",
                "calls": 0, "last_error": "", "registered_at": 0.0, "expires": 0}


@pytest.fixture()
def fake_lines(monkeypatch):
    monkeypatch.setattr(sip, "SipTrunk", FakeLine)
    monkeypatch.setattr(sip, "_LINES", {"trunk": None, "extension": None})
    from xplogent.voip.service import get_service

    service = get_service()
    monkeypatch.setattr(service, "sip_dialer", None)
    monkeypatch.setattr(service, "sip_extension_dialer", None)
    return service


def _both_cfg():
    return SimpleNamespace(voip={
        "telephony": "both",
        "sip": {"server": "sip.telnyx.com", "username": "carrier"},
        "sip_extension": {"enabled": True, "server": "pbx.local", "username": "2000"}})


async def test_registry_holds_two_lines_independently(fake_lines):
    service = fake_lines
    trunk = await sip.apply_settings(_both_cfg())
    ext = await sip.apply_settings(_both_cfg(), line="extension")
    assert trunk is not None and ext is not None and trunk is not ext
    assert sip.current_trunk() is trunk and sip.current_line("extension") is ext
    assert trunk.line == "trunk" and ext.line == "extension"
    assert service.sip_dialer is not None and service.sip_extension_dialer is not None
    # Registration state is per-line.
    ext.registered = True
    assert sip.current_line("extension").status()["registered"] is True
    assert sip.current_trunk().status()["registered"] is False
    # Rebuilding the extension line leaves the trunk running untouched.
    ext2 = await sip.apply_settings(_both_cfg(), line="extension")
    assert ext.stops == 1 and trunk.stops == 0 and ext2 is not ext
    assert sip.current_trunk() is trunk
    # Switching the extension line off stops only that line.
    off = SimpleNamespace(voip={**_both_cfg().voip,
                                "sip_extension": {"enabled": False, "server": "pbx.local",
                                                  "username": "2000"}})
    assert await sip.apply_settings(off, line="extension") is None
    assert ext2.stops == 1 and trunk.stops == 0 and service.sip_extension_dialer is None
    assert sip.current_trunk() is trunk and service.sip_dialer is not None


async def test_extension_line_needs_no_telephony_mode(fake_lines):
    # telephony stays "telnyx" (Voice API): the extension line still runs.
    cfg = SimpleNamespace(voip={"telephony": "telnyx",
                                "sip_extension": {"enabled": True, "server": "pbx.local",
                                                  "username": "2000"}})
    assert sip.wants_extension(cfg.voip) and not sip.wants_sip(cfg.voip)
    ext = await sip.apply_settings(cfg, line="extension")
    assert ext is not None and sip.current_trunk() is None


# ── dial routing ──────────────────────────────────────────────────────────────
async def test_dial_via_extension_uses_extension_dialer():
    svc = VoipService()
    dialed = {"trunk": [], "extension": []}

    async def trunk_dialer(number, meta):
        dialed["trunk"].append((number, meta))
        return {"ok": True}

    async def extension_dialer(number, meta):
        dialed["extension"].append((number, meta))
        return {"ok": True}

    svc.sip_dialer = trunk_dialer
    svc.sip_extension_dialer = extension_dialer
    assert (await svc.dial("101", via="extension", test=True))["ok"]
    assert (await svc.dial("*43", via="extension", test=True))["ok"]
    assert (await svc.dial("100", via="sip", test=True))["ok"]
    assert [d[0] for d in dialed["extension"]] == ["101", "*43"]
    assert [d[0] for d in dialed["trunk"]] == ["100"]
    meta = dialed["extension"][0][1]
    assert meta["transport"] == "sip" and meta["line"] == "extension"
    assert dialed["trunk"][0][1]["line"] == "trunk"
    assert not (await svc.dial("101", via="telnyx", test=True))["ok"]   # short numbers stay SIP-only


async def test_dial_via_extension_without_line_errors():
    svc = VoipService()
    res = await svc.dial("101", via="extension", test=True)
    assert not res["ok"] and "extension" in res["error"]


# ── routes ────────────────────────────────────────────────────────────────────
async def test_extension_routes_save_status_and_validation(monkeypatch):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    applied = []

    async def fake_apply(cfg=None, line="trunk"):
        applied.append(line)
        return None

    monkeypatch.setattr(sip, "apply_settings", fake_apply)
    c = TestClient(create_app())
    bad = c.post("/voip/sip/extension/settings", json={"server": "sip:pbx.local:5060"}).json()
    assert not bad["ok"] and "host name" in bad["error"]
    assert not c.post("/voip/sip/extension/settings", json={"rtp_start": 30000, "rtp_end": 20000}).json()["ok"]
    res = c.post("/voip/sip/extension/settings",
                 json={"enabled": True, "server": "pbx.local", "username": "2000",
                       "password": "extpw", "allow_from": "10.0.0.9"}).json()
    assert res["ok"] and applied == ["extension"]       # only the extension line is rebuilt
    st = res["status"]
    assert st["enabled"] and st["configured"] and st["password_set"]
    v = load_config().voip
    assert v["sip_extension"]["server"] == "pbx.local" and v["sip_extension"]["enabled"] is True
    assert v["sip_extension"]["allow_from"] == ["10.0.0.9"]
    assert "sip" not in v or v["sip"].get("server") in (None, "")   # trunk config untouched
    assert os.environ["SIP_EXTENSION_PASSWORD"] == "extpw"
    assert "extpw" not in str(c.get("/config/full").json())
    full = c.get("/voip/sip/status").json()
    assert "telephony" in full and "configured" in full            # trunk fields stay top-level
    assert full["extension"]["enabled"] and full["extension"]["configured"]
    items = {i["id"]: i for i in c.get("/voip/readiness").json()["items"]}
    assert items["sip_extension_account"]["ok"] and not items["sip_extension_registered"]["ok"]


async def test_dial_route_accepts_extension_via(monkeypatch):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app
    from xplogent.voip.service import get_service

    async def extension_dialer(number, meta):
        return {"ok": True, "via": "extension", "number": number}

    monkeypatch.setattr(get_service(), "sip_extension_dialer", extension_dialer)
    c = TestClient(create_app())
    res = c.post("/voip/dial", json={"number": "101", "via": "extension", "test": True}).json()
    assert res["ok"] and res["via"] == "extension"
    bad = c.post("/voip/dial", json={"number": "101", "via": "smoke-signal", "test": True}).json()
    assert not bad["ok"] and "extension" in bad["error"]


# ── call records + brain context carry the line ───────────────────────────────
def _fake_session_parts():
    async def send(_b):
        return None

    return dict(send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                provider=ScriptedProvider([]), retrieve=None)


def test_call_record_and_context_tagged_with_line(tmp_path):
    cfg = load_config()
    store = Store(cfg.db_path)
    info = {"direction": "inbound", "from": "101", "to": "2000",
            "call_control_id": "ext-call-1", "transport": "sip", "line": "extension"}
    session, call_id, brain = build_session(cfg, store, info, **_fake_session_parts())
    call = store.get_call(call_id)
    assert call["transport"] == "sip" and call["line"] == "extension"
    assert "extension line" in brain.persona
    info2 = {"direction": "inbound", "from": "+15550102000", "to": "2000",
             "call_control_id": "trunk-call-1", "transport": "sip", "line": "trunk"}
    _s2, call_id2, brain2 = build_session(cfg, store, info2, **_fake_session_parts())
    assert store.get_call(call_id2)["line"] == "trunk"
    assert "extension line" not in brain2.persona
    store.close()
