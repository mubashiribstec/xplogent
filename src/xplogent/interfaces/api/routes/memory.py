"""Memory, skills, knowledge graph, persona (SOUL.md / MEMORY.md)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from xplogent.core.config import load_config
from xplogent.interfaces.api import deps
from xplogent.interfaces.api.models import (
    FactBody,
    TextBody,
)
from xplogent.memory.manager import MemoryManager
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder

router = APIRouter()


# ── Memory & skills management ───────────────────────────────────────────


@router.get("/memory/facts")
async def list_facts() -> dict:
    with Store(load_config().db_path) as store:
        out = [{"id": f.id, "content": f.content, "source": f.source} for f in store.all_facts()]
    return {"facts": out}


@router.post("/memory/facts")
async def add_fact(body: FactBody) -> dict:
    cfg = load_config()
    store = Store(cfg.db_path)
    embed_provider = deps.build_provider(cfg.embedding_model)
    mem = MemoryManager(store, Embedder(embed_provider))
    try:
        fact_id = await mem.remember(body.content, source="gui")
    finally:
        await embed_provider.aclose()
        store.close()
    return {"ok": True, "id": fact_id}


@router.delete("/memory/facts/{fact_id}")
async def delete_fact(fact_id: int) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_fact(fact_id)
    return {"ok": True}


@router.delete("/skills/{name}")
async def delete_skill(name: str) -> dict:
    with Store(load_config().db_path) as store:
        store.delete_skill(name)
    return {"ok": True}


@router.get("/skills/library")
async def skills_library() -> dict:
    from xplogent.skills.hub import list_bundled
    return {"packs": list_bundled()}


@router.post("/skills/install")
async def skills_install(body: dict) -> dict:
    from xplogent.skills.hub import install_pack, install_text
    cfg = load_config()
    store = Store(cfg.db_path)
    embed_provider = deps.build_provider(cfg.embedding_model)
    mem = MemoryManager(store, Embedder(embed_provider))
    try:
        if body.get("skill_md"):
            res = await install_text(str(body["skill_md"]), mem)
        elif body.get("src"):
            res = await install_pack(str(body["src"]), mem)
        else:
            res = {"ok": False, "error": "provide 'src' (path/url/pack) or 'skill_md'"}
        for nm in (res.get("installed") or []) if isinstance(res, dict) else []:
            store.add_skill_event(str(nm), "installed", "novice", 1, "installed from hub")
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": str(exc)}
    finally:
        await embed_provider.aclose()
        store.close()
    return res


# ── Knowledge graph ──────────────────────────────────────────────────────


@router.get("/graph")
async def graph(limit: int = 300) -> dict:
    with Store(load_config().db_path) as store:
        snap = store.graph_snapshot(limit=limit)
    return snap


@router.get("/graph/neighbors")
async def graph_neighbors(entity: str) -> dict:
    with Store(load_config().db_path) as store:
        rows = store.neighbors(entity, limit=50)
    return {"entity": entity, "edges": rows}


# ── Persona (SOUL.md) + curated memory (MEMORY.md) ───────────────────────


@router.get("/persona/soul")
async def get_soul() -> dict:
    from xplogent.core.persona import load_soul
    return {"content": load_soul()}


@router.put("/persona/soul")
async def put_soul(body: TextBody) -> dict:
    from xplogent.core.persona import save_soul
    save_soul(body.content)
    return {"ok": True}


@router.get("/persona/memory")
async def get_memory_md() -> dict:
    from xplogent.core.persona import load_memory
    return {"content": load_memory()}


@router.put("/persona/memory")
async def put_memory_md(body: TextBody) -> dict:
    from xplogent.core.persona import save_memory
    save_memory(body.content)
    return {"ok": True}


@router.post("/memory/compact")
async def memory_compact() -> dict:
    from xplogent.core.persona import compact_memory
    cfg = load_config()
    store = Store(cfg.db_path)
    provider = deps.build_provider(cfg.reflection_model)
    try:
        content = await compact_memory(store, provider)
    finally:
        await provider.aclose()
        store.close()
    return {"ok": True, "content": content}


@router.get("/skills/history")
async def skills_history(limit: int = 50) -> dict:
    with Store(load_config().db_path) as store:
        out = store.skill_events(limit=limit)
    return {"events": out}


@router.post("/memory/consolidate")
async def memory_consolidate() -> dict:
    """End-of-conversation save: flush every skill to the skills folder and
    distill MEMORY.md. Reused by the GUI when a chat ends."""
    from xplogent.core.persona import compact_memory
    from xplogent.skills.manager import SkillManager

    cfg = load_config()
    store = Store(cfg.db_path)
    embed_provider = deps.build_provider(cfg.embedding_model)
    provider = deps.build_provider(cfg.reflection_model)
    try:
        sm = SkillManager(MemoryManager(store, Embedder(embed_provider)), cfg.skills_dir)
        exported = await asyncio.to_thread(sm.export_all)
        content = await compact_memory(store, provider)
    finally:
        await embed_provider.aclose()
        await provider.aclose()
        store.close()
    return {"ok": True, "skills_saved": exported, "memory": content}
