"""Embeddings + cosine similarity for semantic recall.

The embedder wraps any provider that implements ``embed`` (Ollama's
``nomic-embed-text`` by default, fully local). If embeddings are unavailable
(e.g. the model isn't pulled), callers fall back to keyword search.
"""

from __future__ import annotations

import math
import time

from xplogent.providers.base import Provider


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def top_k(query_vec: list[float], items: list[tuple[object, list[float]]],
          k: int = 5) -> list[tuple[object, float]]:
    """Rank ``(item, embedding)`` pairs by cosine similarity to ``query_vec``."""
    scored = [(item, cosine(query_vec, emb)) for item, emb in items if emb]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]


class Embedder:
    """Best-effort embeddings.

    A provider without embedding support is switched off for good. Other errors
    (Ollama still loading, a network blip) only switch embeddings off after
    ``FAIL_LIMIT`` consecutive failures, and even then they are retried after
    ``RETRY_AFTER`` seconds instead of staying off for the life of the process.
    """

    FAIL_LIMIT = 3
    RETRY_AFTER = 300.0

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.available = True
        self._fails = 0
        self._retry_at: float | None = None  # None while available or permanently off

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.available:
            if self._retry_at is None or time.monotonic() < self._retry_at:
                return [[] for _ in texts]
            self.available = True  # cool-down over: try again
            self._fails = 0  # fresh budget: one blip must not re-disable at once
        try:
            out = await self.provider.embed(texts)
        except NotImplementedError:
            self.available, self._retry_at = False, None
            return [[] for _ in texts]
        except Exception:  # noqa: BLE001 - embeddings are best-effort
            self._fails += 1
            if self._fails >= self.FAIL_LIMIT:
                self.available = False
                self._retry_at = time.monotonic() + self.RETRY_AFTER
            return [[] for _ in texts]
        self._fails, self._retry_at = 0, None
        return out

    async def embed_one(self, text: str) -> list[float]:
        out = await self.embed([text])
        return out[0] if out else []
