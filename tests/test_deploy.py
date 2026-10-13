"""Server-deployment behaviour: packaged defaults, env auth token, behind-proxy safety."""

from __future__ import annotations

import pytest

from xplogent.core import config as cfgmod


def test_defaults_ship_inside_the_package():
    # Must live next to the code (not the repo root) so wheels / pip installs work.
    path = cfgmod._DEFAULT_CONFIG
    assert path.exists(), path
    assert path.parent.name == "config" and path.parent.parent.name == "xplogent"
    assert cfgmod.load_config().voip["provider"] == "telnyx"   # a section from the packaged file


def _client(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    return TestClient(create_app())


def test_env_auth_token_protects_the_api(monkeypatch, tmp_path):
    monkeypatch.setenv("XPLOGENT_AUTH_TOKEN", "s3cret-token")
    c = _client(monkeypatch, tmp_path)
    assert c.get("/health").json()["auth"] is True            # health stays public
    assert c.get("/config").status_code == 401
    assert c.get("/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/config", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200


def test_carrier_webhook_stays_reachable_with_auth_on(monkeypatch, tmp_path):
    monkeypatch.setenv("XPLOGENT_AUTH_TOKEN", "s3cret-token")
    c = _client(monkeypatch, tmp_path)
    # Not 401: the carrier can't send our token; the path secret guards it (403 when wrong).
    assert c.post("/telephony/webhook/nope", json={}).status_code == 403


def test_dashboard_dir_honours_env_override(monkeypatch, tmp_path):
    from xplogent.interfaces.api.server import _dashboard_dir

    web = tmp_path / "dist"
    web.mkdir()
    (web / "index.html").write_text("<html></html>")
    monkeypatch.setenv("XPLOGENT_WEB_DIR", str(web))
    assert _dashboard_dir() == web


@pytest.mark.parametrize("behind_proxy,host,expect_token", [
    ("1", "127.0.0.1", True),     # localhost bind behind Caddy/nginx must still get a token
    ("", "127.0.0.1", False),     # plain local dev: no token
    ("", "0.0.0.0", True),        # exposed bind: token (pre-existing behaviour)
])
def test_run_server_generates_token_when_exposed(monkeypatch, tmp_path, behind_proxy, host, expect_token):
    pytest.importorskip("uvicorn")
    import uvicorn

    from xplogent.interfaces.api import server

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.delenv("XPLOGENT_AUTH_TOKEN", raising=False)
    if behind_proxy:
        monkeypatch.setenv("XPLOGENT_BEHIND_PROXY", behind_proxy)
    else:
        monkeypatch.delenv("XPLOGENT_BEHIND_PROXY", raising=False)
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))

    server.run_server(host=host, port=18999)

    token = cfgmod.load_config().raw.get("server", {}).get("auth_token") or ""
    assert bool(token) is expect_token
    assert seen["proxy_headers"] is True
    assert seen["forwarded_allow_ips"] == "127.0.0.1"


def test_forwarded_allow_ips_is_configurable(monkeypatch, tmp_path):
    pytest.importorskip("uvicorn")
    import uvicorn

    from xplogent.interfaces.api import server

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setenv("XPLOGENT_FORWARDED_ALLOW_IPS", "172.18.0.0/16")
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    server.run_server(host="127.0.0.1", port=18999)
    assert seen["forwarded_allow_ips"] == "172.18.0.0/16"
