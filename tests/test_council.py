"""Multi-model 'council' chat over the websocket: fan-out + synthesis."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")


def test_council_fans_out_and_synthesizes(monkeypatch, tmp_path):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from fastapi.testclient import TestClient

    import xplogent.interfaces.api.deps as deps
    import xplogent.interfaces.api.server as srv
    from tests.conftest import ScriptedProvider
    from xplogent.providers.base import Message, Role

    def fake_build(model, **_k):
        return ScriptedProvider([Message(role=Role.ASSISTANT, content=f"ans-from-{model}")])

    monkeypatch.setattr(deps, "build_provider", fake_build)
    client = TestClient(srv.create_app())

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "session"
        ws.send_json({"type": "task", "task": "what is 2+2?",
                      "models": ["openai:gpt-4o", "anthropic:claude-sonnet-4-6"],
                      "synth_model": "openai:gpt-4o"})
        channels: dict[str, str] = {}
        while True:
            ev = ws.receive_json()
            if ev["type"] == "council_token":
                channels[ev["channel"]] = channels.get(ev["channel"], "") + ev["text"]
            elif ev["type"] == "done":
                break

    assert "openai:gpt-4o" in channels
    assert "anthropic:claude-sonnet-4-6" in channels
    assert "synthesis" in channels
    assert "ans-from-openai:gpt-4o" in channels["openai:gpt-4o"]
