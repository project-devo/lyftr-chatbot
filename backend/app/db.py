"""SQLite session storage (stdlib sqlite3, one short-lived connection per operation)."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    system_prompt TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    metadata    TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);
"""


def _path() -> str:
    # sqlite:////data/app.db -> /data/app.db ; sqlite:///./app.db -> ./app.db
    return os.getenv("DATABASE_URL", "sqlite:///./app.db").removeprefix("sqlite:///")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(_path())), exist_ok=True)
    with closing(_connect()) as conn:
        conn.executescript(SCHEMA)


def ping() -> None:
    with closing(_connect()) as conn:
        conn.execute("SELECT 1")


def get_system_prompt(session_id: str) -> tuple[bool, str | None]:
    """(session exists, stored system prompt)."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT system_prompt FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return (row is not None, row["system_prompt"] if row else None)


def history(session_id: str) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    out = []
    for r in rows:
        msg = {"role": r["role"], "content": r["content"]}
        if r["metadata"]:
            msg["metadata"] = json.loads(r["metadata"])
        out.append(msg)
    return out


def save_turn(session_id: str, system_prompt: str | None, user_msg: str, user_at: str, reply: str, metadata: dict) -> None:
    """Session upsert + user message + assistant reply in ONE transaction (only called after LLM success)."""
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO sessions (session_id, system_prompt, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET system_prompt = COALESCE(excluded.system_prompt, system_prompt)",
            (session_id, system_prompt, user_at),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (session_id, user_msg, user_at),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, metadata, created_at) VALUES (?, 'assistant', ?, ?, ?)",
            (session_id, reply, json.dumps(metadata), now()),
        )
