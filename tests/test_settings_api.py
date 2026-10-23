"""Settings / control endpoints over the API."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from xplogent.interfaces.api.server import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.delenv("XPLOGENT_MODEL", raising=False)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return TestClient(create_app())


def test_config_full_shape(client):
    body = client.get("/config/full").json()
    for key in ("model", "safety", "roles", "tools_enabled", "secrets"):
        assert key in body


def test_patch_config_changes_model(client):
    assert client.patch("/config", json={"updates": {"model": "openai:gpt-4o"}}).json()["ok"]
    assert client.get("/config/full").json()["model"] == "openai:gpt-4o"


def test_tools_listing(client):
    tools = client.get("/tools").json()["tools"]
    names = {t["name"] for t in tools}
    assert "shell" in names and "read_file" in names
    assert all("risk" in t and "group" in t for t in tools)


def test_role_edit_round_trip(client):
    client.put("/roles/coder", json={
        "allowed_tools": ["read_file"], "policy": {"low": "auto"},
        "allowed_paths": [], "network": False, "max_steps": 9,
    })
    coder = client.get("/roles").json()["roles"]["coder"]
    assert coder["allowed_tools"] == ["read_file"]
    assert coder["max_steps"] == 9


def test_secrets_masked_status(client):
    assert client.get("/config/full").json()["secrets"]["OPENAI_API_KEY"] is False
    client.put("/secrets", json={"keys": {"OPENAI_API_KEY": "sk-secret"}})
    secrets = client.get("/config/full").json()["secrets"]
    assert secrets["OPENAI_API_KEY"] is True
    # the raw key is never returned
    assert "sk-secret" not in client.get("/config/full").text


def test_memory_fact_lifecycle(client, monkeypatch):
    # avoid a real embedding call
    import xplogent.interfaces.api.deps as deps
    from tests.conftest import ScriptedProvider
    monkeypatch.setattr(deps, "build_provider", lambda *_a, **_k: ScriptedProvider([]))

    client.post("/memory/facts", json={"content": "the sky is blue"})
    facts = client.get("/memory/facts").json()["facts"]
    assert any(f["content"] == "the sky is blue" for f in facts)
    fid = facts[0]["id"]
    assert client.delete(f"/memory/facts/{fid}").json()["ok"]


def test_guide_endpoints(client):
    pages = client.get("/guide").json()["pages"]
    slugs = {p["slug"] for p in pages}
    assert "getting-started" in slugs
    assert client.get("/guide/getting-started").status_code == 200
    assert client.get("/guide/nope").status_code == 404
