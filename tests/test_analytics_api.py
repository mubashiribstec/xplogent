"""Analytics aggregation over the REST API."""

from __future__ import annotations

import pytest


def _client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    return TestClient(create_app())


def test_analytics_aggregates_usage(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    # Seed a couple of usage rows directly through the store.
    from xplogent.core.config import load_config
    from xplogent.memory.store import Store

    store = Store(load_config().db_path)
    store.add_usage("openai:gpt-4o", 100, 20, 0.01, None)
    store.add_usage("openai:gpt-4o", 200, 40, 0.02, None)
    store.add_usage("ollama:llama3.1", 50, 10, 0.0, None)
    store.close()

    a = c.get("/analytics").json()
    assert a["totals"]["turns"] == 3
    assert a["totals"]["input_tokens"] == 350
    assert a["totals"]["output_tokens"] == 70
    models = {m["model"]: m for m in a["by_model"]}
    assert models["openai:gpt-4o"]["turns"] == 2
    assert len(a["by_day"]) == 1  # all seeded "now"
