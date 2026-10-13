"""The cached knowledge-search index returns exactly what the old per-query scan did."""

from __future__ import annotations

import builtins
import random

import pytest

from xplogent.core import rag
from xplogent.memory.store import Store
from xplogent.memory.vector import cosine


def _old_semantic_rank(store: Store, qvec: list[float], limit: int = 20) -> list[int]:
    """The pre-index implementation, kept verbatim as the reference."""
    chunks = store.all_chunks()
    scored = [(c, cosine(qvec, c["embedding"])) for c in chunks if c["embedding"]]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [c["id"] for c, _ in scored[:limit]]


def _fill(store: Store, n: int, seed: int) -> None:
    rnd = random.Random(seed)
    doc = store.add_document("src", "doc", f"h{seed}")
    for i in range(n):
        kind = rnd.random()
        if kind < 0.05:
            vec: list[float] = []                       # never embedded
        elif kind < 0.10:
            vec = [0.0] * 8                              # zero vector
        elif kind < 0.20:
            vec = [rnd.uniform(-1, 1) for _ in range(4)]  # other embedding model
        elif kind < 0.25:
            vec = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # exact ties
        else:
            vec = [rnd.uniform(-1, 1) for _ in range(8)]
        store.add_chunk(doc, i, f"chunk {i}", vec)


@pytest.mark.parametrize("use_numpy", [True, False])
def test_index_ranking_matches_reference(tmp_path, monkeypatch, use_numpy):
    if use_numpy:
        pytest.importorskip("numpy")
    else:
        real_import = builtins.__import__

        def no_numpy(name, *a, **k):
            if name == "numpy":
                raise ImportError("no numpy")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", no_numpy)
    rag._INDEXES.clear()
    store = Store(tmp_path / "r.db")
    _fill(store, 400, seed=1)
    rnd = random.Random(7)
    for _ in range(25):
        q = [rnd.uniform(-1, 1) for _ in range(rnd.choice([8, 8, 8, 4, 3]))]
        assert rag._chunk_index(store).rank(q, 20) == _old_semantic_rank(store, q)
    q = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]          # ties resolved by id order
    assert rag._chunk_index(store).rank(q, 20) == _old_semantic_rank(store, q)
    store.close()


def test_index_rebuilds_when_chunks_change_even_from_another_connection(tmp_path):
    rag._INDEXES.clear()
    path = tmp_path / "r.db"
    a = Store(path)
    _fill(a, 50, seed=2)
    first = rag._chunk_index(a)
    assert rag._chunk_index(a) is first                     # cached between queries

    b = Store(path)                                         # e.g. a CLI ingest
    doc = b.add_document("x", "x", "other")
    b.add_chunk(doc, 0, "new", [0.5] * 8)
    second = rag._chunk_index(a)
    assert second is not first and len(second.ids) == len(first.ids) + 1

    b.delete_document(doc)
    assert len(rag._chunk_index(a).ids) == len(first.ids)

    missing = a.chunks_missing_embeddings(1)
    if missing:                                             # backfill also invalidates
        before = rag._chunk_index(a)
        a.set_chunk_embedding(missing[0]["id"], [0.1] * 8)
        assert rag._chunk_index(a) is not before
    a.close()
    b.close()


@pytest.mark.asyncio
async def test_hybrid_search_results_unchanged(tmp_path):
    """End to end: same fused results as computing the semantic ranks the old way."""
    from tests.conftest import ScriptedProvider
    from xplogent.memory.vector import Embedder

    rag._INDEXES.clear()
    store = Store(tmp_path / "h.db")
    emb = Embedder(ScriptedProvider([]))
    texts = ["the warranty lasts two years", "returns accepted within 30 days",
             "shipping is free over fifty dollars", "support is open 9 to 5",
             "warranty claims need a receipt"] * 6
    for i, t in enumerate(texts):
        await rag.ingest_text(store, emb, f"{t} #{i}", title=f"d{i}")
    for query in ("warranty", "how long is the warranty", "free shipping", "support hours"):
        got = await rag.hybrid_search(store, emb, query, k=5)
        qvec = await emb.embed_one(query)
        sem = {cid: i for i, cid in enumerate(_old_semantic_rank(store, qvec))}
        fts = {row["id"]: i for i, row in enumerate(store.search_chunks_fts(query, 20))}
        fused = {cid: (1 / (60 + fts[cid]) if cid in fts else 0) + (1 / (60 + sem[cid]) if cid in sem else 0)
                 for cid in set(sem) | set(fts)}
        want = sorted(fused, key=lambda c: fused[c], reverse=True)[:5]
        contents = {c["id"]: c["content"] for c in store.all_chunks()}
        assert [h["content"] for h in got] == [contents[c] for c in want]
    store.close()


# ── trace recorder batching ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_recorder_merges_tokens_and_flushes_on_stop(tmp_path):
    import time as _time

    from xplogent.core.events import Event, EventBus, EventType
    from xplogent.monitor.recorder import TraceRecorder

    store = Store(tmp_path / "e.db")
    store.add_events([("old", "_", "token", {"text": "x"})])
    with store._lock:
        store.conn.execute("UPDATE events SET created_at=?", (_time.time() - 90 * 86400,))
        store.conn.commit()
    bus = EventBus()
    rec = TraceRecorder(bus, store, "run1")
    rec.start()                                   # prunes the 90-day-old event
    assert store.run_events("old") == []
    await bus.publish(Event(type=EventType.STEP_START, data={"agent_id": "a"}))
    words = [f"w{i} " for i in range(500)]
    for w in words:
        await bus.publish(Event(type=EventType.TOKEN, data={"agent_id": "a", "text": w}))
    await bus.publish(Event(type=EventType.RUN_END, data={"agent_id": "a", "answer": "ok"}))
    await rec.stop()
    events = store.run_events("run1")
    tokens = [e for e in events if e["type"] == str(EventType.TOKEN)]
    assert "".join(e["data"]["text"] for e in tokens) == "".join(words)
    assert len(tokens) < 50                       # merged, not one row per token
    assert events[-1]["type"] == str(EventType.RUN_END)
    assert rec.snapshot()[0]["steps"] == 1
    store.close()
