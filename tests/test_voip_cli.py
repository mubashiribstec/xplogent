"""`xplogent voip status|calls` and `xplogent dial` against the real API."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store


@pytest.fixture
def cli(monkeypatch):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app
    from xplogent.interfaces.cli import voip_cmds
    from xplogent.interfaces.cli.app import app

    client = TestClient(create_app())
    monkeypatch.setattr(voip_cmds.httpx, "Client", lambda **kw: client)
    monkeypatch.setenv("XPLOGENT_URL", "http://testserver")
    return app


def test_status_and_calls(cli):
    with Store(load_config().db_path) as s:
        cid = s.start_call("inbound", "+15550101111", "+1555", test=True, transport="sip")
        s.end_call(cid, outcome="resolved")
    r = CliRunner().invoke(cli, ["voip", "status"])
    assert r.exit_code == 0, r.output
    assert "Phone agent: off" in r.output and "Live calls: 0" in r.output and "✗" in r.output
    r = CliRunner().invoke(cli, ["voip", "calls", "-n", "5"])
    assert r.exit_code == 0 and "+15550101111" in r.output and "resolved" in r.output and "(test)" in r.output


def test_dial_reports_refusals(cli, monkeypatch):
    from xplogent.voip.service import get_service

    r = CliRunner().invoke(cli, ["dial"])
    assert r.exit_code == 2
    r = CliRunner().invoke(cli, ["dial", "+15550101111"])
    assert r.exit_code == 1 and "switched off" in r.output
    calls = []

    async def fake_dial(number, **kw):
        calls.append((number, kw))
        return {"ok": True, "via": kw["via"]}

    monkeypatch.setattr(get_service(), "dial", fake_dial)
    save_user_config({"voip": {"telephony": "sip"}})
    r = CliRunner().invoke(cli, ["dial", "101", "--test"])
    assert r.exit_code == 0 and "Calling 101 via sip (test call)" in r.output
    assert calls[0][0] == "101" and calls[0][1]["test"] is True and calls[0][1]["via"] == "sip"
