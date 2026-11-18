"""The chat assistant searches the knowledge base too (audit 18 Nov: chat had no RAG)."""

from __future__ import annotations

import pytest

from tests.conftest import ScriptedProvider


@pytest.fixture
def kb(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    import xplogent.providers.registry as registry
    monkeypatch.setattr(registry, "build_provider", lambda spec, **k: ScriptedProvider([]))
    from xplogent.core.config import load_config, save_user_config
    # The test embedder buckets letters, so any two English texts look alike: a high floor
    # means only keyword matches count here (the real floor is 0.3 with real embeddings).
    save_user_config({"voip": {"knowledge_min_similarity": 0.999}})
    return load_config()


async def test_chat_finds_passages_with_their_source(kb, tmp_path):
    from xplogent.core.rag import ingest_path
    from xplogent.memory.store import Store
    from xplogent.memory.vector import Embedder
    from xplogent.tools.knowledge import KnowledgeTool

    doc = tmp_path / "pay-policy.md"
    doc.write_text("Care assistants are paid weekly, every Friday.")
    store = Store(kb.db_path)
    await ingest_path(store, Embedder(ScriptedProvider([])), str(doc))
    store.close()
    res = await KnowledgeTool().run(query="care assistants paid weekly")
    assert res.ok and "pay-policy.md" in res.output and "every Friday" in res.output
    none = await KnowledgeTool().run(query="zebra migration")
    assert none.ok and "No relevant passage" in none.output
    assert not (await KnowledgeTool().run(query="  ")).ok


def test_the_tool_is_on_even_with_an_older_saved_tool_list():
    from xplogent.tools.registry import ToolRegistry
    names = {s.name for s in ToolRegistry.from_config(["shell"]).specs()}
    assert "search_knowledge" in names and "shell" in names
