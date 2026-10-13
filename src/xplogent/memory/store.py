"""SQLite persistence for sessions, messages, facts, skills, and run traces.

Embeddings are stored as JSON arrays; similarity search is done in Python
(cosine) which is plenty fast at personal scale and keeps the dependency
footprint to the standard library.

The connection is opened with ``check_same_thread=False`` and WAL mode, and all
mutations are guarded by a lock, so multiple concurrently-running agents (and
the API threadpool) can share one store safely.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    summary TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    role TEXT,
    content TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT,
    embedding TEXT,
    source TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    description TEXT,
    body TEXT,
    embedding TEXT,
    uses INTEGER DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    goal TEXT,
    mode TEXT,
    status TEXT,
    started_at REAL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    agent_id TEXT,
    type TEXT,
    data TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    sender TEXT,
    recipient TEXT,
    content TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    prompt TEXT,
    mode TEXT,
    spec TEXT,
    tz TEXT,
    enabled INTEGER DEFAULT 1,
    next_run REAL,
    last_run REAL,
    last_status TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    title TEXT,
    hash TEXT,
    chunks INTEGER DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS doc_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER,
    ord INTEGER,
    content TEXT,
    embedding TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost REAL,
    session_id INTEGER,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS evals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    description TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS eval_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER,
    prompt TEXT,
    criteria TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER,
    model TEXT,
    passed INTEGER,
    total INTEGER,
    score REAL,
    detail TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT,
    action TEXT,
    target TEXT,
    risk TEXT,
    allowed INTEGER,
    detail TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS kg_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    type TEXT,
    mentions INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS kg_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT,
    relation TEXT,
    object TEXT,
    source TEXT,
    created_at REAL,
    UNIQUE(subject, relation, object)
);
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    graph TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS skill_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    action TEXT,
    level TEXT,
    stars INTEGER,
    detail TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    type TEXT,
    spec TEXT,
    prompt TEXT,
    mode TEXT,
    enabled INTEGER DEFAULT 1,
    last_fired REAL,
    last_status TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    phone TEXT,
    email TEXT,
    timezone TEXT,
    status TEXT DEFAULT 'new',
    notes TEXT,
    fields TEXT,
    source TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT,
    contact_id INTEGER,
    from_number TEXT,
    to_number TEXT,
    provider_call_id TEXT,
    status TEXT DEFAULT 'active',
    outcome TEXT,
    summary TEXT,
    cost REAL DEFAULT 0,
    started_at REAL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS call_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER,
    speaker TEXT,
    text TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_call_turns_call ON call_turns(call_id);
CREATE TABLE IF NOT EXISTS dnc (
    phone TEXT PRIMARY KEY,
    reason TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_msgs_run ON agent_messages(run_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON doc_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_cases_eval ON eval_cases(eval_id);
CREATE INDEX IF NOT EXISTS idx_evalruns_eval ON eval_runs(eval_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(created_at);
CREATE INDEX IF NOT EXISTS idx_kg_edge_subj ON kg_edges(subject);
CREATE INDEX IF NOT EXISTS idx_kg_edge_obj ON kg_edges(object);
"""


@dataclass
class Fact:
    id: int
    content: str
    embedding: list[float]
    source: str
    created_at: float
    embed_model: str = ""


def skill_level(uses: int, successes: int, failures: int) -> tuple[str, int]:
    """Map usage + outcomes to a (label, stars 1-3) proficiency level."""
    total = successes + failures
    rate = (successes / total) if total else 1.0
    if uses >= 8 and rate >= 0.7:
        return "expert", 3
    if uses >= 3 and rate >= 0.4:
        return "proficient", 2
    return "novice", 1


