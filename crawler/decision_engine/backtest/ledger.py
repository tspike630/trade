"""Independent backtest ledger (separate sqlite; does not touch crawl DB).

Tables: runs, backtest_log (fills/signals), positions, equity_curve.
Inspired by paper_trade.PaperLedger but isolated for historical replay.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BacktestLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        start_cash: float = 10_000.0,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.start_cash = float(start_cash)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self.run_id: str | None = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "BacktestLedger":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                strategy_id TEXT,
                params_json TEXT,
                start_cash REAL,
                end_equity REAL,
                status TEXT,
                notes TEXT
            );
            CREATE TABLE IF NOT EXISTS backtest_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                action TEXT NOT NULL,
                chain_id TEXT,
                token_address TEXT,
                pair_address TEXT,
                price REAL,
                size_usd REAL,
                size_pct REAL,
                reason TEXT,
                position_id TEXT,
                pnl_usd REAL,
                filter_ok INTEGER,
                filter_reasons TEXT,
                raw_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_backtest_log_run
                ON backtest_log (run_id, ts);
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                chain_id TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                token_address TEXT,
                qty REAL NOT NULL,
                entry_price REAL NOT NULL,
                size_usd REAL NOT NULL,
                opened_at TEXT NOT NULL,
                status TEXT NOT NULL,
                tp_pct REAL,
                sl_pct REAL,
                max_hold_min REAL,
                exit_price REAL,
                closed_at TEXT,
                exit_reason TEXT,
                pnl_usd REAL,
                meta_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bt_pos_run
                ON positions (run_id, strategy_id, status);
            CREATE TABLE IF NOT EXISTS equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                cash_usd REAL NOT NULL,
                mtm_usd REAL NOT NULL,
                equity_usd REAL NOT NULL,
                open_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_equity_run
                ON equity_curve (run_id, strategy_id, ts);
            CREATE TABLE IF NOT EXISTS account (
                run_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                cash_usd REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, strategy_id)
            );
            """
        )
        self.conn.commit()

    def start_run(
        self,
        *,
        strategy_id: str,
        params: dict[str, Any] | None = None,
        notes: str = "",
    ) -> str:
        rid = str(uuid.uuid4())
        self.run_id = rid
        now = _utc()
        self.conn.execute(
            """
            INSERT INTO runs (run_id, started_at, strategy_id, params_json,
                              start_cash, status, notes)
            VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                rid,
                now,
                strategy_id,
                json.dumps(params or {}, default=str),
                self.start_cash,
                notes,
            ),
        )
        self.conn.commit()
        return rid

    def ensure_account(self, strategy_id: str, *, ts: str | None = None) -> None:
        assert self.run_id
        existing = self.conn.execute(
            "SELECT 1 FROM account WHERE run_id=? AND strategy_id=?",
            (self.run_id, strategy_id),
        ).fetchone()
        if existing:
            return
        self.conn.execute(
            """
            INSERT INTO account (run_id, strategy_id, cash_usd, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (self.run_id, strategy_id, self.start_cash, ts or _utc()),
        )

    def finish_run(
        self,
        *,
        strategy_id: str,
        end_equity: float,
        status: str = "done",
        notes: str | None = None,
    ) -> None:
        assert self.run_id
        self.conn.execute(
            """
            UPDATE runs SET finished_at=?, end_equity=?, status=?,
                   notes=COALESCE(?, notes)
            WHERE run_id=?
            """,
            (_utc(), end_equity, status, notes, self.run_id),
        )
        self.conn.commit()

    def get_cash(self, strategy_id: str) -> float:
        assert self.run_id
        row = self.conn.execute(
            "SELECT cash_usd FROM account WHERE run_id=? AND strategy_id=?",
            (self.run_id, strategy_id),
        ).fetchone()
        return float(row["cash_usd"]) if row else 0.0

    def set_cash(self, strategy_id: str, cash: float, *, ts: str | None = None) -> None:
        assert self.run_id
        self.conn.execute(
            "UPDATE account SET cash_usd=?, updated_at=? WHERE run_id=? AND strategy_id=?",
            (cash, ts or _utc(), self.run_id, strategy_id),
        )

    def log_event(
        self,
        *,
        strategy_id: str,
        action: str,
        ts: str,
        chain_id: str | None = None,
        token_address: str | None = None,
        pair_address: str | None = None,
        price: float | None = None,
        size_usd: float | None = None,
        size_pct: float | None = None,
        reason: str | None = None,
        position_id: str | None = None,
        pnl_usd: float | None = None,
        filter_ok: bool | None = None,
        filter_reasons: list[str] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        assert self.run_id
        self.conn.execute(
            """
            INSERT INTO backtest_log (
                run_id, ts, strategy_id, action, chain_id, token_address,
                pair_address, price, size_usd, size_pct, reason, position_id,
                pnl_usd, filter_ok, filter_reasons, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                ts,
                strategy_id,
                action,
                chain_id,
                token_address,
                pair_address,
                price,
                size_usd,
                size_pct,
                reason,
                position_id,
                pnl_usd,
                None if filter_ok is None else (1 if filter_ok else 0),
                json.dumps(filter_reasons or []),
                json.dumps(raw or {}, default=str),
            ),
        )

    def open_position(
        self,
        *,
        strategy_id: str,
        chain_id: str,
        pair_address: str,
        token_address: str | None,
        price: float,
        size_usd: float,
        ts: str,
        tp_pct: float | None = 10.0,
        sl_pct: float | None = 5.0,
        max_hold_min: float | None = 60.0,
        reason: str = "",
        meta: dict[str, Any] | None = None,
        size_pct: float | None = None,
    ) -> Optional[str]:
        assert self.run_id
        if price <= 0 or size_usd <= 0:
            return None
        cash = self.get_cash(strategy_id)
        if size_usd > cash:
            size_usd = cash
        if size_usd <= 0:
            self.log_event(
                strategy_id=strategy_id,
                action="skip_no_cash",
                ts=ts,
                chain_id=chain_id,
                token_address=token_address,
                pair_address=pair_address,
                price=price,
                reason="insufficient cash",
            )
            return None
        qty = size_usd / price
        pid = str(uuid.uuid4())
        self.set_cash(strategy_id, cash - size_usd, ts=ts)
        self.conn.execute(
            """
            INSERT INTO positions (
                id, run_id, strategy_id, chain_id, pair_address, token_address,
                qty, entry_price, size_usd, opened_at, status,
                tp_pct, sl_pct, max_hold_min, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                pid,
                self.run_id,
                strategy_id,
                chain_id,
                pair_address,
                token_address,
                qty,
                price,
                size_usd,
                ts,
                tp_pct,
                sl_pct,
                max_hold_min,
                json.dumps(meta or {}),
            ),
        )
        self.log_event(
            strategy_id=strategy_id,
            action="buy",
            ts=ts,
            chain_id=chain_id,
            token_address=token_address,
            pair_address=pair_address,
            price=price,
            size_usd=size_usd,
            size_pct=size_pct,
            reason=reason,
            position_id=pid,
            filter_ok=True,
            raw=meta,
        )
        return pid

    def close_position(
        self,
        pid: str,
        *,
        price: float,
        ts: str,
        reason: str,
    ) -> dict[str, Any] | None:
        assert self.run_id
        row = self.conn.execute(
            "SELECT * FROM positions WHERE id=? AND run_id=?",
            (pid, self.run_id),
        ).fetchone()
        if not row or row["status"] != "open":
            return None
        qty = float(row["qty"])
        notional = qty * price
        pnl = notional - float(row["size_usd"])
        sid = row["strategy_id"]
        self.set_cash(sid, self.get_cash(sid) + notional, ts=ts)
        self.conn.execute(
            """
            UPDATE positions SET status='closed', exit_price=?, closed_at=?,
                   exit_reason=?, pnl_usd=? WHERE id=?
            """,
            (price, ts, reason, pnl, pid),
        )
        self.log_event(
            strategy_id=sid,
            action="sell",
            ts=ts,
            chain_id=row["chain_id"],
            token_address=row["token_address"],
            pair_address=row["pair_address"],
            price=price,
            size_usd=notional,
            reason=reason,
            position_id=pid,
            pnl_usd=pnl,
        )
        return {"id": pid, "pnl_usd": pnl, "exit_price": price, "reason": reason}

    def list_open(self, strategy_id: str) -> list[dict[str, Any]]:
        assert self.run_id
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT * FROM positions
                WHERE run_id=? AND strategy_id=? AND status='open'
                ORDER BY opened_at
                """,
                (self.run_id, strategy_id),
            ).fetchall()
        ]

    def list_closed(self, strategy_id: str) -> list[dict[str, Any]]:
        assert self.run_id
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT * FROM positions
                WHERE run_id=? AND strategy_id=? AND status='closed'
                ORDER BY closed_at
                """,
                (self.run_id, strategy_id),
            ).fetchall()
        ]

    def mark_equity(
        self,
        *,
        strategy_id: str,
        ts: str,
        marks: dict[str, float],
    ) -> dict[str, Any]:
        """marks: chain|pair -> price."""
        assert self.run_id
        cash = self.get_cash(strategy_id)
        open_pos = self.list_open(strategy_id)
        mtm = 0.0
        for p in open_pos:
            key = f"{p['chain_id']}|{p['pair_address']}"
            px = marks.get(key, float(p["entry_price"]))
            mtm += float(p["qty"]) * px
        eq = {
            "cash_usd": cash,
            "mtm_usd": mtm,
            "equity_usd": cash + mtm,
            "open_count": len(open_pos),
        }
        self.conn.execute(
            """
            INSERT INTO equity_curve
                (run_id, strategy_id, ts, cash_usd, mtm_usd, equity_usd, open_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                strategy_id,
                ts,
                eq["cash_usd"],
                eq["mtm_usd"],
                eq["equity_usd"],
                eq["open_count"],
            ),
        )
        return eq

    def equity_series(self, strategy_id: str) -> list[dict[str, Any]]:
        assert self.run_id
        return [
            dict(r)
            for r in self.conn.execute(
                """
                SELECT ts, cash_usd, mtm_usd, equity_usd, open_count
                FROM equity_curve
                WHERE run_id=? AND strategy_id=?
                ORDER BY ts, id
                """,
                (self.run_id, strategy_id),
            ).fetchall()
        ]

    def commit(self) -> None:
        self.conn.commit()
