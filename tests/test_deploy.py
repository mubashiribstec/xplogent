"""Server-deployment behaviour: packaged defaults, env auth token, behind-proxy safety."""

from __future__ import annotations

import pytest

from xplogent.core import config as cfgmod


def test_defaults_ship_inside_the_package():
    # Must live next to the code (not the repo root) so wheels / pip installs work.
    path = cfgmod._DEFAULT_CONFIG
    assert path.exists(), path
    assert path.parent.name == "config" and path.parent.parent.name == "xplogent"
    assert cfgmod.load_config().voip["telephony"] == "telnyx"   # a section from the packaged file


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


# ── Docker one-command install (get.sh) + compose layout ───────────────────────
import os  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
_bash = pytest.mark.skipif(platform.system() != "Linux" or not shutil.which("bash"),
                           reason="the Docker installer targets Linux")


def _get_sh(tmp_path, *args, env=None):
    run_env = {**os.environ, "XPL_SRC": str(REPO), "XPL_BIN_DIR": str(tmp_path / "bin"), **(env or {})}
    return subprocess.run(["bash", str(REPO / "get.sh"), "--dry-run", "--yes",
                           "--dir", str(tmp_path / "x"), *args],
                          capture_output=True, text=True, env=run_env, stdin=subprocess.DEVNULL)


def _env(tmp_path) -> dict[str, str]:
    text = (tmp_path / "x" / "deploy" / "docker" / ".env").read_text()
    return dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#"))


