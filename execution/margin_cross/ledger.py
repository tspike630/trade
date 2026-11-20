"""SQLite ledger for one cross-margin track. Not the spot ledger."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    canonical TEXT NOT NULL,
                    state TEXT NOT NULL,
                    baseline_base TEXT,
                    payload_json TEXT NOT NULL,
                    updated_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    intent_id TEXT NOT NULL,
                    op_type TEXT NOT NULL,
                    client_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    PRIMARY KEY (intent_id, op_type, client_key)
                );
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def intent_row(self, intent_id: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()

    def list_intents(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM intents ORDER BY updated_ms ASC, intent_id ASC"
            ).fetchall()

    def insert_intent(
        self,
        *,
        intent_id: str,
        purpose: str,
        canonical: str,
        state: str,
        payload: dict[str, Any],
        now_ms: int,
        baseline_base: str | None,
    ) -> bool:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT canonical FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if existing is not None:
                conn.commit()
                return False
            conn.execute(
                """
                INSERT INTO intents (
                    intent_id, purpose, canonical, state, baseline_base,
                    payload_json, updated_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    purpose,
                    canonical,
                    state,
                    baseline_base,
                    json.dumps(payload, sort_keys=True),
                    now_ms,
                ),
            )
            conn.commit()
        return True

    def set_state(
        self, intent_id: str, state: str, now_ms: int, baseline_base: str | None = None
    ) -> None:
        with self._conn() as conn:
            if baseline_base is None:
                conn.execute(
                    "UPDATE intents SET state = ?, updated_ms = ? WHERE intent_id = ?",
                    (state, now_ms, intent_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE intents
                    SET state = ?, updated_ms = ?, baseline_base = ?
                    WHERE intent_id = ?
                    """,
                    (state, now_ms, baseline_base, intent_id),
                )

    def begin_operation(
        self,
        intent_id: str,
        op_type: str,
        client_key: str,
        request: dict[str, Any],
    ) -> str:
        """Return ``inserted`` or the operation state already stored."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT state FROM operations
                WHERE intent_id = ? AND op_type = ? AND client_key = ?
                """,
                (intent_id, op_type, client_key),
            ).fetchone()
            if row is not None:
                conn.commit()
                return str(row["state"])
            conn.execute(
                """
                INSERT INTO operations (
                    intent_id, op_type, client_key, state, request_json, response_json
                ) VALUES (?, ?, ?, 'started', ?, NULL)
                """,
                (intent_id, op_type, client_key, json.dumps(request, sort_keys=True)),
            )
            conn.commit()
        return "inserted"

    def peek(self, intent_id: str, op_type: str, client_key: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT * FROM operations
                WHERE intent_id = ? AND op_type = ? AND client_key = ?
                """,
                (intent_id, op_type, client_key),
            ).fetchone()

    def mark(
        self,
        intent_id: str,
        op_type: str,
        client_key: str,
        state: str,
        response: dict[str, Any] | None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE operations
                SET state = ?, response_json = ?
                WHERE intent_id = ? AND op_type = ? AND client_key = ?
                """,
                (
                    state,
                    None if response is None else json.dumps(response, sort_keys=True),
                    intent_id,
                    op_type,
                    client_key,
                ),
            )

    def operation_count(self, op_type: str | None = None) -> int:
        with self._conn() as conn:
            if op_type is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM operations").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM operations WHERE op_type = ?",
                    (op_type,),
                ).fetchone()
        return int(row["n"]) if row is not None else 0
