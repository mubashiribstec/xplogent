"""Documents / RAG."""

from __future__ import annotations

from fastapi import APIRouter

from xplogent.core.config import load_config
from xplogent.interfaces.api import deps
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder

router = APIRouter()


# ── Documents / RAG ──────────────────────────────────────────────────────


@router.get("/docs")
async def list_docs() -> dict:
    with Store(load_config().db_path) as store:
        out = store.list_documents()
    return {"documents": out}


@router.post("/docs/ingest")
async def ingest_docs(body: dict) -> dict:
    from xplogent.core.rag import ingest_path, ingest_text
    cfg = load_config()
    store = Store(cfg.db_path)
    provider = deps.build_provider(cfg.embedding_model)
    mem_embedder = Embedder(provider)
    try:
        if body.get("content"):
            res = await ingest_text(store, mem_embedder, str(body["content"]),
                                    str(body.get("title", "pasted")))
        elif body.get("path"):
            res = await ingest_path(store, mem_embedder, str(body["path"]))
        else:
            res = {"ok": False, "error": "provide 'path' or 'content'"}
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": str(exc)}
    finally:
        await provider.aclose()
        store.close()
    return res


@router.get("/docs/search")
async def search_docs(q: str, k: int = 5) -> dict:
    from xplogent.core.rag import hybrid_search
    cfg = load_config()
    store = Store(cfg.db_path)
    provider = deps.build_provider(cfg.embedding_model)
    try:
        hits = await hybrid_search(store, Embedder(provider), q, k=k)
    finally:
        await provider.aclose()
        store.close()
    return {"hits": hits}


@router.delete("/docs/{doc_id}")
async def delete_doc(doc_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_document(doc_id)
    return {"ok": True}
