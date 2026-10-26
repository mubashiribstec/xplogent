"""Named Ollama servers: each with its own bearer token, usable for any task and as backup."""

from __future__ import annotations

import json

import pytest

from xplogent.core.config import allowed_env_key, load_config, save_env, save_user_config, secret_status
from xplogent.providers import ollama as oll


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    for k in ("OLLAMA_API_KEY", "OLLAMA_API_KEY_GPU", "OLLAMA_API_KEY_LAB_2", "OLLAMA_HOST"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


@pytest.fixture
def client(home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    return TestClient(create_app())


def test_spec_and_secret_names():
    assert oll.split_server("qwen2.5:14b@gpu") == ("qwen2.5:14b", "gpu")
    assert oll.split_server("llama3.1") == ("llama3.1", "")
    assert oll.server_token_env("lab-2") == "OLLAMA_API_KEY_LAB_2"
    assert allowed_env_key("OLLAMA_API_KEY_GPU") and not allowed_env_key("OLLAMA_API_KEY_gpu")
    assert not allowed_env_key("LD_PRELOAD")


def test_each_server_gets_only_its_own_token(home, monkeypatch):
    save_user_config({"ollama": {"host": "http://cpu.local:11434",
                                 "servers": [{"name": "gpu", "host": "https://gpu.example.com/"}]}})
    monkeypatch.setenv("OLLAMA_API_KEY", "default-token")
    monkeypatch.setenv("OLLAMA_API_KEY_GPU", "gpu-token")
    gpu = oll.OllamaProvider("qwen2.5:14b@gpu")
    assert gpu.model == "qwen2.5:14b" and gpu.server == "gpu"
    assert str(gpu._client.base_url).rstrip("/") == "https://gpu.example.com"
    assert gpu._client.headers["Authorization"] == "Bearer gpu-token"
    default = oll.OllamaProvider("llama3.1")
    assert str(default._client.base_url).startswith("http://cpu.local")
    assert default._client.headers["Authorization"] == "Bearer default-token"
    with pytest.raises(ValueError, match="no Ollama server named 'nope'"):
        oll.OllamaProvider("x@nope")


def test_named_server_works_through_the_registry_and_as_backup(home, monkeypatch):
    from xplogent.providers.registry import backup_spec, build_provider

    save_user_config({"ollama": {"servers": [{"name": "gpu", "host": "https://gpu.example.com"}]},
                      "fallback": {"enabled": True, "model": "qwen2.5:7b", "server": "gpu"}})
    assert backup_spec() == "ollama:qwen2.5:7b@gpu"
    prov = build_provider("openrouter:google/gemma-4-31b-it:free")
    assert prov.backup_spec == "ollama:qwen2.5:7b@gpu" and prov.primary.model == "google/gemma-4-31b-it:free"
    save_user_config({"fallback": {"server": ""}})
    assert backup_spec() == "ollama-local:qwen2.5:7b"


def test_server_tokens_are_stored_encrypted_and_listed_without_values(home):
    save_env({"OLLAMA_API_KEY_GPU": "secret-gpu-token"})
    assert secret_status()["OLLAMA_API_KEY_GPU"] is True
    assert "secret-gpu-token" not in (home / ".env").read_text() if (home / ".env").exists() else True


def test_servers_api_add_test_list_remove(client, monkeypatch):
    from xplogent.interfaces.api.routes import system

    probes = []

    async def fake_probe(host, token, timeout=8.0):
        probes.append((host, token))
        return {"ok": True, "models": ["qwen2.5:14b"]}

    monkeypatch.setattr(system, "_probe_ollama", fake_probe)
    bad = client.post("/providers/ollama/servers", json={"name": "Bad Name!", "host": "https://x"}).json()
    assert not bad["ok"]
    assert not client.post("/providers/ollama/servers", json={"name": "gpu", "host": "ftp://x"}).json()["ok"]
    out = client.post("/providers/ollama/servers",
                      json={"name": "gpu", "host": "https://gpu.example.com/", "token": "tok-123456"}).json()
    assert out["ok"] and out["servers"] == [{"name": "gpu", "host": "https://gpu.example.com", "token_set": True}]
    assert "tok-123456" not in json.dumps(client.get("/providers/ollama/servers").json())
    assert client.post("/providers/ollama/test", json={"server": "gpu"}).json()["ok"]
    assert probes[-1] == ("https://gpu.example.com", "tok-123456")             # the saved token, server side
    # Updating the URL keeps the token; the default server's settings are untouched.
    client.post("/providers/ollama/servers", json={"name": "gpu", "host": "https://gpu2.example.com"})
    assert client.get("/providers/ollama/servers").json()["servers"][0]["token_set"] is True
    gone = client.delete("/providers/ollama/servers/gpu").json()
    assert gone["ok"] and gone["servers"] == []
    import os
    assert not os.environ.get("OLLAMA_API_KEY_GPU")
    assert (load_config().raw.get("ollama") or {}).get("servers") == []


def test_backup_status_probes_the_named_server(client, monkeypatch):
    from xplogent.interfaces.api.routes import system

    seen = []

    async def fake_probe(host, token, timeout=8.0):
        seen.append((host, token))
        return {"ok": True, "models": ["qwen2.5:7b"]}

    monkeypatch.setattr(system, "_probe_ollama", fake_probe)
    client.post("/providers/ollama/servers", json={"name": "gpu", "host": "https://gpu.example.com", "token": "t-999999"})
    client.post("/providers/backup", json={"server": "gpu", "model": "qwen2.5:7b", "enabled": True})
    st = client.get("/providers/backup").json()
    assert st["server"] == "gpu" and st["reachable"] and st["installed"]
    assert seen[-1] == ("https://gpu.example.com", "t-999999")
