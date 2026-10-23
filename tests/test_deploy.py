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
    res = _get_sh(tmp_path, "--mode", "local", "--no-ollama", "--model", "openai:gpt-4o-mini")
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
    assert _get_sh(tmp_path, "--mode", "local", "--no-ollama").returncode == 0
    token = _env(tmp_path)["XPLOGENT_AUTH_TOKEN"]
    assert _get_sh(tmp_path).returncode == 0
    assert _env(tmp_path)["XPLOGENT_AUTH_TOKEN"] == token
    res = _get_sh(tmp_path, "--reconfigure", "--domain", "agent.example.com", "--ollama")
    assert res.returncode == 0, res.stderr
    env = _env(tmp_path)
    assert env["XPLOGENT_AUTH_TOKEN"] == token
    assert env["COMPOSE_FILE"] == "docker-compose.yml:compose.https.yml"
    assert env["DOMAIN"] == "agent.example.com"
    assert env["COMPOSE_PROFILES"] in ("ollama", "ollama-gpu")
    assert env["OLLAMA_HOST"] == "http://ollama:11434"


@_bash
def test_installer_takes_the_api_key_from_the_environment(tmp_path):
    res = _get_sh(tmp_path, "--mode", "local", "--no-ollama", "--model", "anthropic:claude-x",
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
    assert base["ollama"]["profiles"] == ["ollama"]
    assert base["ollama-gpu"]["networks"]["default"]["aliases"] == ["ollama"]
    https = yaml.safe_load((d / "compose.https.yml").read_text())["services"]
    assert set(https) == {"caddy"} and "80:80" in https["caddy"]["ports"]
    local = yaml.safe_load((d / "compose.local.yml").read_text())["services"]
    assert local["xplogent"]["ports"] == ["${XPLOGENT_BIND:-127.0.0.1}:${XPLOGENT_PORT:-8765}:8765"]


@_bash
def test_installer_flags_apply_to_an_existing_install(tmp_path):
    """Re-running with --no-ollama used to be ignored ("keeping its settings")."""
    assert _get_sh(tmp_path, "--mode", "local", "--ollama").returncode == 0
    before = _env(tmp_path)
    assert before["COMPOSE_PROFILES"].startswith("ollama")
    res = _get_sh(tmp_path, "--no-ollama", "--model", "openai:gpt-4o-mini")
    assert res.returncode == 0, res.stderr
    after = _env(tmp_path)
    assert after["COMPOSE_PROFILES"] == "" and after["OLLAMA_HOST"] == ""
    assert after["XPLOGENT_MODEL"] == "openai:gpt-4o-mini"
    assert after["XPLOGENT_AUTH_TOKEN"] == before["XPLOGENT_AUTH_TOKEN"]
    assert after["COMPOSE_FILE"] == before["COMPOSE_FILE"]          # untouched
    # Only --lan: everything else (model, profiles) stays as it was.
    assert _get_sh(tmp_path, "--lan").returncode == 0
    lan = _env(tmp_path)
    assert lan["XPLOGENT_BIND"] == "0.0.0.0"
    assert lan["XPLOGENT_MODEL"] == "openai:gpt-4o-mini" and lan["COMPOSE_PROFILES"] == ""
    # No flags at all: nothing changes.
    assert _get_sh(tmp_path).returncode == 0
    assert _env(tmp_path) == lan


@_bash
def test_installer_backup_model_runs_local_ollama_without_redirecting_main(tmp_path):
    res = _get_sh(tmp_path, "--mode", "local", "--no-ollama", "--backup-model")
    assert res.returncode == 0, res.stderr
    env = _env(tmp_path)
    assert env["COMPOSE_PROFILES"] in ("ollama", "ollama-gpu")
    assert env.get("OLLAMA_HOST", "") == ""                # main model stays where it was


def test_sip_compose_file_publishes_udp_and_passes_settings():
    yaml = pytest.importorskip("yaml")
    sip = yaml.safe_load((REPO / "deploy" / "docker" / "compose.sip.yml").read_text())["services"]["xplogent"]
    assert all(p.endswith("/udp") for p in sip["ports"])
    assert {"XPLOGENT_SIP_PORT", "XPLOGENT_RTP_START", "XPLOGENT_RTP_END", "XPLOGENT_HOST_IP"} <= set(sip["environment"])


@_bash
def test_installer_keeps_sip_ports_when_switching_mode(tmp_path):
    assert _get_sh(tmp_path, "--mode", "local", "--no-ollama").returncode == 0
    assert (tmp_path / "x" / "deploy" / "docker" / "compose.sip.yml").exists()
    env_file = tmp_path / "x" / "deploy" / "docker" / ".env"
    env_file.write_text(env_file.read_text() + "XPLOGENT_SIP=1\n")
    res = _get_sh(tmp_path, "--reconfigure", "--domain", "agent.example.com", "--no-ollama")
    assert res.returncode == 0, res.stderr
    assert _env(tmp_path)["COMPOSE_FILE"] == "docker-compose.yml:compose.https.yml:compose.sip.yml"


def test_helper_has_sip_command():
    helper = (REPO / "deploy" / "docker" / "xplogent-docker").read_text()
    assert "sip on [--ip ADDR] | off" in helper and "compose.sip.yml" in helper
