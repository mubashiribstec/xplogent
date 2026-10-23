"""Regression tests for the 2026-10-23 viral-readiness audit batches.

Each test pins a verified finding so it cannot come back:
- RAG ingest followed symlinks into credential files and let a direct path
  bypass the extension allowlist.
- Imported skill names became file paths on export (traversal), knowledge
  export dropped tools/trigger/source, and one malformed entry crashed the
  whole import.
- A SKILL.md whose frontmatter is a YAML list crashed the installer.
- The reflector could create an empty-named skill and read "false" as True.
"""
from __future__ import annotations

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.backup import export_knowledge, import_knowledge
from xplogent.core.rag import _iter_files, ingest_path
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder
from xplogent.providers.base import Message, Provider, Role, StreamEvent, StreamKind
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
