"""Three-tier memory manager.

* **short-term** — the rolling conversation (managed by the agent loop / context).
* **long-term** — durable facts & preferences, recalled semantically.
* **episodic** — searchable history of past sessions and messages.

Also stores and retrieves **skills** (used by the self-improvement subsystem).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from xplogent.memory.store import SkillRow, Store
from xplogent.memory.vector import Embedder, cosine, top_k


def _keyword_rank(query: str, texts: list[str]) -> list[int]:
    """Indices of ``texts`` ranked by how many distinct query words they
    contain. The old fallback tested whether the WHOLE query string appeared
    verbatim inside a fact/skill, which almost never happens with natural
    questions — semantic-off recall silently returned nothing."""
    tokens = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    if not tokens:
        return []
    scored = [(sum(1 for t in tokens if t in text.lower()), i)
              for i, text in enumerate(texts)]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [i for _hits, i in scored]


@dataclass
class RetrievedSkill:
    name: str
    description: str
    body: str
    score: float
    trigger: str = ""
    tools: list[str] = field(default_factory=list)


class MemoryManager:
    def __init__(self, store: Store, embedder: Embedder, session_id: int | None = None) -> None:
        self.store = store
        self.embedder = embedder
        self.session_id = session_id
        prov = getattr(embedder, "provider", None)
        self.embed_model = f"{getattr(prov, 'name', '')}:{getattr(prov, 'model', '')}" if prov else ""

    # -- episodic logging ------------------------------------------------------
    def log(self, role: str, content: str) -> None:
        """Record a message in the current session (episodic memory)."""
        if self.session_id is not None and content:
            self.store.add_message(self.session_id, role, content)
            # Name the session after its first user message so the sidebar reads well.
            if role == "user":
                self.store.set_session_title(self.session_id, content[:60])

    # -- long-term facts -------------------------------------------------------
    async def remember(self, content: str, source: str = "") -> int:
        vec = await self.embedder.embed_one(content)
        # Skip near-duplicate facts to curb memory bloat.
        if vec:
            for f in self.store.all_facts():
                if f.embedding and cosine(vec, f.embedding) > 0.95:
                    return f.id
        return self.store.add_fact(content, vec, source, embed_model=self.embed_model)

    def record_skill_outcome(self, name: str, success: bool) -> None:
        self.store.record_skill_outcome(name, success)

    def _embed_match(self, em: str) -> bool:
        """A stored vector is comparable only if it came from the same embed model."""
        return not em or not self.embed_model or em == self.embed_model

    async def _query_vec(self, query: str) -> list[float]:
        """Embed a query, reusing the last result: the agent recalls facts and skills
        for the same task on every step, which used to cost two embeddings per step."""
        cached = getattr(self, "_qcache", None)
        if cached and cached[0] == query:
            return cached[1]
        vec = await self.embedder.embed_one(query)
        if vec:
            self._qcache = (query, vec)
        return vec

    async def recall(self, query: str, k: int = 5) -> list[str]:
        facts = self.store.all_facts()
        if not facts:
            return []
        qvec = await self._query_vec(query)
        if qvec:
            usable = [(f, f.embedding) for f in facts
                      if f.embedding and self._embed_match(f.embed_model)]
            ranked = top_k(qvec, usable, k)
            return [f.content for f, score in ranked if score > 0.2]  # type: ignore[attr-defined]
        # keyword fallback (embeddings unavailable)
        return [facts[i].content
                for i in _keyword_rank(query, [f.content for f in facts])[:k]]

    # -- episodic --------------------------------------------------------------
    def search_history(self, query: str, limit: int = 10) -> list[dict]:
        return self.store.search_messages(query, limit)

    # -- skills ----------------------------------------------------------------
    async def save_skill(self, name: str, description: str, body: str,
                         tools: list[str] | None = None, trigger: str = "",
                         source: str = "learned") -> None:
        vec = await self.embedder.embed_one(f"{name}: {description} {trigger}")
        self.store.upsert_skill(name, description, body, vec, embed_model=self.embed_model,
                                tools=tools, trigger=trigger, source=source)

    async def relevant_skills(self, query: str, k: int = 3,
                              counted: set[str] | None = None) -> list[RetrievedSkill]:
        """Top-k skills for ``query``. A use is recorded once per skill: names in
        ``counted`` (already retrieved earlier in the same run) aren't counted again."""
        skills: list[SkillRow] = self.store.all_skills()
        if not skills:
            return []
        qvec = await self._query_vec(query)
        if qvec:
            usable = [(s, s.embedding) for s in skills
                      if s.embedding and self._embed_match(s.embed_model)]
            ranked = top_k(qvec, usable, k + 2)
            # Nudge proficient/expert skills up so battle-tested ones win ties.
            ranked.sort(key=lambda t: t[1] + 0.03 * t[0].stars, reverse=True)  # type: ignore[attr-defined]
            picked = [(s, score) for s, score in ranked[:k] if score > 0.25]
        else:
            idx = _keyword_rank(query, [f"{s.name} {s.description} {s.trigger}"
                                        for s in skills])
            picked = [(skills[i], 1.0) for i in idx[:k]]
        out = []
        for s, score in picked:
            if counted is None or s.name not in counted:  # type: ignore[attr-defined]
                self.store.increment_skill_use(s.name)  # type: ignore[attr-defined]
            out.append(RetrievedSkill(s.name, s.description, s.body, score,  # type: ignore[attr-defined]
                                      trigger=s.trigger, tools=s.tools))
        return out

    def list_skills(self) -> list[SkillRow]:
        return self.store.all_skills()
