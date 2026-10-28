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
import zlib
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
CREATE TABLE IF NOT EXISTS call_debug (
    call_id INTEGER PRIMARY KEY,
    data TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS training_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    filename TEXT,
    path TEXT,
    size INTEGER DEFAULT 0,
    status TEXT DEFAULT 'queued',
    progress REAL DEFAULT 0,
    error TEXT DEFAULT '',
    transcript TEXT DEFAULT '',
    doc_id INTEGER,
    chunks INTEGER DEFAULT 0,
    options TEXT DEFAULT '{}',
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS dnc (
    phone TEXT PRIMARY KEY,
    reason TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_msgs_run ON agent_messages(run_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON doc_chunks(doc_id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(created_at);
"""


# Columns added after the initial schema (applied idempotently by Store._migrate).
_MIGRATIONS = [
    ("facts", "embed_model", "TEXT DEFAULT ''"),
    ("skills", "embed_model", "TEXT DEFAULT ''"),
    ("skills", "successes", "INTEGER DEFAULT 0"),
    ("skills", "failures", "INTEGER DEFAULT 0"),
    ("skills", "last_used", "REAL"),
    ("skills", "tools", "TEXT DEFAULT ''"),     # JSON list of required tools
    ("skills", "trigger", "TEXT DEFAULT ''"),   # when to use this skill
    ("skills", "source", "TEXT DEFAULT ''"),    # learned | installed | pack id
    ("calls", "test", "INTEGER DEFAULT 0"),     # test calls: not counted, no contact updates
    ("calls", "transport", "TEXT DEFAULT ''"),  # telnyx | sip | browser
    ("calls", "line", "TEXT DEFAULT ''"),       # SIP line: trunk | extension ('' otherwise)
    ("calls", "recording", "TEXT DEFAULT ''"),  # path of the call's WAV (voip.recording)
    ("calls", "original_called_number", "TEXT DEFAULT ''"),   # number the caller dialled (SIP Diversion)
    ("calls", "via_queue", "INTEGER DEFAULT 0"),               # reached the AI through a PBX queue
    ("calls", "diversion_reason", "TEXT DEFAULT ''"),
    ("calls", "queue_wait_s", "INTEGER"),                      # seconds waited in the queue, if the PBX said
    ("calls", "crm_status", "TEXT DEFAULT ''"),     # sending | retrying | sent | failed | skipped
    ("calls", "crm_detail", "TEXT DEFAULT ''"),
    ("calls", "crm_payload", "TEXT DEFAULT ''"),    # exactly what was sent, for Resend
    ("calls", "demo", "INTEGER DEFAULT 0"),         # sample data for demos: never counted, never sent
    ("calls", "brief", "TEXT DEFAULT ''"),          # JSON call brief: applicant, vacancy, campaign (CRM v2)
]
# Stored in the database (PRAGMA user_version): changes whenever the schema text or
# the migration list changes, so existing databases get upgraded automatically.
SCHEMA_VERSION = zlib.crc32((_SCHEMA + repr(_MIGRATIONS) + "fts1").encode()) & 0x7FFFFFFF


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


def _without_payload(call: dict[str, Any]) -> dict[str, Any]:
    call.pop("crm_payload", None)       # large, and only Resend needs it (get_crm_payload)
    if "brief" in call:
        try:
            call["brief"] = json.loads(call["brief"]) if call["brief"] else None
        except ValueError:
            call["brief"] = None
    return call


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._fts = False
        with self._lock:
            # Request handlers open a Store per call; re-running the schema script,
            # migrations and FTS setup every time was most of that cost. The database
            # records which schema it has, so this only runs when the schema changed.
            if self.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                self._fts = bool(self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='doc_chunks_fts'").fetchone())
                if self._fts:
                    self._backfill_fts()  # heal an index emptied/never filled
                return
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
            self._migrate()
            self._init_fts()
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema (idempotent)."""
        def cols(table: str) -> set[str]:
            return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

        for table, col, ddl in _MIGRATIONS:
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
        self._backfill_fts()

    def _backfill_fts(self) -> None:
        """Fill an FTS index that is empty while its source table is not
        (content that predates FTS, or an index lost/rebuilt since)."""
        try:
            have = self.conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            if not have and self.conn.execute(
                    "SELECT COUNT(*) FROM messages").fetchone()[0]:
                self.conn.execute(
                    "INSERT INTO messages_fts (rowid, content, session_id, role) "
                    "SELECT id, content, session_id, role FROM messages"
                )
                self.conn.commit()
            have = self.conn.execute("SELECT COUNT(*) FROM doc_chunks_fts").fetchone()[0]
            if not have and self.conn.execute(
                    "SELECT COUNT(*) FROM doc_chunks").fetchone()[0]:
                self.conn.execute(
                    "INSERT INTO doc_chunks_fts (rowid, content, doc_id) "
                    "SELECT id, content, doc_id FROM doc_chunks"
                )
                self.conn.commit()
        except sqlite3.OperationalError:
            pass  # one of the FTS tables is absent; search falls back gracefully

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # -- sessions --------------------------------------------------------------
    def session_exists(self, session_id: int) -> bool:
        return bool(self._query("SELECT 1 FROM sessions WHERE id=?", (session_id,)))

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
        self.add_events([(run_id, agent_id, type_, data)])

    def add_events(self, events: list[tuple[str, str, str, dict[str, Any]]]) -> None:
        """Insert many monitor events in one transaction (one commit, not one per token)."""
        if not events:
            return
        now = time.time()
        with self._lock:
            self.conn.executemany(
                "INSERT INTO events (run_id, agent_id, type, data, created_at) VALUES (?,?,?,?,?)",
                [(r, a, t, json.dumps(d, default=str), now) for r, a, t, d in events])
            self.conn.commit()

    def prune_events(self, max_age_days: float = 30.0) -> int:
        """Drop monitor events older than ``max_age_days`` (the table grows per token)."""
        cur = self._write("DELETE FROM events WHERE created_at < ?",
                          (time.time() - max_age_days * 86400,))
        return int(cur.rowcount or 0)

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
        self._bump_chunks()

    def _bump_chunks(self) -> None:
        self._write("INSERT INTO meta (key, value) VALUES ('chunks', 1) "
                    "ON CONFLICT(key) DO UPDATE SET value = value + 1")

    def chunks_signature(self) -> tuple[int, int, int]:
        """Changes whenever document chunks or their vectors change (any process).

        Lets the in-memory search index know when to rebuild without re-reading
        every vector on every query.
        """
        with self._lock:
            ver = self.conn.execute("SELECT value FROM meta WHERE key='chunks'").fetchone()
            count, max_id = self.conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM doc_chunks").fetchone()
        return (int(ver[0]) if ver else 0, int(count), int(max_id))

    def delete_stale_documents(self, source: str, keep_hash: str) -> int:
        """Drop earlier versions of the document at ``source`` (any hash other
        than ``keep_hash``) so re-ingesting an edited file replaces it instead
        of leaving the stale text searchable next to the new."""
        rows = self._query("SELECT id FROM documents WHERE source=? AND hash<>?",
                           (source, keep_hash))
        for r in rows:
            self.delete_document(r["id"])
        return len(rows)

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
        self._bump_chunks()

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
        self._bump_chunks()

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

    @staticmethod
    def _phone_match(stored: str, key: str) -> bool:
        """Suffix match on digit keys: a 7–9 digit key matches the stored number
        whose digits END with it (and vice versa). Plain equality could never
        hit for short keys, so DNC/contact lookups silently failed for them."""
        sk = Store.phone_key(stored)
        return bool(sk) and (sk.endswith(key) or key.endswith(sk))

    def find_contact_by_phone(self, phone: str) -> dict[str, Any] | None:
        # Match on the last 10 digits so +1-555... and 555... both hit. Too-short input
        # ("123", a withheld caller ID) must not match whichever contact ends that way.
        key = self.phone_key(phone)
        if len(key) < 7:
            return None
        # No LIKE pre-filter: a stored "+1 (212) 555-0100" does not END with the
        # digit key, so SQL would never hand the row to the matcher.
        for r in self._query("SELECT * FROM contacts ORDER BY id DESC"):
            if not self._phone_match(r["phone"], key):
                continue
            d = dict(r)
            d["fields"] = json.loads(d.get("fields") or "{}")
            return d
        return None

    def get_contact(self, contact_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM contacts WHERE id=?", (contact_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["fields"] = json.loads(d.get("fields") or "{}")
        return d

    def count_contacts(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM contacts")[0][0])

    def delete_contact(self, contact_id: int) -> None:
        self._write("DELETE FROM contacts WHERE id=?", (contact_id,))

    def update_contact_status(self, contact_id: int, status: str, notes: str = "") -> None:
        self._write("UPDATE contacts SET status=?, notes=CASE WHEN ?<>'' THEN ? ELSE notes END "
                    "WHERE id=?", (status, notes, notes, contact_id))

    def start_call(self, direction: str, from_number: str, to_number: str,
                   provider_call_id: str = "", contact_id: int | None = None, *,
                   test: bool = False, transport: str = "", line: str = "",
                   original_called_number: str = "", via_queue: bool = False,
                   diversion_reason: str = "", queue_wait_s: int | None = None) -> int:
        cur = self._write(
            "INSERT INTO calls (direction, contact_id, from_number, to_number, provider_call_id, "
            "status, started_at, test, transport, line, original_called_number, via_queue, "
            "diversion_reason, queue_wait_s) VALUES (?,?,?,?,?,'active',?,?,?,?,?,?,?,?)",
            (direction, contact_id, from_number, to_number, provider_call_id, time.time(),
             int(test), transport, line, original_called_number, int(via_queue),
             diversion_reason, queue_wait_s))
        return int(cur.lastrowid)

    def set_call_brief(self, call_id: int, brief: dict[str, Any]) -> None:
        self._write("UPDATE calls SET brief=? WHERE id=?", (json.dumps(brief, default=str), call_id))

    def add_call_turn(self, call_id: int, speaker: str, text: str) -> None:
        self._write("INSERT INTO call_turns (call_id, speaker, text, created_at) VALUES (?,?,?,?)",
                    (call_id, speaker, text, time.time()))

    def end_call(self, call_id: int, outcome: str = "", summary: str = "",
                 cost: float = 0.0, status: str = "ended") -> None:
        self._write("UPDATE calls SET status=?, outcome=?, summary=?, cost=?, ended_at=? WHERE id=?",
                    (status, outcome, summary, cost, time.time(), call_id))

    def recover_interrupted_calls(self, live_ids: set[int] | frozenset[int] = frozenset()) -> list[int]:
        """Calls still marked active with no live session (the server stopped mid-call):
        close them as ``interrupted`` and return their ids."""
        ids = [int(r[0]) for r in self._query("SELECT id FROM calls WHERE status='active'")
               if int(r[0]) not in live_ids]
        for call_id in ids:
            row = self._query("SELECT started_at FROM calls WHERE id=?", (call_id,))[0]
            last = self._query("SELECT MAX(created_at) FROM call_turns WHERE call_id=?", (call_id,))[0][0]
            ended = float(last or row[0] or time.time())
            self._write("UPDATE calls SET status='ended', outcome='interrupted', ended_at=?, "
                        "summary=CASE WHEN COALESCE(summary,'')='' THEN ? ELSE summary END WHERE id=?",
                        (ended, "The call was cut off: the server stopped during the call.", call_id))
        return ids

    def save_call_debug(self, call_id: int, data: dict[str, Any]) -> None:
        self._write("INSERT OR REPLACE INTO call_debug (call_id, data, created_at) VALUES (?,?,?)",
                    (call_id, json.dumps(data, default=str), time.time()))

    def get_call_debug(self, call_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT data FROM call_debug WHERE call_id=?", (call_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def prune_call_debug(self, older_than_days: float) -> int:
        """Drop debug traces older than the recordings' retention (0 = keep)."""
        if older_than_days <= 0:
            return 0
        cur = self._write("DELETE FROM call_debug WHERE created_at < ?",
                          (time.time() - older_than_days * 86400,))
        return cur.rowcount

    def set_call_recording(self, call_id: int, path: str) -> None:
        self._write("UPDATE calls SET recording=? WHERE id=?", (path, call_id))

    def list_calls(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT c.*, ct.name AS contact_name FROM calls c "
            "LEFT JOIN contacts ct ON ct.id = c.contact_id ORDER BY c.id DESC LIMIT ?", (limit,))
        return [_without_payload(dict(r)) for r in rows]

    def count_calls(self, include_demo: bool = False) -> int:
        where = "" if include_demo else " WHERE demo=0"
        return int(self._query("SELECT COUNT(*) FROM calls" + where)[0][0])

    def add_demo_call(self, call: dict[str, Any], turns: list[tuple[str, str]],
                      debug: dict[str, Any] | None = None) -> int:
        """Sample call for demos (demo=1): straight into the table, no telephony, no CRM."""
        cur = self._write(
            "INSERT INTO calls (direction, from_number, to_number, status, outcome, summary, cost, "
            "started_at, ended_at, transport, line, via_queue, original_called_number, queue_wait_s, demo) "
            "VALUES (?,?,?,'ended',?,?,?,?,?,?,?,?,?,?,1)",
            (call["direction"], call["from_number"], call["to_number"], call["outcome"], call["summary"],
             call["cost"], call["started_at"], call["ended_at"], call["transport"], call.get("line", ""),
             int(call.get("via_queue", 0)), call.get("original_called_number", ""), call.get("queue_wait_s")))
        call_id = int(cur.lastrowid)
        for n, (speaker, text) in enumerate(turns):
            self._write("INSERT INTO call_turns (call_id, speaker, text, created_at) VALUES (?,?,?,?)",
                        (call_id, speaker, text, call["started_at"] + 4 * n))
        if debug is not None:
            self.save_call_debug(call_id, debug)
        return call_id

    def clear_demo_calls(self) -> int:
        """Remove every demo call and its transcript and diagnostics; real calls are untouched."""
        ids = [r[0] for r in self._query("SELECT id FROM calls WHERE demo=1")]
        for call_id in ids:
            self._write("DELETE FROM call_turns WHERE call_id=?", (call_id,))
            self._write("DELETE FROM call_debug WHERE call_id=?", (call_id,))
        self._write("DELETE FROM calls WHERE demo=1")
        return len(ids)

    def set_crm_payload(self, call_id: int, payload: dict[str, Any]) -> None:
        self._write("UPDATE calls SET crm_payload=? WHERE id=?", (json.dumps(payload, default=str), call_id))

    def get_crm_payload(self, call_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT crm_payload FROM calls WHERE id=?", (call_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def set_crm_status(self, call_id: int, status: str, detail: str = "") -> None:
        self._write("UPDATE calls SET crm_status=?, crm_detail=? WHERE id=?", (status, detail[:300], call_id))

    def get_call(self, call_id: int) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT c.*, ct.name AS contact_name FROM calls c "
            "LEFT JOIN contacts ct ON ct.id = c.contact_id WHERE c.id=?", (call_id,))
        if not rows:
            return None
        d = _without_payload(dict(rows[0]))
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
        rows = self._query("SELECT phone FROM dnc")
        return any(self._phone_match(r["phone"], key) for r in rows)

    # -- phone agent training material ------------------------------------------
    _TRAINING_FIELDS = {"status", "progress", "error", "transcript", "doc_id", "chunks", "path",
                        "size", "options"}

    def add_training_item(self, kind: str, filename: str, path: str = "", size: int = 0,
                          options: dict[str, Any] | None = None) -> int:
        now = time.time()
        cur = self._write(
            "INSERT INTO training_items (kind, filename, path, size, status, options, created_at, "
            "updated_at) VALUES (?,?,?,?,'queued',?,?,?)",
            (kind, filename, path, size, json.dumps(options or {}), now, now))
        return int(cur.lastrowid)

    def update_training_item(self, item_id: int, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items() if k in self._TRAINING_FIELDS}
        if not cols:
            return
        if isinstance(cols.get("options"), dict):
            cols["options"] = json.dumps(cols["options"])
        sets = ", ".join(f"{k}=?" for k in cols)
        self._write(f"UPDATE training_items SET {sets}, updated_at=? WHERE id=?",
                    (*cols.values(), time.time(), item_id))

    @staticmethod
    def _training_row(r: Any, full: bool) -> dict[str, Any]:
        d = dict(r)
        d["options"] = json.loads(d.get("options") or "{}")
        if not full:
            d["has_transcript"] = bool(d.pop("transcript", ""))
        return d

    def list_training_items(self, status: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM training_items"
        args: tuple = ()
        if status:
            sql, args = sql + " WHERE status=?", (status,)
        return [self._training_row(r, False) for r in self._query(sql + " ORDER BY id DESC", args)]

    def get_training_item(self, item_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM training_items WHERE id=?", (item_id,))
        return self._training_row(rows[0], True) if rows else None

    def training_transcripts(self, kinds: tuple[str, ...] = ("recording",)) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in kinds)
        rows = self._query(
            f"SELECT id, kind, filename, transcript FROM training_items WHERE status='done' "
            f"AND kind IN ({marks}) AND transcript<>'' ORDER BY id", kinds)
        return [dict(r) for r in rows]

    def delete_training_item(self, item_id: int) -> None:
        self._write("DELETE FROM training_items WHERE id=?", (item_id,))

    def close(self) -> None:
        with self._lock:
            self.conn.close()
