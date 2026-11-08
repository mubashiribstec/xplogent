"""Regression tests for the audits.

- 2026-10-23 viral-readiness audit batches (restored 8 Nov: commit 763e4c2 replaced this file
  and dropped them by mistake). Each pins a verified finding so it cannot come back: RAG ingest
  followed symlinks into credential files and let a direct path bypass the extension allowlist;
  imported skill names became file paths on export, knowledge export dropped fields, and one
  malformed entry crashed the import; a SKILL.md with list frontmatter crashed the installer;
  the reflector could create an empty-named skill and read "false" as True; and the later
  fallback, retry, static-route, backend-argument and prompt checks.
- The deployment audit (27 Oct): guide in Docker, chat small talk, junk facts, gaps.
"""
from __future__ import annotations

import time

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.backup import export_knowledge, import_knowledge
from xplogent.core.rag import _iter_files, ingest_path
from xplogent.core.smalltalk import is_small_talk
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder
from xplogent.providers.base import Message, Provider, Role, StreamEvent, StreamKind, ToolCall
from xplogent.skills.pack import parse_skill_md
from xplogent.skills.reflection import Reflector


@pytest.mark.asyncio
async def test_rag_does_not_follow_symlink_to_secret(tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY MATERIAL")
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "leak.txt").symlink_to(secret)
    (folder / "real.md").write_text("harmless notes about deployment")
    names = [p.name for p in _iter_files(folder)]
    assert names == ["real.md"]

    store = Store(tmp_path / "m.db")
    res = await ingest_path(store, Embedder(ScriptedProvider([])), str(folder))
    assert res["ok"] and res["ingested"] == ["real.md"]
    assert all("PRIVATE" not in c["content"] for c in store.all_chunks())
    store.close()


def test_rag_single_file_respects_extension_allowlist(tmp_path):
    f = tmp_path / "passwd"
    f.write_text("root:x:0:0:root:/root:/bin/bash")
    assert _iter_files(f) == []
    g = tmp_path / "notes.txt"
    g.write_text("fine")
    assert _iter_files(g) == [g]


def test_skill_md_with_list_frontmatter_does_not_crash():
    out = parse_skill_md("---\n- a\n- b\n---\n\n# Heading\n\nbody text")
    assert out["name"]  # falls back to the heading, no AttributeError
    out2 = parse_skill_md("---\njust a string\n---\n\nbody")
    assert out2["body"] == "body"


def test_import_knowledge_sanitises_names_keeps_fields_and_tolerates_junk(tmp_path):
    src = Store(tmp_path / "a.db")
    src.upsert_skill("deploy_app", "deploy the app", "steps", [],
                     tools=["shell"], trigger="when deploying", source="learned")
    data = export_knowledge(src)
    src.close()
    assert data["skills"][0]["tools"] == ["shell"]
    assert data["skills"][0]["trigger"] == "when deploying"

    dst = Store(tmp_path / "b.db")
    out = import_knowledge(dst, data)
    assert out["skills_added"] == 1
    row = dst.all_skills()[0]
    assert row.tools == ["shell"] and row.trigger == "when deploying"
    assert row.source == "learned"

    hostile = {"facts": [{"no_content": 1}, {"content": "a real fact"}],
               "skills": [{"name": "../escaped", "body": "x"}]}
    out2 = import_knowledge(dst, hostile)
    assert out2["facts_added"] == 1 and out2["skills_added"] == 1
    assert out2["warnings"]  # the malformed fact was reported, not raised
    names = {s.name for s in dst.all_skills()}
    assert "escaped" in names and "../escaped" not in names
    dst.close()


@pytest.mark.asyncio
async def test_reflector_empty_name_and_string_false():
    from xplogent.providers.base import Message, Role

    reply = ('{"success": "false", "facts": [], '
             '"skill": {"name": "!!!", "body": "do the thing"}}')
    provider = ScriptedProvider([Message(role=Role.ASSISTANT, content=reply)])
    result = await Reflector(provider).reflect("task", "transcript")
    assert result.success is False
    assert result.skill is not None and result.skill.name == "skill"


# ── batch 2: phone matching, keyword fallback, stale re-ingest, FTS ──────

