"""Fixes from the deployment audit: guide in Docker, chat small talk, junk facts, gaps."""

from __future__ import annotations

import time

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.smalltalk import is_small_talk
from xplogent.providers.base import Message, Role, ToolCall


@pytest.mark.parametrize("text", ["hi", "Hello!", "hey there", "thanks", "Thank you so much",
                                  "good morning", "how are you?", "ok", "AoA", "what's up"])
def test_small_talk(text):
    assert is_small_talk(text)


@pytest.mark.parametrize("text", ["hi, can you read my notes?", "hello what is the price",
                                  "list the files in /tmp", "thanks, now write a summary of the call",
                                  "how do I set up the PBX extension?", "Our prices start at 99"])
def test_not_small_talk(text):
    assert not is_small_talk(text)


def test_guide_dir_from_env(tmp_path, monkeypatch):
    from xplogent.core import guide

    (tmp_path / "01-start.md").write_text("# Getting started\n")
    monkeypatch.setenv("XPLOGENT_GUIDE_DIR", str(tmp_path))
    assert guide.list_pages() == [{"slug": "start", "title": "Getting started", "file": "01-start.md"}]


def test_docker_image_ships_the_guide():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    docker = (root / "deploy/docker/Dockerfile").read_text()
    assert "COPY docs/guide /app/guide" in docker and "XPLOGENT_GUIDE_DIR=/app/guide" in docker
    assert "!docs/guide" in (root / ".dockerignore").read_text()


def _agent(tmp_path, monkeypatch, replies):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.core.agent import Agent
    from xplogent.core.config import load_config
    from xplogent.safety.approval import SafetyManager
    from xplogent.tools.registry import ToolRegistry

    class Recording(ScriptedProvider):
        def __init__(self, replies):
            super().__init__(replies)
            self.calls = []

        async def stream(self, messages, tools=None, **kw):
            self.calls.append({"tools": tools})
            async for ev in super().stream(messages, tools, **kw):
                yield ev

    prov = Recording(replies)
    agent = Agent(load_config(), prov, ToolRegistry.from_config(["filesystem"]), SafetyManager())
    return agent, prov


async def test_greeting_gets_no_tools_and_no_reflection(tmp_path, monkeypatch):
    agent, prov = _agent(tmp_path, monkeypatch, [Message(role=Role.ASSISTANT, content="Hi! How can I help?")])
    reflected = []

    async def post(task, transcript, steps):
        reflected.append(steps)

    monkeypatch.setattr(agent, "_post_task", post)
    assert await agent.run("hi") == "Hi! How can I help?"
    assert prov.calls[0]["tools"] in (None, [])                  # no tools offered for "hi"
    assert reflected == [0]


async def test_a_failed_tool_is_not_worth_reflecting_on(tmp_path, monkeypatch):
    agent, _ = _agent(tmp_path, monkeypatch, [
        Message(role=Role.ASSISTANT, content="", tool_calls=[
            ToolCall(id="1", name="read_file", arguments={"path": "/nope/notes.txt"})]),
        Message(role=Role.ASSISTANT, content="That file doesn't exist; I'll create it.")])
    steps = []

    async def post(task, transcript, n):
        steps.append(n)

    monkeypatch.setattr(agent, "_post_task", post)
    await agent.run("open my notes file please")
    assert steps == [0]                                           # the only tool call failed


def test_forget_learned_facts_keeps_yours(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from fastapi.testclient import TestClient

    from xplogent.core.config import load_config
    from xplogent.interfaces.api.server import create_app
    from xplogent.memory.store import Store

    with Store(load_config().db_path) as s:
        s.add_fact("User has a file named notes.txt", [], "reflection")
        s.add_fact("Prices start at 99", [], "gui")
        old = s.add_fact("Learned long ago", [], "reflection")
        s._write("UPDATE facts SET created_at=? WHERE id=?", (time.time() - 3 * 86400, old))
    c = TestClient(create_app(), base_url="http://127.0.0.1")
    facts = c.get("/memory/facts").json()["facts"]
    assert all("created_at" in f for f in facts)
    assert c.post("/memory/facts/forget-learned", json={"hours": 24}).json()["removed"] == 1
    left = {f["content"] for f in c.get("/memory/facts").json()["facts"]}
    assert left == {"Prices start at 99", "Learned long ago"}


def test_small_talk_is_not_a_knowledge_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.voip import jsonl
    from xplogent.voip.service import knowledge_gap_path, log_knowledge_gap, top_knowledge_gaps

    log_knowledge_gap("hi")
    log_knowledge_gap("Do you install on flat roofs?")
    jsonl.append(knowledge_gap_path(), {"ts": time.time(), "query": "hello"})   # logged by an old build
    assert [g["query"] for g in top_knowledge_gaps()] == ["Do you install on flat roofs?"]


def test_readiness_flags_the_placeholder_company():
    from xplogent.voip.readiness import checklist

    items = {i["id"]: i for i in checklist({"company": "our company"}, "openai:x", env={"OPENAI_API_KEY": "k"},
                                           gpu=True)}
    assert not items["company"]["ok"] and not items["company"]["required"]
    items = {i["id"]: i for i in checklist({"company": "Kingsbury"}, "openai:x", env={"OPENAI_API_KEY": "k"}, gpu=True)}
    assert items["company"]["ok"]


def test_ws_ping_answers_without_a_call(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app

    with TestClient(create_app(), base_url="http://127.0.0.1").websocket_connect("/ws/voip/ping") as ws:
        ws.send_text("{}")
        assert ws.receive_json() == {"type": "pong"}
