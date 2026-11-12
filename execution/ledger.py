"""Independent execution ledger (execution_ledger.db).

Read-only against the crawl/decision DB. Never writes main-library business tables.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from execution.observe_schema import ensure_observe_schema
from execution.verified_fet import VERIFIED_FET_EXIT
from execution.order_events import record_event

log = logging.getLogger("execution.ledger")

BUSY_TIMEOUT_MS = 30_000
# Legacy and untagged rows belong to this arm. Kept as a literal so the
# ledger does not import the spot adapter package.
_FADE_ARM = "live_bnb_boll_fade"
_CLASSIC_ARM = "live_bnb_boll_classic"
_FADE_LEDGER_NAME = "execution_ledger_live_bnb_boll_fade.db"
_CLASSIC_LEDGER_NAME = "execution_ledger_live_bnb_boll_classic.db"
_BUY_LOCK_NAME = "binance_spot_shared_buy.lock"


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bnb_purpose_clause(purpose: str | None) -> tuple[str, tuple[Any, ...]]:
    """Limit a spot-position query to one arm.

    Untagged rows are legacy fade holds. An empty ``purpose`` does not filter.
    """
    arm = str(purpose or "").strip()
    if not arm:
        return "", ()
    if arm == _FADE_ARM:
        return " AND (purpose IS NULL OR purpose = '' OR purpose = ?)", (arm,)
    return " AND purpose = ?", (arm,)


def peer_live_bnb_ledger_path(path: str | Path) -> Path | None:
    """The other arm's ledger file when ``path`` is one of the two named books."""
    current = Path(path)
    name = current.name
    if name == _FADE_LEDGER_NAME:
        return current.with_name(_CLASSIC_LEDGER_NAME)
    if name == _CLASSIC_LEDGER_NAME:
        return current.with_name(_FADE_LEDGER_NAME)
    return None


def read_spot_buy_blocks_readonly(path: str | Path) -> list[dict[str, Any]]:
    """Non-terminal spot rows from another file. Missing file or table is empty.

    Opens read-only and does not create the database.
    """
    file = Path(path)
    if not file.is_file():
        return []
    uri = "file:" + file.resolve().as_posix() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM binance_spot_positions
            WHERE status NOT IN ('closed', 'closed_dust', 'oco_filled', 'closed_canceled')
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


