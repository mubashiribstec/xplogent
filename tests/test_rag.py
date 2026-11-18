"""RAG: chunking, ingest, and hybrid (semantic + bm25) search with citations."""

from __future__ import annotations

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.rag import chunk_text, hybrid_search, ingest_path, ingest_text
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder


def test_chunk_text_splits_on_paragraphs():
    text = "\n\n".join(f"paragraph number {i} with some words" for i in range(200))
    chunks = chunk_text(text, size=400, overlap=50)
    assert len(chunks) > 1
    assert all(len(c) <= 700 for c in chunks)


@pytest.mark.asyncio
async def test_ingest_and_search(tmp_path):
    store = Store(tmp_path / "m.db")
    embedder = Embedder(ScriptedProvider([]))
    f = tmp_path / "notes.md"
    f.write_text("# Deploy\n\nWe deploy the website every Friday to the prod server.\n\n"
                 "# Pets\n\nThe office cat is named Mochi.")
    res = await ingest_path(store, embedder, str(f))
    assert res["ok"] and res["chunks"] >= 1
    assert store.list_documents()[0]["title"] == "notes.md"

    hits = await hybrid_search(store, embedder, "when do we deploy the website", k=3)
    assert hits
    assert any("Friday" in h["content"] for h in hits)
    assert hits[0]["source"] == "notes.md"
    store.close()


@pytest.mark.asyncio
async def test_ingest_dedupes_by_hash(tmp_path):
    store = Store(tmp_path / "m.db")
    embedder = Embedder(ScriptedProvider([]))
    await ingest_text(store, embedder, "same content here", "a")
    await ingest_text(store, embedder, "same content here", "b")  # identical → skipped
    assert len(store.list_documents()) == 1
    store.close()


@pytest.mark.asyncio
async def test_delete_document(tmp_path):
    store = Store(tmp_path / "m.db")
    embedder = Embedder(ScriptedProvider([]))
    await ingest_text(store, embedder, "delete me please", "x")
    doc_id = store.list_documents()[0]["id"]
    store.delete_document(doc_id)
    assert store.list_documents() == []
    assert await hybrid_search(store, embedder, "delete me", k=3) == []
    store.close()


# ── audit 18 Nov: P1-12, P1-13, P2-19, P2-20 ───────────────────────────────────────────
FAKE_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"


@pytest.mark.parametrize("name", ["credentials.txt", "secrets.md", "prod.env", ".envrc",
                                  "service-account.json", "api-keys.csv", "my_private_key.txt"])
def test_secret_looking_names_are_refused(name):
    from pathlib import Path

    from xplogent.core.rag import is_sensitive
    assert is_sensitive(Path(name))
    assert is_sensitive(Path("secrets") / "notes.txt")            # a secrets/ folder too


@pytest.mark.parametrize("name", ["password-reset-policy.md", "faq.md", "tokens-of-thanks.txt", "envelope.md"])
def test_ordinary_names_are_fine(name):
    from pathlib import Path

    from xplogent.core.rag import is_sensitive
    assert not is_sensitive(Path(name))


@pytest.mark.parametrize("text", [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
    f"our key is {FAKE_KEY}", "AKIAIOSFODNN7EXAMPLE is the id", "export STRIPE_SECRET_KEY=whsec_1234567890",
    "DB_PASSWORD = 'hunter2hunter2'", "ghp_" + "x" * 36,
])
def test_secret_content_is_found(text):
    from xplogent.core.rag import secret_in_text
    assert secret_in_text(text)


@pytest.mark.parametrize("text", [
    "Reset your password via the portal.", "Shifts pay £12.60 an hour; tokens of thanks at Christmas.",
    "Password: ask the office manager.",
])
def test_ordinary_text_is_not_a_secret(text):
    from xplogent.core.rag import secret_in_text
    assert not secret_in_text(text)


async def test_pasted_secrets_and_huge_text_are_refused(tmp_path, monkeypatch):
    from xplogent.core import rag
    store = Store(tmp_path / "m.db")
    embedder = Embedder(ScriptedProvider([]))
    res = await ingest_text(store, embedder, f"OPENAI_API_KEY={FAKE_KEY}\n", "env dump")
    assert not res["ok"] and "API key" in res["error"]
    res = await ingest_text(store, embedder, "hello", ".env")
    assert not res["ok"]
    monkeypatch.setattr(rag, "MAX_TEXT_CHARS", 10)
    assert not (await ingest_text(store, embedder, "x" * 11, "big"))["ok"]
    store.close()


async def test_folder_ingest_skips_secret_files_big_files_and_symlinks(tmp_path, monkeypatch):
    from xplogent.core import rag
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "faq.md").write_text("We open at nine.")
    (docs / "notes.txt").write_text(f"api_key: {FAKE_KEY}")
    (docs / "big.txt").write_text("y" * 100)
    target = tmp_path / "target.txt"
    target.write_text("linked text")
    (docs / "link.txt").symlink_to(target)
    monkeypatch.setattr(rag, "MAX_FILE_BYTES", 50)
    store = Store(tmp_path / "m.db")
    res = await ingest_path(store, Embedder(ScriptedProvider([])), str(docs))
    assert res["ingested"] == ["faq.md"]
    assert any("notes.txt" in r and "API key" in r for r in res["refused"])
    assert any("big.txt" in r for r in res["refused"])
    assert (await ingest_path(store, Embedder(ScriptedProvider([])), str(docs / "link.txt")))["ingested"] == []
    store.close()


def test_a_parent_folder_named_like_a_secret_does_not_hide_documents():
    from pathlib import Path

    from xplogent.core.rag import is_sensitive
    assert not is_sensitive(Path("/srv/secret-projects/handbook/faq.md"))
    assert not is_sensitive(Path("/tmp/test_credentials_case0/proj/notes.md"))
    assert is_sensitive(Path("/srv/docs/secrets/notes.md"))
