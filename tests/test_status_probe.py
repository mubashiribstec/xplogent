"""The Ollama probe behind GET /status must never raise (regression: a malformed
saved host made /status 500, so the dashboard showed 'Backend unreachable' and a
red health dot while the server was fine)."""
from __future__ import annotations

import asyncio

from xplogent.interfaces.api.routes.system import _probe_ollama


def _run(host: str) -> dict:
    return asyncio.run(_probe_ollama(host, "", timeout=1.0))


def test_probe_survives_a_bad_port():
    out = _run("http://localhost:bad")
    assert out["ok"] is False and out["models"] == [] and out["error"]


def test_probe_survives_a_garbage_host():
    out = _run("http://exa mple.com:11434")
    assert out["ok"] is False and out["error"]


def test_probe_reports_unreachable_cleanly():
    out = _run("http://127.0.0.1:1")  # nothing listens there
    assert out["ok"] is False and "cannot reach" in out["error"]


def test_status_endpoint_is_200_with_a_malformed_saved_host(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:bad")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        resp = client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["ollama"]["reachable"] is False
