"""Where the CRM and Xplogent find each other: the address the CRM should use for Xplogent
(Docker https / LAN / this server only), and CRM addresses that can't work from a container."""

from __future__ import annotations

import pytest

from xplogent.core.config import load_config, save_user_config
from xplogent.voip import crm, readiness

_ENV = ("XPLOGENT_IN_DOCKER", "COMPOSE_FILE", "DOMAIN", "XPLOGENT_BIND", "XPLOGENT_PORT", "XPLOGENT_HOST_IP",
        "XPLOGENT_LAN_ADDRESS",
        "XPLOGENT_API_HOST", "XPLOGENT_API_PORT")


@pytest.fixture
def docker(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XPLOGENT_IN_DOCKER", "1")
    return monkeypatch


def test_https_mode_gives_the_domain(docker):
    docker.setenv("COMPOSE_FILE", "docker-compose.yml:compose.https.yml")
    docker.setenv("DOMAIN", "agent.example.com")
    out = crm.callback_address({})
    assert out["url"] == "https://agent.example.com" and not out["warning"]


def test_local_lan_mode_gives_the_lan_address(docker):
    docker.setenv("COMPOSE_FILE", "docker-compose.yml:compose.local.yml")
    docker.setenv("XPLOGENT_BIND", "0.0.0.0")
    assert crm.callback_address({})["url"] == "http://<this server's LAN IP>:8765"
    docker.setenv("XPLOGENT_HOST_IP", "192.168.1.30")
    docker.setenv("XPLOGENT_PORT", "9000")
    out = crm.callback_address({})
    assert out["url"] == "http://192.168.1.30:9000" and not out["warning"] and "firewall" in out["note"]
    docker.setenv("XPLOGENT_LAN_ADDRESS", "192.168.1.31")          # written by `xplogent-docker local --lan`
    assert crm.callback_address({})["url"] == "http://192.168.1.31:9000"


def test_this_server_only_warns_with_the_fix(docker):
    docker.setenv("COMPOSE_FILE", "docker-compose.yml:compose.local.yml")      # XPLOGENT_BIND unset = 127.0.0.1
    out = crm.callback_address({})
    assert out["url"] == "http://127.0.0.1:8765"
    assert "another machine" in out["warning"] and "xplogent-docker local --lan" in out["note"]


def test_outside_docker(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    assert crm.callback_address({"public_url": "https://agent.example.com/"})["url"] == "https://agent.example.com"
    assert "another machine" in crm.callback_address({})["warning"]
    monkeypatch.setenv("XPLOGENT_API_HOST", "0.0.0.0")
    assert not crm.callback_address({})["warning"]


@pytest.mark.parametrize("url, bad", [("http://127.0.0.1:8000", True), ("http://localhost:8000", True),
                                      ("http://192.168.1.20:8000", False),
                                      ("http://host.docker.internal:8000", False)])
def test_a_loopback_crm_address_is_explained_in_docker(docker, url, bad):
    assert bool(crm.loopback_problem(url)) is bad
    res = {"ok": False, "status": 0, "error": "ConnectError: [Errno 111] Connection refused"}
    why = crm.explain_failure(res, base_url=url)
    assert ("the container itself" in why["reason"]) is bad
    if bad:
        assert "LAN address" in why["fix"] and "host.docker.internal" in why["fix"]


def test_loopback_is_fine_outside_docker(monkeypatch):
    monkeypatch.delenv("XPLOGENT_IN_DOCKER", raising=False)
    assert crm.loopback_problem("http://127.0.0.1:8089") is None              # the mock CRM on this machine


def test_readiness_and_the_card_say_how_the_crm_reaches_us(docker, monkeypatch):
    docker.setenv("COMPOSE_FILE", "docker-compose.yml:compose.local.yml")
    save_user_config({"voip": {"crm": {"enabled": True, "base_url": "http://127.0.0.1:8000"}}})
    v = load_config().voip
    items = {i["id"]: i for i in readiness.checklist(v, "openai:gpt-4o-mini", env={})}
    assert not items["crm_address"]["ok"] and "container itself" in items["crm_address"]["detail"]
    assert not items["crm_callback"]["ok"] and "--lan" in items["crm_callback"]["detail"]
    assert not items["crm_callback"]["required"]
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app
    monkeypatch.delenv("XPLOGENT_AUTH_TOKEN", raising=False)
    info = TestClient(create_app()).get("/voip/agent").json()["crm"]
    assert info["callback"]["url"] == "http://127.0.0.1:8765" and "container itself" in info["address_problem"]
