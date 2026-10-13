"""Independent execution ledger (execution_ledger.db).

Read-only against the crawl/decision DB. Never writes main-library business tables.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_ledger_path(explicit: str | Path | None = None) -> Path:
    """Resolve execution_ledger.db path.

    Priority:
      1. explicit argument
      2. EXECUTION_LEDGER_PATH env
      3. default ``execution_ledger.db`` next to decision_engine DB (project root style)
    """
    if explicit is not None and str(explicit).strip():
        p = Path(str(explicit)).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()

    env = os.environ.get("EXECUTION_LEDGER_PATH", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()

    # Same directory style as decision_engine.config.DB_PATH (project root)
    try:
        from decision_engine.config import DB_PATH as _DE_DB

        root = Path(_DE_DB).resolve().parent
    except Exception:  # noqa: BLE001
        root = Path(__file__).resolve().parents[1]
    return (root / "execution_ledger.db").resolve()


class ExecutionLedger:
    """SQLite ledger: execution_log + cursor last_processed_id."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = resolve_ledger_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ExecutionLedger":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                decision_log_id INTEGER NOT NULL,
                strategy_id TEXT NOT NULL,
                action TEXT,
                chain_id TEXT,
                token_address TEXT,
                pair_address TEXT,
                size_pct REAL,
                reason TEXT,
                mode TEXT,
                validation_ok INTEGER NOT NULL,
                validation_errors TEXT,
                executed INTEGER NOT NULL DEFAULT 0,
                executor TEXT,
                receipt_json TEXT,
                status TEXT,
                status_updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_exec_log_decision
                ON execution_log (decision_log_id);
            CREATE INDEX IF NOT EXISTS idx_exec_log_strategy
                ON execution_log (strategy_id, ts);
            CREATE TABLE IF NOT EXISTS execution_cursor (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._ensure_status_columns()
        self.conn.commit()

    def _ensure_status_columns(self) -> None:
        """Idempotent migration: status + status_updated_at on execution_log.

        CREATE TABLE IF NOT EXISTS does not add columns to an existing table,
        so existing ledgers get ALTER TABLE once. Repeat opens are no-ops.
        """
        have = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(execution_log)").fetchall()
        }
        if "status" not in have:
            self.conn.execute("ALTER TABLE execution_log ADD COLUMN status TEXT")
        if "status_updated_at" not in have:
            self.conn.execute(
                "ALTER TABLE execution_log ADD COLUMN status_updated_at TEXT"
            )

    # --- cursor ---

    def get_last_processed_id(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM execution_cursor WHERE key = ?",
            ("last_processed_id",),
        ).fetchone()
        if not row:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    def set_last_processed_id(self, decision_log_id: int) -> None:
        now = _utc()
        self.conn.execute(
            """
            INSERT INTO execution_cursor (key, value, updated_at)
            VALUES ('last_processed_id', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (str(int(decision_log_id)), now),
        )
        self.conn.commit()

    # --- writes ---

    def append_receipt(
        self,
        *,
        decision_log_id: int,
        strategy_id: str,
        action: str | None,
        chain_id: str | None,
        token_address: str | None,
        pair_address: str | None,
        size_pct: float | None,
        reason: str | None,
        mode: str | None,
        validation_ok: bool,
        validation_errors: list[str] | None,
        executed: int = 0,
        executor: str | None = None,
        receipt: dict[str, Any] | None = None,
        ts: str | None = None,
        status: str | None = None,
        status_updated_at: str | None = None,
    ) -> int:
        """Insert one execution_log row. strategy_id is required on every row."""
        if not strategy_id:
            raise ValueError("strategy_id is required on every execution_log row")
        now = ts or _utc()
        err_json = (
            json.dumps(validation_errors, ensure_ascii=False)
            if validation_errors
            else None
        )
        receipt_json = (
            json.dumps(receipt, ensure_ascii=False) if receipt is not None else None
        )
        cur = self.conn.execute(
            """
            INSERT INTO execution_log (
                ts, decision_log_id, strategy_id, action, chain_id,
                token_address, pair_address, size_pct, reason, mode,
                validation_ok, validation_errors, executed, executor, receipt_json,
                status, status_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                int(decision_log_id),
                str(strategy_id),
                action,
                chain_id,
                token_address,
                pair_address,
                size_pct,
                reason,
                mode,
                1 if validation_ok else 0,
                err_json,
                int(executed),
                executor,
                receipt_json,
                status,
                status_updated_at,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) AS n FROM execution_log").fetchone()["n"]
        )

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM execution_log ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
