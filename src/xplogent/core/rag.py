"""RAG over your documents — ingest files/folders, chunk, embed, and hybrid-search.

Text/markdown/code are read natively; PDFs use the optional ``pypdf`` package.
Search fuses semantic similarity (embeddings) with bm25 full-text (FTS5) via
reciprocal-rank fusion, so the agent can answer from the user's own documents and
cite sources. Reuses the same SQLite ``Store`` and ``Embedder`` as memory.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from xplogent.core.logging import get_logger
from xplogent.memory.vector import Embedder, cosine

_log = get_logger("rag")

# Read these as text; everything else is skipped unless it's a PDF.
_TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx",
             ".json", ".yaml", ".yml", ".toml", ".html", ".css", ".sh", ".java", ".go",
             ".rs", ".c", ".cpp", ".h", ".rb", ".php", ".sql", ".csv", ".log", ".tex"}


def read_text(path: Path) -> str:
    """Extract text from a file (native for text/code, pypdf for PDF)."""
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("PDF support needs pypdf: pip install 'xplogent[rag]'") from None
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    return path.read_text(encoding="utf-8", errors="replace")


def chunk_text(text: str, size: int = 3200, overlap: int = 400) -> list[str]:
    """Split text into ~``size``-char chunks (≈800 tokens) on paragraph boundaries."""
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 > size and buf:
            chunks.append(buf.strip())
            buf = buf[-overlap:] + "\n\n" + p if overlap else p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf.strip():
        chunks.append(buf.strip())
    # Hard-split any oversized chunk.
    out: list[str] = []
    for c in chunks:
        if len(c) <= size * 1.5:
            out.append(c)
        else:
            out.extend(c[i:i + size] for i in range(0, len(c), size))
    return out or ([text.strip()] if text.strip() else [])


# Never ingest credentials into the searchable knowledge base (they could then be read
# back through search/export, or quoted to a caller by the phone agent).
_SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store"}
# Also skipped when walking a folder (noise, not secrets).
_SENSITIVE_DIRS = _SECRET_DIRS | {".git", "node_modules", ".venv", "venv", "__pycache__"}
_SENSITIVE_NAMES = {".env", ".netrc", ".pgpass", ".npmrc", ".pypirc", "secrets.enc",
                    "secret.key", "credentials", "credentials.json", "id_rsa", "id_dsa",
                    "id_ecdsa", "id_ed25519", "known_hosts", "authorized_keys"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".jks"}


def is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    if name in _SENSITIVE_NAMES or name.startswith((".env.", "id_rsa", "id_ed25519")):
        return True
    if path.suffix.lower() in _SENSITIVE_SUFFIXES:
        return True
    return any(part.lower() in _SENSITIVE_DIRS for part in path.parts[:-1])


def _iter_files(path: Path) -> list[Path]:
    if path.is_file():
        return [] if is_sensitive(path) else [path]
    files: list[Path] = []
    for p in path.rglob("*"):
        if p.is_file() and (p.suffix.lower() in _TEXT_EXT or p.suffix.lower() == ".pdf") \
                and not is_sensitive(p.relative_to(path)):
            files.append(p)
            if len(files) >= 500:  # safety bound
                break
    return files


async def ingest_path(store, embedder: Embedder, path: str) -> dict[str, Any]:
    """Ingest a file or folder into the document store. Returns a summary."""
    root = Path(path).expanduser()
    if not root.exists():
        return {"ok": False, "error": f"no such path: {root}"}
    if (root.is_file() and is_sensitive(root)) or any(
            part.lower() in _SECRET_DIRS for part in root.resolve().parts):
        return {"ok": False, "error": f"refusing to ingest credentials/secret files: {root}"}
    ingested, skipped, chunks_total = [], 0, 0
    files = await asyncio.to_thread(_iter_files, root)   # big folders: don't block the loop
    for fp in files:
        try:
            text = await asyncio.to_thread(read_text, fp)
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't stop the rest
            _log.warning("skip %s: %s", fp, exc)
            skipped += 1
            continue
        if not text.strip():
            skipped += 1
            continue
        h = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if store.doc_exists(h):
            skipped += 1
            continue
        doc_id = store.add_document(str(fp), fp.name, h)
        pieces = chunk_text(text)
        vecs = await embedder.embed(pieces)
        for i, (piece, vec) in enumerate(zip(pieces, vecs, strict=False)):
            store.add_chunk(doc_id, i, piece, vec)
        ingested.append(fp.name)
        chunks_total += len(pieces)
    return {"ok": True, "ingested": ingested, "skipped": skipped, "chunks": chunks_total}


async def ingest_text(store, embedder: Embedder, content: str, title: str = "pasted") -> dict[str, Any]:
    h = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
    if store.doc_exists(h):
        return {"ok": True, "ingested": [], "skipped": 1, "chunks": 0}
    doc_id = store.add_document(title, title, h)
    pieces = chunk_text(content)
    vecs = await embedder.embed(pieces)
    for i, (piece, vec) in enumerate(zip(pieces, vecs, strict=False)):
        store.add_chunk(doc_id, i, piece, vec)
    return {"ok": True, "ingested": [title], "skipped": 0, "chunks": len(pieces)}


async def hybrid_search(store, embedder: Embedder, query: str, k: int = 5) -> list[dict[str, Any]]:
    """Fuse semantic + bm25 results with reciprocal-rank fusion; returns cited chunks."""
    fts = store.search_chunks_fts(query, limit=20)
    fts_rank = {row["id"]: i for i, row in enumerate(fts)}

    sem_rank: dict[int, int] = {}
    chunks = store.all_chunks()
    qvec = await embedder.embed_one(query)
    if qvec and chunks:
        scored = [(c, cosine(qvec, c["embedding"])) for c in chunks if c["embedding"]]
        scored.sort(key=lambda t: t[1], reverse=True)
        for i, (c, _s) in enumerate(scored[:20]):
            sem_rank[c["id"]] = i

    by_id = {c["id"]: c for c in chunks}
    for row in fts:
        by_id.setdefault(row["id"], {"id": row["id"], "doc_id": row["doc_id"], "content": row["content"]})

    fused: dict[int, float] = {}
    for cid in set(fts_rank) | set(sem_rank):
        score = 0.0
        if cid in fts_rank:
            score += 1.0 / (60 + fts_rank[cid])
        if cid in sem_rank:
            score += 1.0 / (60 + sem_rank[cid])
        fused[cid] = score
    top = sorted(fused, key=lambda c: fused[c], reverse=True)[:k]
    out = []
    for cid in top:
        c = by_id.get(cid)
        if not c:
            continue
        out.append({"content": c["content"], "source": store.doc_title(c["doc_id"]),
                    "doc_id": c["doc_id"], "score": round(fused[cid], 4)})
    return out
