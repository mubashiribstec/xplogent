"""Knowledge-base search for the chat assistant.

The phone agent already answers from the knowledge base; this gives the dashboard chat the
same documents (hybrid keyword + semantic search), with each passage labelled by its source
so answers can cite it. Read-only.
"""

from __future__ import annotations

from typing import Any

from xplogent.safety.approval import RiskLevel
from xplogent.tools.base import Tool, ToolResult


class KnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "Search the company knowledge base (uploaded documents, FAQs, policies, job details, "
        "call-training material). Use it before answering questions about the company, its "
        "vacancies, pay, policies or procedures, and name the source document in your answer. "
        "If nothing relevant is found, say so instead of guessing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look up, in plain words."},
            "k": {"type": "integer", "minimum": 1, "maximum": 8,
                  "description": "How many passages (default 5)."},
        },
        "required": ["query"],
    }
    risk = RiskLevel.LOW

    async def run(self, query: str = "", k: int = 5, **_: Any) -> ToolResult:
        query = " ".join(str(query or "").split())[:500]
        if not query:
            return ToolResult.failure("provide a 'query'")
        from xplogent.core.config import load_config
        from xplogent.core.rag import hybrid_search
        from xplogent.memory.store import Store
        from xplogent.memory.vector import Embedder
        from xplogent.providers.registry import build_provider

        cfg = load_config()
        min_sim = float((cfg.voip or {}).get("knowledge_min_similarity", 0.3) or 0)
        provider = build_provider(cfg.embedding_model)
        store = Store(cfg.db_path)
        try:
            hits = await hybrid_search(store, Embedder(provider), query, k=max(1, min(8, int(k or 5))))
        except Exception as exc:  # noqa: BLE001 - report, don't crash the chat turn
            return ToolResult.failure(f"the knowledge base is unavailable: {type(exc).__name__}")
        finally:
            store.close()
            await provider.aclose()
        hits = [h for h in hits if h.get("keyword") or h.get("similarity", 0.0) >= min_sim]
        if not hits:
            return ToolResult.success("No relevant passage found in the knowledge base.", hits=0)
        text = "\n\n".join(f"[{i}] source: {h.get('source') or 'knowledge base'}\n{h['content']}"
                           for i, h in enumerate(hits, 1))
        return ToolResult.success(text, hits=len(hits),
                                  sources=sorted({str(h.get("source") or "") for h in hits}))


def knowledge_tools() -> list[Tool]:
    return [KnowledgeTool()]
