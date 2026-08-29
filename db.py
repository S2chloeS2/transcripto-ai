"""SQLite persistence for TranscriptoAI.

Sessions survive restarts, which is what makes the Review screen worth having.
Everything is stored in a single file so the app stays clone-and-run.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "transcripto.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL DEFAULT 'Untitled session',
    kind        TEXT    NOT NULL DEFAULT 'lecture',   -- lecture | meeting
    source      TEXT    NOT NULL DEFAULT 'mic',       -- system | mic | link
    source_url  TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    summary     TEXT,
    keywords    TEXT             -- JSON array
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,   -- user | assistant
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_segments_session ON segments(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- sessions

def create_session(title, kind="lecture", source="mic", source_url=None):
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (title, kind, source, source_url, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (title, kind, source, source_url, ts, ts),
        )
        return cur.lastrowid


def get_session(session_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None


def list_sessions():
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, ("
            "  SELECT COUNT(*) FROM segments g WHERE g.session_id = s.id"
            ") AS segment_count"
            " FROM sessions s ORDER BY s.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_session(session_id, **fields):
    allowed = {"title", "kind", "summary", "keywords", "source_url"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "keywords" and value is not None and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.extend([now(), session_id])
    with connect() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", values)


def delete_session(session_id):
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


# ---------------------------------------------------------------- segments

def add_segment(session_id, text):
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO segments (session_id, text, created_at) VALUES (?,?,?)",
            (session_id, text, ts),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, session_id))


def get_segments(session_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT text, created_at FROM segments WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_transcript(session_id):
    return " ".join(s["text"] for s in get_segments(session_id)).strip()


# ---------------------------------------------------------------- messages

def add_message(session_id, role, content):
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, role, content, now()),
        )


def get_messages(session_id, limit=40):
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
