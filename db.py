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
    created_at TEXT    NOT NULL,
    speaker    TEXT,          -- "A", "B" … from diarization; NULL when unknown
    start_ms   INTEGER,       -- offset into the recording
    end_ms     INTEGER
);

CREATE TABLE IF NOT EXISTS speakers (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    label      TEXT    NOT NULL,   -- "A", "B" …
    name       TEXT,               -- what the user renamed them to
    PRIMARY KEY (session_id, label)
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
        # Older databases predate the diarization columns; add what is missing.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(segments)")}
        for column, decl in (
            ("speaker", "TEXT"),
            ("start_ms", "INTEGER"),
            ("end_ms", "INTEGER"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE segments ADD COLUMN {column} {decl}")


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

def add_segment(session_id, text, speaker=None, start_ms=None, end_ms=None):
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO segments (session_id, text, created_at, speaker, start_ms, end_ms)"
            " VALUES (?,?,?,?,?,?)",
            (session_id, text, ts, speaker, start_ms, end_ms),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, session_id))


def replace_segments(session_id, segments):
    """Swap in a fresh set of segments — used when a session is re-transcribed
    with diarization, which reassigns text to speakers across the whole file."""
    ts = now()
    with connect() as conn:
        conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
        conn.executemany(
            "INSERT INTO segments (session_id, text, created_at, speaker, start_ms, end_ms)"
            " VALUES (?,?,?,?,?,?)",
            [
                (session_id, s["text"], ts, s.get("speaker"),
                 s.get("start_ms"), s.get("end_ms"))
                for s in segments
            ],
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, session_id))


def get_segments(session_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT text, created_at, speaker, start_ms, end_ms"
            " FROM segments WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_transcript(session_id, with_speakers=False):
    """The session's words.

    With `with_speakers`, each line is prefixed by who said it — which is what
    makes a meeting summary able to say who committed to what.
    """
    segs = get_segments(session_id)
    if not with_speakers or not any(s.get("speaker") for s in segs):
        return " ".join(s["text"] for s in segs).strip()

    names = get_speaker_names(session_id)
    lines = []
    for s in segs:
        label = s.get("speaker")
        who = names.get(label) or (f"화자 {label}" if label else "알 수 없음")
        lines.append(f"{who}: {s['text']}")
    return "\n".join(lines).strip()


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


# ---------------------------------------------------------------- speakers

def set_speaker_name(session_id, label, name):
    with connect() as conn:
        conn.execute(
            "INSERT INTO speakers (session_id, label, name) VALUES (?,?,?)"
            " ON CONFLICT(session_id, label) DO UPDATE SET name=excluded.name",
            (session_id, label, name),
        )


def get_speaker_names(session_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT label, name FROM speakers WHERE session_id=?", (session_id,)
        ).fetchall()
        return {r["label"]: r["name"] for r in rows if r["name"]}


def speaker_stats(session_id):
    """Talk time per speaker, as milliseconds and share of the total."""
    segs = [s for s in get_segments(session_id) if s.get("speaker")]
    if not segs:
        return []
    names = get_speaker_names(session_id)
    totals = {}
    for s in segs:
        span = (s.get("end_ms") or 0) - (s.get("start_ms") or 0)
        if span <= 0:
            span = len(s["text"]) * 60  # rough fallback when timings are absent
        totals[s["speaker"]] = totals.get(s["speaker"], 0) + span

    grand = sum(totals.values()) or 1
    return [
        {
            "label": label,
            "name": names.get(label) or f"화자 {label}",
            "ms": ms,
            "share": round(ms / grand * 100),
        }
        for label, ms in sorted(totals.items(), key=lambda kv: -kv[1])
    ]