@contextmanager
def buy_reservation_lock(ledger_path: str | Path) -> Iterator[None]:
    """One exclusive lock for both arms' ledgers in the same directory.

    Held across the funding read and the ledger insert so the other process
    cannot pass the same free balance first.     Does not place an order itself.
    """
    directory = Path(ledger_path).resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / _BUY_LOCK_NAME).open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def resolve_ledger_path(explicit: str | Path | None = None) -> Path:
    """Resolve execution_ledger.db path.

    Priority:
      1. explicit argument
      2. EXECUTION_LEDGER_PATH or EXECUTION_LEDGER_DB env
      3. default ``execution_ledger.db`` next to decision_engine DB (project root style)
    """
    if explicit is not None and str(explicit).strip():
        p = Path(str(explicit)).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()

    env = (
        os.environ.get("EXECUTION_LEDGER_PATH", "").strip()
        or os.environ.get("EXECUTION_LEDGER_DB", "").strip()
    )
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
        ensure_observe_schema(self.conn)
        self._ensure_status_columns()
        self._ensure_unique_decision()
        self._ensure_binance_spot_positions()
        self._ensure_binance_spot_position_columns()
        self._ensure_mcp_book()

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

    def _ensure_binance_spot_positions(self) -> None:
        """Held Binance spot longs for the live_bnb protective-exit loop.

        One row per decision. ``quantity`` is the exchange base fill, never a
        quote conversion. Status ``open`` can be sold; ``qty_unconfirmed``
        cannot; ``exit_submitted`` must not be sold again.
        """
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS binance_spot_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_log_id INTEGER NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                purpose TEXT,
                status TEXT NOT NULL,
                quantity TEXT,
                stop_price TEXT NOT NULL,
                take_profit_price TEXT NOT NULL,
                entry_price TEXT,
                entry_order_id TEXT,
                exit_order_id TEXT,
                exit_reason TEXT,
                exit_client_order_id TEXT,
                last_error TEXT,
                oco_list_client_id TEXT,
                oco_order_list_id TEXT,
                oco_list_status TEXT,
                oco_above_order_id TEXT,
                oco_below_order_id TEXT,
                oco_above_client_id TEXT,
                oco_below_client_id TEXT,
                residual_qty TEXT,
                residual_quote TEXT,
                protection TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bnb_spot_pos_status "
            "ON binance_spot_positions (status)"
        )

    def _ensure_binance_spot_position_columns(self) -> None:
        """Add OCO and dust columns on ledgers created before they existed."""
        have = {
            row[1]
            for row in self.conn.execute(
                "PRAGMA table_info(binance_spot_positions)"
            ).fetchall()
        }
        for name in (
            "oco_list_client_id",
            "oco_order_list_id",
            "oco_list_status",
            "oco_above_order_id",
            "oco_below_order_id",
            "oco_above_client_id",
            "oco_below_client_id",
            "residual_qty",
            "residual_quote",
            "purpose",
            "oco_ratchet_count",
            "initial_stop_price",
            "entry_order_qty",
            "entry_limit_price",
            "created_at_ms",
            "exit_filled_qty",
        ):
            if name not in have:
                self.conn.execute(
                    f"ALTER TABLE binance_spot_positions ADD COLUMN {name} TEXT"
                )

    def _ensure_mcp_book(self) -> None:
        """Canonical hold and spent client ids for the live_bnb gate.

        These rows are not ``binance_spot_positions`` and are not an exit plan.
        """
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_positions (
                purpose TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_log_id INTEGER NOT NULL,
                quantity TEXT,
                entry_order_id TEXT,
                client_order_id TEXT,
                avg_price TEXT,
                cummulative_quote_qty TEXT,
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (purpose, decision_log_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_position_exits (
                decision_log_id INTEGER PRIMARY KEY,
                sell_order_id TEXT NOT NULL,
                avg_price TEXT NOT NULL,
                proceeds TEXT NOT NULL,
                gross_pnl TEXT NOT NULL,
                fee_entry_bnb TEXT NOT NULL,
                fee_exit_bnb TEXT NOT NULL,
                fee_total_bnb TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spent_orders (
                client_order_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                executed_qty TEXT,
                decision_log_id INTEGER,
                purpose TEXT NOT NULL,
                commission TEXT,
                commission_asset TEXT
            )
            """
        )

    def import_verified_fill(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """Insert one verified spot fill. Does not move the execution cursor.

        Exit prices are refused. A repeated client id is a no-op.
        """
        if receipt.get("stop_price") not in (None, "") or receipt.get("take_profit_price") not in (
            None,
            "",
        ):
            raise ValueError("blind_exit_rejected")
        encoded = json.dumps(receipt, sort_keys=True, default=str)
        if "0.156425" in encoded:
            raise ValueError("blind_exit_rejected")
        cursor_before = self.get_last_processed_id()
        purpose = str(receipt.get("purpose") or "").strip()
        symbol = str(receipt.get("symbol") or "").strip()
        client_id = str(receipt.get("client_order_id") or "").strip()
        order_id = str(receipt.get("order_id") or "").strip()
        decision_id = int(receipt["decision_log_id"])
        if not purpose or not symbol or not client_id or not order_id:
            raise ValueError("verified_fill_incomplete")
        now = _utc()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO canonical_positions (
                purpose, symbol, status, decision_log_id, quantity,
                entry_order_id, client_order_id, avg_price,
                cummulative_quote_qty, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                purpose,
                symbol,
                str(receipt.get("status") or "held_naked"),
                decision_id,
                receipt.get("quantity"),
                order_id,
                client_id,
                receipt.get("avg_price"),
                receipt.get("cummulative_quote_qty"),
                receipt.get("source"),
                now,
                now,
            ),
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO spent_orders (
                client_order_id, order_id, symbol, executed_qty,
                decision_log_id, purpose, commission, commission_asset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                order_id,
                symbol,
                receipt.get("executed_qty", receipt.get("quantity")),
                decision_id,
                purpose,
                receipt.get("commission"),
                receipt.get("commission_asset"),
            ),
        )
        self.conn.commit()
        if self.get_last_processed_id() != cursor_before:
            raise RuntimeError("verified fill moved the execution cursor")
        return self.mcp_book()

    def close_canonical_exit(self, exit_row: dict[str, Any]) -> dict[str, Any]:
        """Record the verified FET sell. Does not touch the execution log or cursor.

        Decision 147831 only. The numbers are the operator's closed row:
        sell 3736070415, average 0.1459, proceeds 20.44059, gross pnl -1.49907,
        and the two BNB fees that sum to 0.00004528. A second call is a no-op.
        """
        cursor_before = self.get_last_processed_id()
        log_before = self.count()
        events_before = int(
            self.conn.execute("SELECT COUNT(*) AS n FROM order_events").fetchone()["n"]
        )
        decision_id = int(exit_row["decision_log_id"])
        if decision_id != int(VERIFIED_FET_EXIT["decision_log_id"]):
            raise ValueError("exit_decision_mismatch")
        for key in (
            "sell_order_id",
            "avg_price",
            "proceeds",
            "gross_pnl",
            "fee_entry_bnb",
            "fee_exit_bnb",
            "fee_total_bnb",
        ):
            if str(exit_row.get(key)) != str(VERIFIED_FET_EXIT[key]):
                raise ValueError(f"exit_field_mismatch:{key}")
        row = self.conn.execute(
            """
            SELECT cummulative_quote_qty, client_order_id, status
            FROM canonical_positions
            WHERE decision_log_id = ? AND purpose = ?
            """,
            (decision_id, "live_bnb_boll_fade"),
        ).fetchone()
        if row is None:
            raise ValueError("canonical_row_missing")
        if str(row["client_order_id"]) != "bnbfade-147831":
            raise ValueError("exit_client_mismatch")
        proceeds = Decimal(str(VERIFIED_FET_EXIT["proceeds"]))
        entry_quote = Decimal(str(row["cummulative_quote_qty"]))
        pnl = proceeds - entry_quote
        if pnl != Decimal(str(VERIFIED_FET_EXIT["gross_pnl"])):
            raise ValueError("exit_pnl_mismatch")
        fees = Decimal(str(VERIFIED_FET_EXIT["fee_entry_bnb"])) + Decimal(
            str(VERIFIED_FET_EXIT["fee_exit_bnb"])
        )
        if fees != Decimal(str(VERIFIED_FET_EXIT["fee_total_bnb"])):
            raise ValueError("exit_fee_mismatch")
        now = _utc()
        self.conn.execute(
            """
            UPDATE canonical_positions
            SET status = 'closed', updated_at = ?
            WHERE decision_log_id = ? AND purpose = ?
            """,
            (now, decision_id, "live_bnb_boll_fade"),
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO canonical_position_exits (
                decision_log_id, sell_order_id, avg_price, proceeds, gross_pnl,
                fee_entry_bnb, fee_exit_bnb, fee_total_bnb, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                VERIFIED_FET_EXIT["sell_order_id"],
                VERIFIED_FET_EXIT["avg_price"],
                VERIFIED_FET_EXIT["proceeds"],
                VERIFIED_FET_EXIT["gross_pnl"],
                VERIFIED_FET_EXIT["fee_entry_bnb"],
                VERIFIED_FET_EXIT["fee_exit_bnb"],
                VERIFIED_FET_EXIT["fee_total_bnb"],
                now,
            ),
        )
        self.conn.commit()
        if self.get_last_processed_id() != cursor_before:
            raise RuntimeError("canonical exit moved the execution cursor")
        if self.count() != log_before:
            raise RuntimeError("canonical exit wrote execution_log")
        events_after = int(
            self.conn.execute("SELECT COUNT(*) AS n FROM order_events").fetchone()["n"]
        )
        if events_after != events_before:
            raise RuntimeError("canonical exit wrote order_events")
        return self.mcp_book()

    def mcp_book(self) -> dict[str, Any]:
        """Book the live_bnb gate reads. Open orders are not invented."""
        canonical = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT purpose, symbol, status, decision_log_id, quantity,
                       entry_order_id, client_order_id, avg_price,
                       cummulative_quote_qty, source
                FROM canonical_positions
                ORDER BY decision_log_id
                """
            )
        ]
        spent = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT order_id, client_order_id, symbol, executed_qty,
                       decision_log_id, purpose, commission, commission_asset
                FROM spent_orders
                ORDER BY decision_log_id
                """
            )
        ]
        return {
            "canonical_positions": canonical,
            "spent_orders": spent,
            "open_orders": [],
        }

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

    def arm_binance_spot_position(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Insert a held long once. A replay returns the existing row unchanged."""
        decision_id = int(spec["decision_log_id"])
        now = _utc()
        purpose = str(spec.get("purpose") or "").strip() or _FADE_ARM
        self.conn.execute(
            """
            INSERT OR IGNORE INTO binance_spot_positions (
                decision_log_id, symbol, purpose, status, quantity, stop_price,
                take_profit_price, entry_price, entry_order_id, protection,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                str(spec["symbol"]),
                purpose,
                str(spec["status"]),
                spec.get("quantity"),
                str(spec["stop_price"]),
                str(spec["take_profit_price"]),
                spec.get("entry_price"),
                spec.get("entry_order_id"),
                str(spec["protection"]),
                now,
                now,
            ),
        )
        self.conn.commit()
        row = self.binance_spot_position_by_decision(decision_id)
        if row is None:
            raise RuntimeError(f"binance spot position missing for {decision_id}")
        extra = {
            key: spec[key]
            for key in (
                "oco_ratchet_count",
                "initial_stop_price",
                "entry_order_qty",
                "entry_limit_price",
                "created_at_ms",
            )
            if key in spec and spec.get(key) not in (None, "")
        }
        if extra:
            updated = self.update_binance_spot_position(int(row["id"]), extra)
            if isinstance(updated, dict):
                return updated
        return row

    def binance_spot_position_by_decision(
        self, decision_log_id: int
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM binance_spot_positions WHERE decision_log_id = ?",
            (int(decision_log_id),),
        ).fetchone()
        return dict(row) if row else None

    def binance_spot_position_by_id(self, position_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM binance_spot_positions WHERE id = ?",
            (int(position_id),),
        ).fetchone()
        return dict(row) if row else None

    _BINANCE_SPOT_MUTABLE = (
        "status",
        "quantity",
        "stop_price",
        "take_profit_price",
        "oco_ratchet_count",
        "initial_stop_price",
        "entry_order_qty",
        "entry_limit_price",
        "created_at_ms",
        "exit_filled_qty",
        "protection",
        "exit_order_id",
        "exit_reason",
        "exit_client_order_id",
        "last_error",
        "oco_list_client_id",
        "oco_order_list_id",
        "oco_list_status",
        "oco_above_order_id",
        "oco_below_order_id",
        "oco_above_client_id",
        "oco_below_client_id",
        "residual_qty",
        "residual_quote",
    )

    def update_binance_spot_position(
        self, position_id: int, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Write a whitelisted subset of the held-position row."""
        allowed = {
            key: fields[key]
            for key in self._BINANCE_SPOT_MUTABLE
            if key in fields
        }
        if not allowed:
            return self.binance_spot_position_by_id(position_id)
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        values = [allowed[key] for key in allowed]
        values.append(_utc())
        values.append(int(position_id))
        self.conn.execute(
            f"UPDATE binance_spot_positions SET {assignments}, updated_at = ? WHERE id = ?",
            values,
        )
        self.conn.commit()
        return self.binance_spot_position_by_id(position_id)

    def list_binance_spot_positions_for_exit(
        self, purpose: str | None = None
    ) -> list[dict[str, Any]]:
        clause, params = _bnb_purpose_clause(purpose)
        rows = self.conn.execute(
            f"""
            SELECT * FROM binance_spot_positions
            WHERE status IN (
                'open', 'qty_unconfirmed', 'oco_working', 'oco_unknown', 'oco_failed',
                'maker_resting', 'maker_cancel_unknown', 'time_stop_unknown'
            )
            {clause}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_binance_spot_buy_blocks(
        self, purpose: str | None = None
    ) -> list[dict[str, Any]]:
        """Non-terminal spot rows for one arm. The buy gate reads these.

        A protected open blocks a new BUY of that same symbol. Another symbol
        on the arm is not stacked onto that row. Unsafe rows still block the
        arm. A ``closed_dust`` row is included so a partial-exit remainder
        can still block; a whole-qty dust abandon does not.
        ``purpose`` omitted returns every arm. Callers that pass a
        purpose leave the other arm's open position out of this list.
        """
        clause, params = _bnb_purpose_clause(purpose)
        rows = self.conn.execute(
            f"""
            SELECT * FROM binance_spot_positions
            WHERE status NOT IN ('closed', 'oco_filled', 'closed_canceled')
            {clause}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def confirm_binance_spot_qty(
        self, position_id: int, quantity: str
    ) -> dict[str, Any] | None:
        """Promote ``qty_unconfirmed`` once the entry order reports a base fill."""
        now = _utc()
        self.conn.execute(
            """
            UPDATE binance_spot_positions
            SET quantity = ?, status = 'open', last_error = NULL, updated_at = ?
            WHERE id = ? AND status = 'qty_unconfirmed'
            """,
            (str(quantity), now, int(position_id)),
        )
        self.conn.commit()
        saved = self.conn.execute(
            "SELECT * FROM binance_spot_positions WHERE id = ?",
            (int(position_id),),
        ).fetchone()
        return dict(saved) if saved else None

    def apply_binance_spot_exit(
        self, position_id: int, outcome: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Record a sell only after the exchange accepted it. Failures stay open."""
        current = self.conn.execute(
            "SELECT * FROM binance_spot_positions WHERE id = ?",
            (int(position_id),),
        ).fetchone()
        if current is None:
            return None
        row = dict(current)
        now = _utc()
        submitted = bool(outcome.get("submitted")) and bool(outcome.get("exchange_order_id"))
        if submitted and row["status"] == "open":
            self.conn.execute(
                """
                UPDATE binance_spot_positions SET
                    status = 'exit_submitted',
                    quantity = COALESCE(?, quantity),
                    exit_order_id = ?,
                    exit_reason = ?,
                    exit_client_order_id = ?,
                    protection = COALESCE(?, protection),
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (
                    outcome.get("quantity"),
                    str(outcome.get("exchange_order_id")),
                    outcome.get("reason"),
                    outcome.get("exit_client_order_id"),
                    outcome.get("protection"),
                    now,
                    int(position_id),
                ),
            )
        elif outcome.get("error") and row["status"] in {"open", "qty_unconfirmed"}:
            self.conn.execute(
                """
                UPDATE binance_spot_positions
                SET last_error = ?, updated_at = ?
                WHERE id = ? AND status IN ('open', 'qty_unconfirmed')
                """,
                (str(outcome.get("error")), now, int(position_id)),
            )
        self.conn.commit()
        saved = self.conn.execute(
            "SELECT * FROM binance_spot_positions WHERE id = ?",
            (int(position_id),),
        ).fetchone()
        return dict(saved) if saved else None