def test_dnc_and_contact_match_short_numbers(tmp_path):
    store = Store(tmp_path / "m.db")
    store.add_dnc("+1 212 555 0100", "opt-out")
    assert store.is_dnc("5550100") is True       # 7-digit form of the same number
    assert store.is_dnc("+12125550100") is True
    assert store.is_dnc("5550199") is False
    assert store.is_dnc("123") is False          # too short to trust

    cid = store.add_contact("Paul", "+1 (212) 555-0100")
    assert cid
    found = store.find_contact_by_phone("5550100")
    assert found is not None and found["name"] == "Paul"
    assert store.find_contact_by_phone("5550199") is None
    store.close()


@pytest.mark.asyncio
async def test_keyword_fallback_recall_with_natural_query(tmp_path):
    from xplogent.memory.manager import MemoryManager
    from xplogent.providers.base import Provider

    class NoEmbed(Provider):
        name = "noembed"

        async def stream(self, messages, tools=None, **kwargs):  # pragma: no cover
            raise NotImplementedError
            yield

    store = Store(tmp_path / "m.db")
    store.add_fact("The user prefers dark roast coffee in the morning", [], "test")
    store.add_fact("The office cat is named Mochi", [], "test")
    store.upsert_skill("deploy_app", "deploy the web app to production", "steps", [])
    mgr = MemoryManager(store, Embedder(NoEmbed(model="x")))
    hits = await mgr.recall("user coffee preference")
    assert hits and "coffee" in hits[0]
    skills = await mgr.relevant_skills("deploy the app please")
    assert skills and skills[0].name == "deploy_app"
    store.close()


@pytest.mark.asyncio
async def test_reingest_replaces_stale_version(tmp_path):
    store = Store(tmp_path / "m.db")
    embedder = Embedder(ScriptedProvider([]))
    f = tmp_path / "note.txt"
    f.write_text("version one content")
    await ingest_path(store, embedder, str(f))
    f.write_text("version two content changed")
    await ingest_path(store, embedder, str(f))
    docs = [d for d in store.list_documents() if d["title"] == "note.txt"]
    assert len(docs) == 1
    contents = [c["content"] for c in store.all_chunks()]
    assert any("version two" in c for c in contents)
    assert not any("version one" in c for c in contents)
    store.close()


def test_doc_chunks_fts_backfilled_on_open(tmp_path):
    db = tmp_path / "m.db"
    store = Store(db)
    doc_id = store.add_document("/x/a.txt", "a.txt", "h1")
    store.add_chunk(doc_id, 0, "quixotic zebras congregate", [])
    # Simulate chunks that predate the FTS index: wipe the index, reopen.
    store.conn.execute("DELETE FROM doc_chunks_fts")
    store.conn.commit()
    store.close()
    store2 = Store(db)
    hits = store2.search_chunks_fts("quixotic", 5)
    assert hits and "quixotic" in hits[0]["content"]
    store2.close()


# ── batch 3: fallback truncation, empty model spec, 408 retry ────────────

class _TruncatedProvider(Provider):
    name = "truncated"

    async def stream(self, messages, tools=None, **kwargs):
        yield StreamEvent(kind=StreamKind.TOKEN, text="partial ")
        yield StreamEvent(kind=StreamKind.TOKEN, text="answer")
        # stream just ends: no DONE, no exception (a gateway cut)

    async def embed(self, texts):
        return [[] for _ in texts]


class _SilentProvider(Provider):
    name = "silent"

    async def stream(self, messages, tools=None, **kwargs):
        return
        yield  # pragma: no cover - makes this an async generator

    async def embed(self, texts):
        return [[] for _ in texts]


class _GoodProvider(Provider):
    name = "good"

    async def stream(self, messages, tools=None, **kwargs):
        yield StreamEvent(kind=StreamKind.TOKEN, text="from backup")
        yield StreamEvent(kind=StreamKind.DONE,
                          message=Message(role=Role.ASSISTANT, content="from backup"))

    async def embed(self, texts):
        return [[] for _ in texts]


@pytest.mark.asyncio
async def test_fallback_keeps_partial_answer_when_stream_truncates():
    from xplogent.providers.fallback import FallbackProvider

    fb = FallbackProvider(_TruncatedProvider(model="m"), lambda: _GoodProvider(model="b"))
    msg = await fb.complete([Message(role=Role.USER, content="hi")])
    assert msg.content == "partial answer"
    assert fb.used_backup is False


