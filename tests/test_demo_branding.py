"""Demo mode (badged, never counted or sent, one-click clear) and white-label branding."""

from __future__ import annotations

import pytest

from xplogent.core.config import load_config
from xplogent.memory.store import Store

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    return TestClient(create_app())


def test_demo_calls_are_badged_uncounted_unsendable_and_clearable(client):
    with Store(load_config().db_path) as store:
        real = store.start_call("inbound", "+15550001111", "2000", transport="sip")
    assert client.post("/voip/demo", json={"on": True}).json()["demo_calls"] == 4
    calls = client.get("/voip/calls").json()["calls"]
    demo = [c for c in calls if c["demo"]]
    assert len(demo) == 4 and all(c["status"] == "ended" and c["outcome"] for c in demo)
    assert client.get("/voip/status").json()["calls"] == 1                 # demo calls aren't counted
    detail = client.get(f"/voip/calls/{demo[0]['id']}").json()["call"]
    assert detail["turns"] and client.get(f"/voip/calls/{demo[0]['id']}/debug").json()["debug"]["turns"]
    out = client.post(f"/voip/calls/{demo[0]['id']}/crm/resend").json()
    assert not out["ok"]                                                   # never reaches the CRM
    assert client.post("/voip/demo", json={"on": False}).json()["removed"] == 4
    assert [c["id"] for c in client.get("/voip/calls").json()["calls"]] == [real]
    with Store(load_config().db_path) as store:
        assert store.get_call_debug(demo[0]["id"]) is None


def test_logo_upload_checks_the_bytes_and_refuses_svg(client):
    ok = client.post("/branding/logo", files={"file": ("logo.png", PNG, "image/png")}).json()
    assert ok["ok"] and ok["logo"]
    got = client.get("/branding/logo")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert not client.post("/branding/logo", files={"file": ("x.svg", svg, "image/svg+xml")}).json()["ok"]
    fake = client.post("/branding/logo", files={"file": ("x.png", b"<html>", "image/png")}).json()
    assert not fake["ok"] and "PNG, JPEG or WebP" in fake["error"]
    big = client.post("/branding/logo", files={"file": ("big.png", PNG + b"\x00" * 600_000, "image/png")}).json()
    assert not big["ok"] and "512 KB" in big["error"]
    assert client.delete("/branding/logo").json()["ok"] and client.get("/branding/logo").status_code == 404


def test_accent_company_and_tour_state(client):
    assert not client.post("/branding", json={"accent": "red; background:url(x)"}).json()["ok"]
    out = client.post("/branding", json={"accent": "#1A73E8", "company": "  Kingsbury   Personnel "}).json()
    assert out["ok"] and out["accent"] == "#1A73E8" and out["company"] == "Kingsbury Personnel"
    assert client.get("/branding").json()["company"] == "Kingsbury Personnel"
    assert client.post("/ui/tour", json={"done": True}).json()["ok"]
    assert client.get("/config/full").json()["ui"]["tour_done"] is True


def test_branding_is_public_but_changing_it_is_not(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setenv("XPLOGENT_AUTH_TOKEN", "t" * 32)
    c = TestClient(create_app())
    assert c.get("/branding").status_code == 200                           # the login page needs it
    assert c.get("/branding/logo").status_code == 404                      # public, just no logo yet
    assert c.post("/branding", json={"accent": "#000000"}).status_code == 401
    assert c.post("/branding/logo", files={"file": ("l.png", PNG, "image/png")}).status_code == 401
    assert c.post("/voip/demo", json={"on": True}).status_code == 401
