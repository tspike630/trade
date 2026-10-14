"""Session + message CRUD for M6. Writes only to dashboard_state.db."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dashboard.settings import db_root, resolve_db_file

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages(session_id, id);
"""


def state_db_path() -> Path:
    return resolve_db_file(("DASHBOARD_STATE_DB",), "dashboard_state.db", db_root())


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChatMessage:
    id: int
    session_id: str
    role: str
    content: str
    created_at: str


class SessionStore:
    """Local-only persistence. Never open business *.db files."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def list_sessions(self, query: str = "") -> list[SessionInfo]:
        needle = (query or "").strip()
        with self._connect() as conn:
            if needle:
                rows = conn.execute(
                    "SELECT id, title, created_at, updated_at FROM chat_sessions "
                    "WHERE title LIKE ? ORDER BY updated_at DESC",
                    (f"%{needle}%",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, title, created_at, updated_at FROM chat_sessions "
                    "ORDER BY updated_at DESC"
                ).fetchall()
        return [
            SessionInfo(
                id=str(row["id"]),
                title=str(row["title"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def create_session(self, title: str = "新会话") -> SessionInfo:
        sid = uuid.uuid4().hex
        now = _utc_now()
        clean = (title or "新会话").strip()[:80] or "新会话"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat_sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (sid, clean, now, now),
            )
        return SessionInfo(id=sid, title=clean, created_at=now, updated_at=now)

    def rename_session(self, session_id: str, title: str) -> bool:
        clean = (title or "").strip()[:80]
        if not clean:
            return False
        now = _utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?",
                (clean, now, session_id),
            )
            return cur.rowcount > 0

    def delete_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            cur = conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0

    def touch(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (_utc_now(), session_id),
            )

    def list_messages(self, session_id: str, limit: int = 200) -> list[ChatMessage]:
        """Newest ``limit`` messages, oldest of that window first.

        ``ORDER BY id ASC LIMIT`` kept the earliest rows, so once a session
        passed the cap the model context never saw the latest turns.
        """
        bounded = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, session_id, role, content, created_at FROM chat_messages "
                "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, bounded),
            ).fetchall()
        rows = list(reversed(rows))
        return [
            ChatMessage(
                id=int(row["id"]),
                session_id=str(row["session_id"]),
                role=str(row["role"]),
                content=str(row["content"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def append_message(self, session_id: str, role: str, content: str) -> ChatMessage:
        now = _utc_now()
        body = content if content is not None else ""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, role, body, now),
            )
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            msg_id = int(cur.lastrowid)
        return ChatMessage(
            id=msg_id,
            session_id=session_id,
            role=role,
            content=body,
            created_at=now,
        )

    def export_transcript(self, session_id: str) -> str:
        msgs = self.list_messages(session_id)
        return json.dumps(
            [{"role": m.role, "content": m.content, "at": m.created_at} for m in msgs],
            ensure_ascii=False,
            indent=2,
        )
