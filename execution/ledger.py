"""Independent execution ledger (execution_ledger.db).

Read-only against the crawl/decision DB. Never writes main-library business tables.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution.order_events import record_event

log = logging.getLogger("execution.ledger")

BUSY_TIMEOUT_MS = 30_000


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
        # isolation_level=None so claim/finalize can BEGIN IMMEDIATE explicitly.
        self.conn = sqlite3.connect(
            str(self.path),
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._init_schema()

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

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
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_log_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                event_ts TEXT NOT NULL,
                detail_json TEXT,
                UNIQUE(execution_log_id, event)
            );
            """
        )
        self._ensure_status_columns()
        self._ensure_unique_decision()

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

    def _ensure_unique_decision(self) -> None:
        """UNIQUE(decision_log_id) on existing ledgers that only had a plain index.

        If historical duplicates exist, leave the index off and log — do not
        delete rows. A clean ledger (production has no duplicates) gets the index.
        """
        existing = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_exec_log_decision'"
        ).fetchone()
        if existing:
            return
        dup = self.conn.execute(
            """
            SELECT decision_log_id, COUNT(*) AS n
            FROM execution_log
            GROUP BY decision_log_id
            HAVING n > 1
            LIMIT 1
            """
        ).fetchone()
        if dup is not None:
            log.warning(
                "execution_log has duplicate decision_log_id=%s (n=%s); "
                "unique index ux_exec_log_decision not created",
                dup["decision_log_id"],
                dup["n"],
            )
            return
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_exec_log_decision "
            "ON execution_log (decision_log_id)"
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
        """Advance the cursor to max(current, decision_log_id). Never rewinds."""
        self._advance_cursor(int(decision_log_id))

    def _advance_cursor(self, decision_log_id: int) -> None:
        """Cursor upsert. Participates in an open transaction; otherwise autocommits."""
        now = _utc()
        self.conn.execute(
            """
            INSERT INTO execution_cursor (key, value, updated_at)
            VALUES ('last_processed_id', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = CASE
                    WHEN CAST(excluded.value AS INTEGER) > CAST(execution_cursor.value AS INTEGER)
                    THEN excluded.value
                    ELSE execution_cursor.value
                END,
                updated_at = CASE
                    WHEN CAST(excluded.value AS INTEGER) > CAST(execution_cursor.value AS INTEGER)
                    THEN excluded.updated_at
                    ELSE execution_cursor.updated_at
                END
            """,
            (str(int(decision_log_id)), now),
        )

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
        return int(cur.lastrowid)

    def get_by_decision_log_id(self, decision_log_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM execution_log WHERE decision_log_id = ? ORDER BY id ASC LIMIT 1",
            (int(decision_log_id),),
        ).fetchone()
        return dict(row) if row else None

    def claim_decision(
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
    ) -> dict[str, Any]:
        """Insert status=claimed before any side effect. Commits immediately.

        On UNIQUE conflict returns the existing row with created=False.
        """
        strategy = str(strategy_id or "").strip() or "unknown"
        now = _utc()
        try:
            cur = self.conn.execute(
                """
                INSERT INTO execution_log (
                    ts, decision_log_id, strategy_id, action, chain_id,
                    token_address, pair_address, size_pct, reason, mode,
                    validation_ok, validation_errors, executed, executor, receipt_json,
                    status, status_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 0, NULL, NULL, 'claimed', ?)
                """,
                (
                    now,
                    int(decision_log_id),
                    strategy,
                    action,
                    chain_id,
                    token_address,
                    pair_address,
                    size_pct,
                    reason,
                    mode,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            self._rollback()
            existing = self.get_by_decision_log_id(decision_log_id)
            if existing is None:
                raise
            return {
                "execution_log_id": int(existing["id"]),
                "created": False,
                "status": existing.get("status"),
                "row": existing,
            }
        return {
            "execution_log_id": int(cur.lastrowid),
            "created": True,
            "status": "claimed",
            "row": self.get_by_decision_log_id(decision_log_id),
        }

    def finalize_receipt(
        self,
        execution_log_id: int,
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
        executed: int,
        executor: str | None,
        receipt: dict[str, Any] | None,
        status: str,
        events: list[tuple[str, dict[str, Any] | None]] | None = None,
        advance_cursor: bool = True,
        ts: str | None = None,
    ) -> None:
        """Update the claimed row, advance cursor with MAX, write events. One transaction."""
        now = ts or _utc()
        strategy = str(strategy_id or "").strip() or "unknown"
        err_json = (
            json.dumps(validation_errors, ensure_ascii=False)
            if validation_errors
            else None
        )
        receipt_json = (
            json.dumps(receipt, ensure_ascii=False) if receipt is not None else None
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                UPDATE execution_log SET
                    ts = ?, strategy_id = ?, action = ?, chain_id = ?,
                    token_address = ?, pair_address = ?, size_pct = ?, reason = ?,
                    mode = ?, validation_ok = ?, validation_errors = ?, executed = ?,
                    executor = ?, receipt_json = ?, status = ?, status_updated_at = ?
                WHERE id = ?
                """,
                (
                    now,
                    strategy,
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
                    now,
                    int(execution_log_id),
                ),
            )
            if advance_cursor:
                self._advance_cursor(int(decision_log_id))
            for event, detail in events or []:
                record_event(
                    self.path,
                    int(execution_log_id),
                    event,
                    detail,
                    event_ts=now,
                    conn=self.conn,
                    commit=False,
                )
            self.conn.execute("COMMIT")
        except Exception:
            self._rollback()
            raise

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
