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


def test_status_reports_a_failing_part_instead_of_500(monkeypatch, tmp_path):
    """Muse's 4 Nov report: the Health card said 'Backend unreachable' while the backend was
    up. Any part of /status that throws now shows in ``errors``; the rest still answers."""
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.routes import system
    from xplogent.interfaces.api.server import create_app

    def broken():
        raise RuntimeError("secrets store unreadable")

    monkeypatch.setattr(system, "secret_status", broken)
    monkeypatch.setattr(system, "available_providers", lambda: (_ for _ in ()).throw(ValueError("bad provider")))
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["errors"]["secrets"] == "RuntimeError: secrets store unreadable"
    assert "bad provider" in body["errors"]["providers"]
    assert body["secrets"] == {} and body["providers"] == [] and body["model"]
    assert "revision" in body or "version" in body           # the build info still answers