@dataclass
class SkillRow:
    id: int
    name: str
    description: str
    body: str
    embedding: list[float]
    uses: int
    successes: int = 0
    failures: int = 0
    last_used: float | None = None
    embed_model: str = ""
    tools: list[str] = field(default_factory=list)
    trigger: str = ""
    source: str = ""

    @property
    def level(self) -> str:
        return skill_level(self.uses, self.successes, self.failures)[0]

    @property
    def stars(self) -> int:
        return skill_level(self.uses, self.successes, self.failures)[1]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._fts = False
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
            self._migrate()
            self._init_fts()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema (idempotent)."""
        def cols(table: str) -> set[str]:
            return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

        adds = [
            ("facts", "embed_model", "TEXT DEFAULT ''"),
            ("skills", "embed_model", "TEXT DEFAULT ''"),
            ("skills", "successes", "INTEGER DEFAULT 0"),
            ("skills", "failures", "INTEGER DEFAULT 0"),
            ("skills", "last_used", "REAL"),
            ("skills", "tools", "TEXT DEFAULT ''"),     # JSON list of required tools
            ("skills", "trigger", "TEXT DEFAULT ''"),   # when to use this skill
            ("skills", "source", "TEXT DEFAULT ''"),    # learned | installed | pack id
        ]
        for table, col, ddl in adds:
            if col not in cols(table):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self.conn.commit()

    def _init_fts(self) -> None:
        """Best-effort full-text index over message + document content (FTS5)."""
        try:
            self.conn.executescript(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                "content, session_id UNINDEXED, role UNINDEXED);"
                "CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5("
                "content, doc_id UNINDEXED);"
            )
            self.conn.commit()
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
            return
        # Backfill once if the index is empty but messages already exist.
        have = self.conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        if not have and self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]:
            self.conn.execute(
                "INSERT INTO messages_fts (rowid, content, session_id, role) "
                "SELECT id, content, session_id, role FROM messages"
            )
            self.conn.commit()

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # -- sessions --------------------------------------------------------------
    def create_session(self, title: str = "") -> int:
        cur = self._write(
            "INSERT INTO sessions (title, summary, created_at) VALUES (?,?,?)",
            (title, "", time.time()),
        )
        return int(cur.lastrowid)

    def set_session_summary(self, session_id: int, summary: str) -> None:
        self._write("UPDATE sessions SET summary=? WHERE id=?", (summary, session_id))

    def set_session_title(self, session_id: int, title: str) -> None:
        """Set the title only if it's still the default, so it reads as the first message."""
        self._write(
            "UPDATE sessions SET title=? WHERE id=? "
            "AND (title IS NULL OR title='' OR title='chat')",
            (title, session_id),
        )

    def rename_session(self, session_id: int, title: str) -> None:
        """Force-set a session title (user rename)."""
        self._write("UPDATE sessions SET title=? WHERE id=?", (title, session_id))

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count "
            "FROM sessions s ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    def delete_session(self, session_id: int) -> None:
        self._write("DELETE FROM messages WHERE session_id=?", (session_id,))
        self._write("DELETE FROM sessions WHERE id=?", (session_id,))
        if self._fts:
            self._write("DELETE FROM messages_fts WHERE session_id=?", (session_id,))

    # -- messages --------------------------------------------------------------
    def add_message(self, session_id: int, role: str, content: str) -> None:
        cur = self._write(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, role, content, time.time()),
        )
        if self._fts:
            self._write(
                "INSERT INTO messages_fts (rowid, content, session_id, role) VALUES (?,?,?,?)",
                (int(cur.lastrowid), content, session_id, role),
            )

    def session_messages(self, session_id: int) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id",
            (session_id,),
        )
        return [dict(r) for r in rows]

    def delete_last_turns(self, session_id: int, n: int = 1) -> int:
        """Remove the last ``n`` user→assistant exchanges from a session.

        A turn starts at a user message; everything from the n-th-from-last user
        message onward (its assistant reply and anything after) is deleted, along
        with the matching FTS rows. Returns the number of messages removed.
        """
        n = max(1, n)
        user_ids = [r["id"] for r in self._query(
            "SELECT id FROM messages WHERE session_id=? AND role='user' ORDER BY id",
            (session_id,))]
        if not user_ids:
            return 0
        cutoff = user_ids[-n] if n <= len(user_ids) else user_ids[0]
        ids = [r["id"] for r in self._query(
            "SELECT id FROM messages WHERE session_id=? AND id>=?", (session_id, cutoff))]
        self._write("DELETE FROM messages WHERE session_id=? AND id>=?", (session_id, cutoff))
        if self._fts:
            for mid in ids:
                self._write("DELETE FROM messages_fts WHERE rowid=?", (mid,))
        return len(ids)

    def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Full-text ranked search (FTS5) with a LIKE fallback when unavailable."""
        if self._fts and query.strip():
            # Require each term (quoted, AND-ed); bm25 ranks best matches first (lower=better).
            terms = [t for t in query.replace('"', " ").split() if t]
            match = " ".join(f'"{t}"' for t in terms)
            if not match:
                match = '"' + query.strip() + '"'
            try:
                rows = self._query(
                    "SELECT session_id, role, content FROM messages_fts "
                    "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?",
                    (match, limit),
                )
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass  # malformed query → fall back to LIKE
        rows = self._query(
            "SELECT session_id, role, content FROM messages WHERE content LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        return [dict(r) for r in rows]

    # -- facts -----------------------------------------------------------------
    def add_fact(self, content: str, embedding: list[float], source: str = "",
                 embed_model: str = "") -> int:
        cur = self._write(
            "INSERT INTO facts (content, embedding, source, created_at, embed_model) "
            "VALUES (?,?,?,?,?)",
            (content, json.dumps(embedding), source, time.time(), embed_model),
        )
        return int(cur.lastrowid)

    def all_facts(self) -> list[Fact]:
        rows = self._query("SELECT * FROM facts")
        return [
            Fact(r["id"], r["content"], json.loads(r["embedding"] or "[]"),
                 r["source"], r["created_at"], r["embed_model"] if "embed_model" in r.keys() else "")
            for r in rows
        ]

    def delete_fact(self, fact_id: int) -> None:
        self._write("DELETE FROM facts WHERE id=?", (fact_id,))

    # -- skills ----------------------------------------------------------------
    def upsert_skill(self, name: str, description: str, body: str,
                     embedding: list[float], embed_model: str = "",
                     tools: list[str] | None = None, trigger: str = "",
                     source: str = "") -> None:
        self._write(
            "INSERT INTO skills (name, description, body, embedding, uses, created_at, "
            "embed_model, tools, trigger, source) VALUES (?,?,?,?,0,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
            "body=excluded.body, embedding=excluded.embedding, embed_model=excluded.embed_model, "
            "tools=excluded.tools, trigger=excluded.trigger, source=excluded.source",
            (name, description, body, json.dumps(embedding), time.time(), embed_model,
             json.dumps(tools or []), trigger, source),
        )

    def all_skills(self) -> list[SkillRow]:
        out = []
        for row in self._query("SELECT * FROM skills"):
            r = dict(row)
            out.append(SkillRow(
                r["id"], r["name"], r["description"], r["body"],
                json.loads(r.get("embedding") or "[]"), r["uses"],
                successes=r.get("successes", 0) or 0, failures=r.get("failures", 0) or 0,
                last_used=r.get("last_used"), embed_model=r.get("embed_model") or "",
                tools=json.loads(r.get("tools") or "[]"),
                trigger=r.get("trigger") or "", source=r.get("source") or "",
            ))
        return out

    def increment_skill_use(self, name: str) -> None:
        self._write("UPDATE skills SET uses = uses + 1 WHERE name=?", (name,))

    def record_skill_outcome(self, name: str, success: bool) -> None:
        col = "successes" if success else "failures"
        self._write(
            f"UPDATE skills SET {col} = {col} + 1, last_used=? WHERE name=?",
            (time.time(), name),
        )

    def delete_skill(self, name: str) -> None:
        self._write("DELETE FROM skills WHERE name=?", (name,))

    def skill_exists(self, name: str) -> bool:
        return bool(self._query("SELECT 1 FROM skills WHERE name=?", (name,)))

    # -- skill activity history ------------------------------------------------
    def add_skill_event(self, name: str, action: str, level: str = "",
                        stars: int = 0, detail: str = "") -> None:
        self._write(
            "INSERT INTO skill_events (name, action, level, stars, detail, created_at) "
            "VALUES (?,?,?,?,?,?)", (name, action, level, stars, detail, time.time()))

    def skill_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT name, action, level, stars, detail, created_at FROM skill_events "
            "ORDER BY id DESC LIMIT ?", (limit,))]

    # -- runs / events / agent messages (monitoring) ---------------------------
    def create_run(self, run_id: str, goal: str, mode: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO runs (id, goal, mode, status, started_at, ended_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, goal, mode, "running", time.time(), None),
        )

    def finish_run(self, run_id: str, status: str = "done") -> None:
        self._write(
            "UPDATE runs SET status=?, ended_at=? WHERE id=?",
            (status, time.time(), run_id),
        )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        return dict(rows[0]) if rows else None

    def add_event(self, run_id: str, agent_id: str, type_: str, data: dict[str, Any]) -> None:
        self._write(
            "INSERT INTO events (run_id, agent_id, type, data, created_at) VALUES (?,?,?,?,?)",
            (run_id, agent_id, type_, json.dumps(data, default=str), time.time()),
        )

    def run_events(self, run_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT agent_id, type, data, created_at FROM events WHERE run_id=? "
            "ORDER BY id LIMIT ?",
            (run_id, limit),
        )
        out = []
        for r in rows:
            out.append({
                "agent_id": r["agent_id"], "type": r["type"],
                "data": json.loads(r["data"] or "{}"), "created_at": r["created_at"],
            })
        return out

    def add_agent_message(self, run_id: str, sender: str, recipient: str | None,
                          content: str) -> None:
        self._write(
            "INSERT INTO agent_messages (run_id, sender, recipient, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, sender, recipient, content, time.time()),
        )

    def list_agent_messages(self, run_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if run_id:
            rows = self._query(
                "SELECT * FROM agent_messages WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM agent_messages ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in rows]

    # -- schedules (recurring / timed agent jobs) ------------------------------
    def add_schedule(self, name: str, prompt: str, mode: str, spec: str,
                     tz: str, next_run: float) -> int:
        cur = self._write(
            "INSERT INTO schedules (name, prompt, mode, spec, tz, enabled, "
            "next_run, last_run, last_status, created_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
            (name, prompt, mode, spec, tz, next_run, None, None, time.time()),
        )
        return int(cur.lastrowid)

    def list_schedules(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT * FROM schedules ORDER BY id DESC")]

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM schedules WHERE id=?", (schedule_id,))
        return dict(rows[0]) if rows else None

    def due_schedules(self, now: float) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL "
            "AND next_run<=? ORDER BY next_run", (now,))]

    def set_schedule_run(self, schedule_id: int, last_run: float | None,
                         last_status: str, next_run: float | None) -> None:
        self._write(
            "UPDATE schedules SET last_run=?, last_status=?, next_run=? WHERE id=?",
            (last_run, last_status, next_run, schedule_id),
        )

    def set_schedule_next(self, schedule_id: int, next_run: float | None) -> None:
        self._write("UPDATE schedules SET next_run=? WHERE id=?", (next_run, schedule_id))

    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> None:
        self._write("UPDATE schedules SET enabled=? WHERE id=?",
                    (1 if enabled else 0, schedule_id))

    def delete_schedule(self, schedule_id: int) -> None:
        self._write("DELETE FROM schedules WHERE id=?", (schedule_id,))

    # -- documents (RAG) -------------------------------------------------------
    def doc_exists(self, content_hash: str) -> int | None:
        rows = self._query("SELECT id FROM documents WHERE hash=?", (content_hash,))
        return int(rows[0]["id"]) if rows else None

    def add_document(self, source: str, title: str, content_hash: str) -> int:
        cur = self._write(
            "INSERT INTO documents (source, title, hash, chunks, created_at) VALUES (?,?,?,0,?)",
            (source, title, content_hash, time.time()),
        )
        return int(cur.lastrowid)

    def add_chunk(self, doc_id: int, ord_: int, content: str, embedding: list[float]) -> None:
        cur = self._write(
            "INSERT INTO doc_chunks (doc_id, ord, content, embedding, created_at) VALUES (?,?,?,?,?)",
            (doc_id, ord_, content, json.dumps(embedding), time.time()),
        )
        if self._fts:
            self._write("INSERT INTO doc_chunks_fts (rowid, content, doc_id) VALUES (?,?,?)",
                        (int(cur.lastrowid), content, doc_id))
        self._write("UPDATE documents SET chunks = chunks + 1 WHERE id=?", (doc_id,))

    def list_documents(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT id, source, title, chunks, created_at FROM documents ORDER BY id DESC")]

    def all_chunks(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT id, doc_id, content, embedding FROM doc_chunks")
        return [{"id": r["id"], "doc_id": r["doc_id"], "content": r["content"],
                 "embedding": json.loads(r["embedding"] or "[]")} for r in rows]

    def chunks_missing_embeddings(self, limit: int = 64) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT id, content FROM doc_chunks WHERE embedding IS NULL OR embedding IN ('', '[]') "
            "ORDER BY id LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def set_chunk_embedding(self, chunk_id: int, embedding: list[float]) -> None:
        self._write("UPDATE doc_chunks SET embedding=? WHERE id=?",
                    (json.dumps(embedding), chunk_id))

    def search_chunks_fts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        if not (self._fts and query.strip()):
            return []
        terms = [t for t in query.replace('"', " ").split() if t]
        match = " ".join(f'"{t}"' for t in terms) or f'"{query.strip()}"'
        try:
            rows = self._query(
                "SELECT rowid AS id, doc_id, content FROM doc_chunks_fts "
                "WHERE doc_chunks_fts MATCH ? ORDER BY bm25(doc_chunks_fts) LIMIT ?",
                (match, limit))
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def doc_title(self, doc_id: int) -> str:
        rows = self._query("SELECT source, title FROM documents WHERE id=?", (doc_id,))
        if not rows:
            return ""
        return rows[0]["title"] or rows[0]["source"] or f"doc {doc_id}"

    def delete_document(self, doc_id: int) -> None:
        self._write("DELETE FROM doc_chunks WHERE doc_id=?", (doc_id,))
        self._write("DELETE FROM documents WHERE id=?", (doc_id,))
        if self._fts:
            self._write("DELETE FROM doc_chunks_fts WHERE doc_id=?", (doc_id,))

    # -- usage accounting (analytics) ------------------------------------------
    def add_usage(self, model: str, input_tokens: int, output_tokens: int,
                  cost: float, session_id: int | None) -> None:
        self._write(
            "INSERT INTO usage (model, input_tokens, output_tokens, cost, session_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (model, input_tokens, output_tokens, cost, session_id, time.time()),
        )

    def usage_rows(self, since: float = 0.0) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT model, input_tokens, output_tokens, cost, created_at FROM usage "
            "WHERE created_at>=? ORDER BY created_at", (since,))]

    # -- evals -----------------------------------------------------------------
    def upsert_eval(self, name: str, description: str = "") -> int:
        row = self._query("SELECT id FROM evals WHERE name=?", (name,))
        if row:
            self._write("UPDATE evals SET description=? WHERE id=?",
                        (description, row[0]["id"]))
            return int(row[0]["id"])
        cur = self._write(
            "INSERT INTO evals (name, description, created_at) VALUES (?,?,?)",
            (name, description, time.time()))
        return int(cur.lastrowid)

    def add_eval_case(self, eval_id: int, prompt: str, criteria: str) -> int:
        cur = self._write(
            "INSERT INTO eval_cases (eval_id, prompt, criteria, created_at) VALUES (?,?,?,?)",
            (eval_id, prompt, criteria, time.time()))
        return int(cur.lastrowid)

    def set_eval_cases(self, eval_id: int, cases: list[dict[str, str]]) -> None:
        """Replace a suite's cases wholesale (used when saving from the GUI)."""
        self._write("DELETE FROM eval_cases WHERE eval_id=?", (eval_id,))
        for c in cases:
            self.add_eval_case(eval_id, c.get("prompt", ""), c.get("criteria", ""))

    def list_evals(self) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT id, name, description, created_at FROM evals ORDER BY id DESC"):
            d = dict(r)
            d["cases"] = [dict(c) for c in self._query(
                "SELECT id, prompt, criteria FROM eval_cases WHERE eval_id=? ORDER BY id",
                (d["id"],))]
            d["runs"] = [dict(rr) for rr in self._query(
                "SELECT passed, total, score, model, created_at FROM eval_runs "
                "WHERE eval_id=? ORDER BY id DESC LIMIT 20", (d["id"],))]
            out.append(d)
        return out

    def eval_cases(self, eval_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT id, prompt, criteria FROM eval_cases WHERE eval_id=? ORDER BY id",
            (eval_id,))]

    def add_eval_run(self, eval_id: int, model: str, passed: int, total: int,
                     score: float, detail: str) -> int:
        cur = self._write(
            "INSERT INTO eval_runs (eval_id, model, passed, total, score, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (eval_id, model, passed, total, score, detail, time.time()))
        return int(cur.lastrowid)

    def delete_eval(self, eval_id: int) -> None:
        self._write("DELETE FROM eval_cases WHERE eval_id=?", (eval_id,))
        self._write("DELETE FROM eval_runs WHERE eval_id=?", (eval_id,))
        self._write("DELETE FROM evals WHERE id=?", (eval_id,))

    # -- audit log -------------------------------------------------------------
    def add_audit(self, actor: str, action: str, target: str = "", risk: str = "",
                  allowed: bool | None = None, detail: str = "") -> None:
        self._write(
            "INSERT INTO audit (actor, action, target, risk, allowed, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (actor, action, target, risk,
             None if allowed is None else int(allowed), detail, time.time()))

    def audit_rows(self, limit: int = 200, action: str = "", risk: str = "") -> list[dict[str, Any]]:
        sql = "SELECT actor, action, target, risk, allowed, detail, created_at FROM audit"
        clauses, params = [], []
        if action:
            clauses.append("action=?")
            params.append(action)
        if risk:
            clauses.append("risk=?")
            params.append(risk)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._query(sql, tuple(params))]

    # -- knowledge graph -------------------------------------------------------
    def upsert_node(self, name: str, type_: str = "") -> None:
        existing = self._query("SELECT id, type FROM kg_nodes WHERE name=?", (name,))
        if existing:
            self._write("UPDATE kg_nodes SET mentions = mentions + 1, "
                        "type = COALESCE(NULLIF(type,''), ?) WHERE name=?", (type_, name))
        else:
            self._write("INSERT INTO kg_nodes (name, type, mentions, created_at) VALUES (?,?,1,?)",
                        (name, type_, time.time()))

    def add_edge(self, subject: str, relation: str, object_: str, source: str = "") -> None:
        self.upsert_node(subject)
        self.upsert_node(object_)
        self._write(
            "INSERT OR IGNORE INTO kg_edges (subject, relation, object, source, created_at) "
            "VALUES (?,?,?,?,?)", (subject, relation, object_, source, time.time()))

    def graph_snapshot(self, limit: int = 300) -> dict[str, list[dict[str, Any]]]:
        nodes = [dict(r) for r in self._query(
            "SELECT name, type, mentions FROM kg_nodes ORDER BY mentions DESC LIMIT ?", (limit,))]
        keep = {n["name"] for n in nodes}
        edges = []
        for r in self._query("SELECT subject, relation, object FROM kg_edges ORDER BY id DESC LIMIT ?",
                             (limit * 2,)):
            d = dict(r)
            if d["subject"] in keep and d["object"] in keep:
                edges.append(d)
        return {"nodes": nodes, "edges": edges}

    def neighbors(self, entity: str, limit: int = 12) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query(
            "SELECT subject, relation, object FROM kg_edges "
            "WHERE subject=? OR object=? ORDER BY id DESC LIMIT ?", (entity, entity, limit))]

    def kg_node_names(self) -> list[str]:
        return [r["name"] for r in self._query("SELECT name FROM kg_nodes")]

    # -- workflows -------------------------------------------------------------
    def save_workflow(self, name: str, graph: dict, workflow_id: int | None = None) -> int:
        now = time.time()
        blob = json.dumps(graph)
        if workflow_id:
            self._write("UPDATE workflows SET name=?, graph=?, updated_at=? WHERE id=?",
                        (name, blob, now, workflow_id))
            return workflow_id
        cur = self._write(
            "INSERT INTO workflows (name, graph, created_at, updated_at) VALUES (?,?,?,?)",
            (name, blob, now, now))
        return int(cur.lastrowid)

    def list_workflows(self) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT id, name, graph, updated_at FROM workflows ORDER BY id DESC"):
            d = dict(r)
            d["graph"] = json.loads(d["graph"] or "{}")
            out.append(d)
        return out

    def get_workflow(self, workflow_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT id, name, graph, updated_at FROM workflows WHERE id=?",
                           (workflow_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["graph"] = json.loads(d["graph"] or "{}")
        return d

    def delete_workflow(self, workflow_id: int) -> None:
        self._write("DELETE FROM workflows WHERE id=?", (workflow_id,))

    # -- proactive triggers ----------------------------------------------------
    def add_trigger(self, name: str, type_: str, spec: str, prompt: str,
                    mode: str = "agent") -> int:
        cur = self._write(
            "INSERT INTO triggers (name, type, spec, prompt, mode, enabled, created_at) "
            "VALUES (?,?,?,?,?,1,?)", (name, type_, spec, prompt, mode, time.time()))
        return int(cur.lastrowid)

    def list_triggers(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM triggers ORDER BY id DESC")]

    def get_trigger(self, trigger_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM triggers WHERE id=?", (trigger_id,))
        return dict(rows[0]) if rows else None

    def get_trigger_by_token(self, token: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM triggers WHERE type='webhook' AND spec=?", (token,))
        return dict(rows[0]) if rows else None

    def set_trigger_fired(self, trigger_id: int, status: str) -> None:
        self._write("UPDATE triggers SET last_fired=?, last_status=? WHERE id=?",
                    (time.time(), status, trigger_id))

    def toggle_trigger(self, trigger_id: int) -> None:
        self._write("UPDATE triggers SET enabled = 1 - enabled WHERE id=?", (trigger_id,))

    def delete_trigger(self, trigger_id: int) -> None:
        self._write("DELETE FROM triggers WHERE id=?", (trigger_id,))

    # -- voip: contacts, calls, transcripts, do-not-call ------------------------
    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Keep digits (and a leading +) so caller IDs match imported numbers."""
        phone = (phone or "").strip()
        digits = "".join(c for c in phone if c.isdigit())
        return ("+" + digits) if phone.startswith("+") else digits

    def add_contact(self, name: str, phone: str, email: str = "", timezone: str = "",
                    notes: str = "", fields: dict | None = None, source: str = "") -> int:
        cur = self._write(
            "INSERT INTO contacts (name, phone, email, timezone, notes, fields, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, self.normalize_phone(phone), email, timezone, notes,
             json.dumps(fields or {}), source, time.time()))
        return int(cur.lastrowid)

    def list_contacts(self, limit: int = 500, q: str = "") -> list[dict[str, Any]]:
        if q:
            like = f"%{q}%"
            rows = self._query(
                "SELECT * FROM contacts WHERE name LIKE ? OR phone LIKE ? OR email LIKE ? "
                "ORDER BY id DESC LIMIT ?", (like, like, like, limit))
        else:
            rows = self._query("SELECT * FROM contacts ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["fields"] = json.loads(d.get("fields") or "{}")
            out.append(d)
        return out

    def find_contact_by_phone(self, phone: str) -> dict[str, Any] | None:
        # Match on the last 10 digits so +1-555... and 555... both hit. Too-short input
        # ("123", a withheld caller ID) must not match whichever contact ends that way.
        key = self.phone_key(phone)
        if len(key) < 7:
            return None
        for r in self._query("SELECT * FROM contacts WHERE phone LIKE ? ORDER BY id DESC",
                             (f"%{key}",)):
            if self.phone_key(r["phone"]) != key:
                continue
            d = dict(r)
            d["fields"] = json.loads(d.get("fields") or "{}")
            return d
        return None

    def count_contacts(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM contacts")[0][0])

    def delete_contact(self, contact_id: int) -> None:
        self._write("DELETE FROM contacts WHERE id=?", (contact_id,))

    def update_contact_status(self, contact_id: int, status: str, notes: str = "") -> None:
        self._write("UPDATE contacts SET status=?, notes=CASE WHEN ?<>'' THEN ? ELSE notes END "
                    "WHERE id=?", (status, notes, notes, contact_id))

    def start_call(self, direction: str, from_number: str, to_number: str,
                   provider_call_id: str = "", contact_id: int | None = None) -> int:
        cur = self._write(
            "INSERT INTO calls (direction, contact_id, from_number, to_number, provider_call_id, "
            "status, started_at) VALUES (?,?,?,?,?,'active',?)",
            (direction, contact_id, from_number, to_number, provider_call_id, time.time()))
        return int(cur.lastrowid)

    def add_call_turn(self, call_id: int, speaker: str, text: str) -> None:
        self._write("INSERT INTO call_turns (call_id, speaker, text, created_at) VALUES (?,?,?,?)",
                    (call_id, speaker, text, time.time()))

    def end_call(self, call_id: int, outcome: str = "", summary: str = "",
                 cost: float = 0.0, status: str = "ended") -> None:
        self._write("UPDATE calls SET status=?, outcome=?, summary=?, cost=?, ended_at=? WHERE id=?",
                    (status, outcome, summary, cost, time.time(), call_id))

    def list_calls(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT c.*, ct.name AS contact_name FROM calls c "
            "LEFT JOIN contacts ct ON ct.id = c.contact_id ORDER BY c.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def get_call(self, call_id: int) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT c.*, ct.name AS contact_name FROM calls c "
            "LEFT JOIN contacts ct ON ct.id = c.contact_id WHERE c.id=?", (call_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["turns"] = [dict(r) for r in self._query(
            "SELECT speaker, text, created_at FROM call_turns WHERE call_id=? ORDER BY id",
            (call_id,))]
        return d

    @staticmethod
    def phone_key(phone: str) -> str:
        """Comparable form of a number: its last 10 digits (``+1 555…`` == ``555…``)."""
        return "".join(c for c in (phone or "") if c.isdigit())[-10:]

    def add_dnc(self, phone: str, reason: str = "") -> None:
        self._write("INSERT OR REPLACE INTO dnc (phone, reason, created_at) VALUES (?,?,?)",
                    (self.normalize_phone(phone), reason, time.time()))

    def is_dnc(self, phone: str) -> bool:
        # Same last-10-digit rule as contact matching: a number stored as +15550100
        # must block a dial to 5550100, and vice versa.
        key = self.phone_key(phone)
        if len(key) < 7:
            return False
        rows = self._query("SELECT phone FROM dnc WHERE phone LIKE ?", (f"%{key}",))
        return any(self.phone_key(r["phone"]) == key for r in rows)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