@_bash
def test_installer_local_mode_writes_private_env_with_token(tmp_path):
    res = _get_sh(tmp_path, "--mode", "local", "--model", "openai:gpt-4o-mini")
    assert res.returncode == 0, res.stderr
    env = _env(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{64}", env["XPLOGENT_AUTH_TOKEN"])
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose.local.yml"
    assert env["XPLOGENT_BIND"] == "127.0.0.1"           # not exposed unless --lan
    assert env["COMPOSE_PROFILES"] == ""
    mode = stat.S_IMODE((tmp_path / "x" / "deploy" / "docker" / ".env").stat().st_mode)
    assert mode == 0o600
    helper = (tmp_path / "bin" / "xplogent-docker").read_text()
    assert f'DEFAULT_DIR="{tmp_path / "x"}"' in helper
    assert env["XPLOGENT_AUTH_TOKEN"] in res.stdout       # printed once for the user


@_bash
def test_installer_rerun_keeps_token_and_reconfigure_switches_to_https(tmp_path):
    assert _get_sh(tmp_path, "--mode", "local").returncode == 0
    token = _env(tmp_path)["XPLOGENT_AUTH_TOKEN"]
    assert _get_sh(tmp_path).returncode == 0
    assert _env(tmp_path)["XPLOGENT_AUTH_TOKEN"] == token
    res = _get_sh(tmp_path, "--reconfigure", "--domain", "agent.example.com")
    assert res.returncode == 0, res.stderr
    env = _env(tmp_path)
    assert env["XPLOGENT_AUTH_TOKEN"] == token
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose.https.yml"
    assert env["DOMAIN"] == "agent.example.com"


@_bash
def test_installer_takes_the_api_key_from_the_environment(tmp_path):
    res = _get_sh(tmp_path, "--mode", "local", "--model", "anthropic:claude-x",
                  env={"ANTHROPIC_API_KEY": "sk-ant-test"})
    assert res.returncode == 0, res.stderr
    assert _env(tmp_path)["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert "sk-ant-test" not in res.stdout                 # never echoed


@_bash
@pytest.mark.parametrize("args, message", [
    (["--domain", "bad;rm -rf /"], "not a valid hostname"),
    (["--mode", "https"], "needs a domain"),
    (["--mode", "ftp"], "must be https or local"),
])
def test_installer_rejects_bad_input(tmp_path, args, message):
    res = _get_sh(tmp_path, *args)
    assert res.returncode != 0 and message in res.stderr


@_bash
def test_install_scripts_parse():
    for script in ("get.sh", "deploy/docker/xplogent-docker", "deploy/install-server.sh", "install.sh"):
        subprocess.run(["bash", "-n", str(REPO / script)], check=True)


def test_compose_layout():
    yaml = pytest.importorskip("yaml")
    d = REPO / "deploy" / "docker"
    base = yaml.safe_load((d / "docker-compose.yml").read_text())["services"]
    assert base["xplogent"]["image"].startswith("${XPLOGENT_IMAGE:-ghcr.io/mubashiribstec/xplogent")
    assert "ports" not in base["xplogent"]                 # only the mode files publish anything
    assert not {"ollama", "ollama-gpu"} & set(base)          # no language model runs on the server
    https = yaml.safe_load((d / "compose.https.yml").read_text())["services"]
    assert set(https) == {"caddy"} and "80:80" in https["caddy"]["ports"]
    local = yaml.safe_load((d / "compose.local.yml").read_text())["services"]
    assert local["xplogent"]["ports"] == ["${XPLOGENT_BIND:-127.0.0.1}:${XPLOGENT_PORT:-8765}:8765"]
    # A CRM (or Ollama) on this same host, outside Docker: 127.0.0.1 in the container is the container.
    assert base["xplogent"]["extra_hosts"] == ["host.docker.internal:host-gateway"]


def test_env_example_documents_the_crm_link():
    env = (REPO / "deploy" / "docker" / ".env.example").read_text(encoding="utf-8")
    assert "# XPLOGENT_CRM_API_TOKEN=" in env                  # commented: the dashboard is the easy way
    assert "XPLOGENT_BASE_URL=http://<this server's LAN IP>:8765" in env
    assert "xplogent-docker local --lan" in env and "host.docker.internal" in env


@_bash
def test_installer_flags_apply_to_an_existing_install(tmp_path):
    assert _get_sh(tmp_path, "--mode", "local", "--model", "openai:gpt-4o-mini").returncode == 0
    before = _env(tmp_path)
    res = _get_sh(tmp_path, "--model", "gemini:gemini-3.6-flash")
    assert res.returncode == 0, res.stderr
    after = _env(tmp_path)
    assert after["XPLOGENT_MODEL"] == "gemini:gemini-3.6-flash"
    assert after["XPLOGENT_AUTH_TOKEN"] == before["XPLOGENT_AUTH_TOKEN"]
    assert after["COMPOSE_FILE"] == before["COMPOSE_FILE"]          # untouched
    # Only --lan: everything else (model, profiles) stays as it was.
    assert _get_sh(tmp_path, "--lan").returncode == 0
    lan = _env(tmp_path)
    assert lan["XPLOGENT_BIND"] == "0.0.0.0"
    assert lan["XPLOGENT_MODEL"] == "gemini:gemini-3.6-flash" and lan["COMPOSE_PROFILES"] == ""
    # The LAN address is recorded for the dashboard (the CRM's XPLOGENT_BASE_URL), when known.
    if "XPLOGENT_LAN_ADDRESS" in lan:
        assert re.fullmatch(r"[0-9a-fA-F:.]+", lan["XPLOGENT_LAN_ADDRESS"])
    # No flags at all: nothing changes.
    assert _get_sh(tmp_path).returncode == 0
    assert _env(tmp_path) == lan


@_bash
def test_installer_tidies_up_an_old_local_ollama(tmp_path):
    """Ollama is no longer bundled: a re-run (even without options) removes what an older
    install set up, and the old flags explain where to go instead."""
    assert _get_sh(tmp_path, "--mode", "local", "--model", "ollama:llama3.1").returncode == 0
    env_file = tmp_path / "x" / "deploy" / "docker" / ".env"
    text = env_file.read_text().replace("COMPOSE_PROFILES=\n", "COMPOSE_PROFILES=ollama,speech\n")
    env_file.write_text(text.replace("OLLAMA_HOST=\n", "OLLAMA_HOST=http://ollama:11434\n"))
    res = _get_sh(tmp_path)
    assert res.returncode == 0, res.stderr
    env = _env(tmp_path)
    assert env["COMPOSE_PROFILES"] == "speech" and env["OLLAMA_HOST"] == ""
    assert "Settings -> Models" in res.stderr                       # the default model was local
    for flag in ("--ollama", "--backup-model"):
        bad = _get_sh(tmp_path, flag)
        assert bad.returncode == 2 and "no longer part of Xplogent" in bad.stderr
    assert _get_sh(tmp_path, "--no-ollama").returncode == 0           # old scripts keep working


def test_helper_tidies_up_and_refuses_the_old_ollama_commands():
    helper = (REPO / "deploy" / "docker" / "xplogent-docker").read_text()
    assert "drop_bundled_ollama" in helper and "_ollama-models" in helper
    assert "ollama|backup-model|pull-model)" in helper and "no longer part of Xplogent" in helper


def test_sip_compose_file_publishes_udp_and_passes_settings():
    yaml = pytest.importorskip("yaml")
    sip = yaml.safe_load((REPO / "deploy" / "docker" / "compose.sip.yml").read_text())["services"]["xplogent"]
    assert all(p.endswith("/udp") for p in sip["ports"])
    assert {"XPLOGENT_SIP_PORT", "XPLOGENT_RTP_START", "XPLOGENT_RTP_END", "XPLOGENT_HOST_IP"} <= set(sip["environment"])
    # Both SIP lines (trunk + PBX extension) are reachable, on ports that don't overlap.
    assert {"XPLOGENT_SIP_EXT_PORT", "XPLOGENT_RTP_EXT_START", "XPLOGENT_RTP_EXT_END"} <= set(sip["environment"])
    published = " ".join(sip["ports"])
    assert "5060" in published and "5062" in published and "10200" in published and "10300" in published


@_bash
def test_installer_keeps_sip_ports_when_switching_mode(tmp_path):
    assert _get_sh(tmp_path, "--mode", "local").returncode == 0
    assert (tmp_path / "x" / "deploy" / "docker" / "compose.sip.yml").exists()
    env_file = tmp_path / "x" / "deploy" / "docker" / ".env"
    env_file.write_text(env_file.read_text() + "XPLOGENT_SIP=1\n")
    res = _get_sh(tmp_path, "--reconfigure", "--domain", "agent.example.com")
    assert res.returncode == 0, res.stderr
    assert _env(tmp_path)["COMPOSE_FILE"] == "docker-compose.yml:compose.https.yml:compose.sip.yml"


def test_crm_link_compose_file_joins_the_shared_network():
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load((REPO / "deploy" / "docker" / "compose.crm.yml").read_text())
    nets = doc["services"]["xplogent"]["networks"]
    assert "default" in nets and nets["crm"]["aliases"] == ["xplogent"]       # keeps its own network too
    assert doc["networks"]["crm"]["external"] is True
    assert doc["networks"]["crm"]["name"] == "${XPLOGENT_CRM_NETWORK:-xplogent-crm}"
    assert "ports" not in doc["services"]["xplogent"]                         # nothing published


def test_helper_has_crm_link_and_keeps_it_across_mode_switches():
    helper = (REPO / "deploy" / "docker" / "xplogent-docker").read_text()
    assert "crm link | unlink | status" in helper and "docker network create" in helper
    assert "compose.crm.yml" in helper.split("DEPLOY_FILES=")[1].split('"')[1]     # refreshed by update
    get_sh = (REPO / "get.sh").read_text()
    assert "deploy/docker/compose.crm.yml" in get_sh and "XPLOGENT_CRM_LINK" in get_sh


@_bash
def test_installer_keeps_the_crm_link_when_switching_mode(tmp_path):
    assert _get_sh(tmp_path, "--mode", "local").returncode == 0
    assert (tmp_path / "x" / "deploy" / "docker" / "compose.crm.yml").exists()
    env_file = tmp_path / "x" / "deploy" / "docker" / ".env"
    env_file.write_text(env_file.read_text() + "XPLOGENT_SIP=1\nXPLOGENT_CRM_LINK=1\n")
    res = _get_sh(tmp_path, "--reconfigure", "--domain", "agent.example.com")
    assert res.returncode == 0, res.stderr
    assert _env(tmp_path)["COMPOSE_FILE"] == "docker-compose.yml:compose.https.yml:compose.sip.yml:compose.crm.yml"


def test_helper_has_sip_command():
    helper = (REPO / "deploy" / "docker" / "xplogent-docker").read_text()
    assert "sip on [--ip ADDR] | off" in helper and "compose.sip.yml" in helper


# ── containers without DNS / internet (23 Nov) ──────────────────────────────────────────
def test_dns_compose_file_sets_the_containers_name_servers():
    import yaml
    doc = yaml.safe_load((REPO / "deploy" / "docker" / "compose.dns.yml").read_text())
    assert doc["services"]["xplogent"]["dns"][0].startswith("${XPLOGENT_DNS_1")


def test_helper_has_dns_and_doctor_and_keeps_the_dns_file():
    helper = (REPO / "deploy" / "docker" / "xplogent-docker").read_text()
    assert "dns IP [IP2] | auto | off | status" in helper and "cmd_doctor" in helper
    assert "flush" in helper and "ip_forward" in helper and "masquerade" in helper
    assert 'files="$files:compose.dns.yml"' in helper
    assert "compose.dns.yml" in helper.split("DEPLOY_FILES=")[1].split('"')[1]     # refreshed by update
    get_sh = (REPO / "get.sh").read_text()
    assert "deploy/docker/compose.dns.yml" in get_sh and "XPLOGENT_DNS_1" in get_sh


@_bash
def test_installer_keeps_the_dns_setting_when_switching_mode(tmp_path):
    assert _get_sh(tmp_path, "--mode", "local").returncode == 0
    assert (tmp_path / "x" / "deploy" / "docker" / "compose.dns.yml").exists()
    env_file = tmp_path / "x" / "deploy" / "docker" / ".env"
    env_file.write_text(env_file.read_text() + "XPLOGENT_DNS_1=192.168.110.1\n")
    res = _get_sh(tmp_path, "--reconfigure", "--domain", "agent.example.com")
    assert res.returncode == 0, res.stderr
    assert _env(tmp_path)["COMPOSE_FILE"] == "docker-compose.yml:compose.https.yml:compose.dns.yml"


@_bash
def test_doctor_names_the_firewall_that_wiped_dockers_rules(tmp_path):
    """Debian 13: nftables.conf starts with 'flush ruleset' and Docker's masquerade is gone."""
    import os
    import stat
    compose = tmp_path / "inst" / "deploy" / "docker"
    compose.mkdir(parents=True)
    (compose / "docker-compose.yml").write_text("services: {}\n")
    (compose / ".env").write_text("COMPOSE_FILE=docker-compose.yml:compose.local.yml\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    stubs = {
        "docker": "exit 0",                                   # `compose ps -q` → empty: not running
        "nft": "echo 'table inet filter { chain forward { policy drop; } }'",
        "systemctl": '[ "$1" = is-enabled ] && exit 0; exit 1',
        "getent": "exit 0", "curl": "exit 0", "resolvectl": "echo 'Global: 192.168.110.1'",
    }
    for name, body in stubs.items():
        f = bin_ / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    nft_conf = tmp_path / "nftables.conf"
    nft_conf.write_text("#!/usr/sbin/nft -f\nflush ruleset\ntable inet filter {}\n")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "XPLOGENT_DOCKER_DIR": str(tmp_path / "inst"),
           "XPLOGENT_NFT_CONF": str(nft_conf), "XPLOGENT_DAEMON_JSON": str(tmp_path / "none.json")}
    res = subprocess.run(["bash", str(REPO / "deploy" / "docker" / "xplogent-docker"), "doctor"],
                         capture_output=True, text=True, env=env)
    out = res.stdout + res.stderr
    assert res.returncode != 0
    assert "flush ruleset" in out and "systemctl disable --now nftables" in out
    assert "masquerade" in out.lower() and "systemctl restart docker" in out
    assert "isn't running" in out