@pytest.mark.asyncio
async def test_fallback_uses_backup_when_primary_answers_nothing():
    from xplogent.providers.fallback import FallbackProvider

    fb = FallbackProvider(_SilentProvider(model="m"), lambda: _GoodProvider(model="b"))
    msg = await fb.complete([Message(role=Role.USER, content="hi")])
    assert msg.content == "from backup"
    assert fb.used_backup is True


def test_empty_model_spec_is_an_error():
    from xplogent.providers import registry

    with pytest.raises(ValueError, match="no model"):
        registry._make("ollama:")


def test_http_408_is_retryable():
    import httpx

    from xplogent.core.retry import ErrorClass, classify_error

    req = httpx.Request("GET", "http://example.test")
    err = httpx.HTTPStatusError("timeout", request=req,
                                response=httpx.Response(408, request=req))
    assert classify_error(err) == ErrorClass.TRANSIENT


# ── batch 4: public-path tightening + session existence ──────────────────

def test_static_bypass_only_at_dashboard_depths():
    from xplogent.interfaces.api.deps import is_public

    assert is_public("/", "GET")
    assert is_public("/assets/index-abc.js", "GET")
    assert is_public("/favicon.ico", "GET")
    # Route-shaped paths that merely END in a static extension are not public.
    assert not is_public("/sessions/1/messages.png", "GET")
    assert not is_public("/config/full.css", "GET")
    assert not is_public("/analytics/x.map", "GET")
    assert not is_public("/assets/index-abc.js", "POST")


def test_session_exists(tmp_path):
    store = Store(tmp_path / "m.db")
    sid = store.create_session("chat")
    assert store.session_exists(sid) is True
    assert store.session_exists(sid + 1000) is False
    store.close()


# ── batch 5: execution-backend guards ────────────────────────────────────

@pytest.mark.asyncio
async def test_docker_backend_refuses_to_mount_system_dirs():
    from xplogent.core.backends import DockerBackend

    code, _out, err = await DockerBackend().run("ls", cwd="/etc")
    assert code == 1 and "refusing to mount" in err


@pytest.mark.asyncio
async def test_docker_backend_rejects_dash_image():
    from xplogent.core.backends import DockerBackend

    code, _out, err = await DockerBackend(image="-oProxyCommand=x").run("ls")
    assert code == 1 and "invalid image" in err


@pytest.mark.asyncio
async def test_ssh_backend_rejects_dash_host():
    from xplogent.core.backends import SSHBackend

    code, _out, err = await SSHBackend(host="-oProxyCommand=x").run("ls")
    assert code == 1 and "invalid host" in err


def test_default_prompt_only_claims_real_tools():
    from xplogent.core.context import DEFAULT_SYSTEM_PROMPT

    for gone in ("mouse", "keyboard", "browser", "execute Python", "search and fetch"):
        assert gone not in DEFAULT_SYSTEM_PROMPT


def test_context_window_stays_bounded():
    from xplogent.core.context import ShortTermMemory, _approx_tokens
    from xplogent.providers.base import Message, Role

    mem = ShortTermMemory(max_tokens=100)
    mem.add(Message(role=Role.USER, content="the task"))
    for i in range(30):
        mem.add(Message(role=Role.USER, content=f"round {i} " + "x" * 120))
        mem.add(Message(role=Role.ASSISTANT, content="y" * 120))
        mem.trim()
    rendered = mem.render()
    total = sum(_approx_tokens(m.content) for m in rendered)
    assert total <= 400  # was ~1600 before the summary was counted + capped


def test_api_only_root_explains_itself():
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    import xplogent.interfaces.api.server as srv

    with patch.object(srv, "_dashboard_dir", return_value=None):
        with TestClient(srv.create_app()) as client:
            r = client.get("/")
    assert r.status_code == 200
    assert r.json()["dashboard"] == "not built"


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
    docker = (root / "deploy/docker/Dockerfile").read_text(encoding="utf-8")
    assert "COPY docs/guide /app/guide" in docker and "XPLOGENT_GUIDE_DIR=/app/guide" in docker
    assert "!docs/guide" in (root / ".dockerignore").read_text(encoding="utf-8")


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
