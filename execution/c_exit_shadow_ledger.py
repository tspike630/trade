"""Independent C-exit shadow paper book (eight grid tags, one SQLite).

Never writes paper_trading.db or execution_ledger.db (main KPI).
Cash and positions are partitioned by grid_tag.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution.c_exit_grid import (
    DAILY_LOSS_CAP_USD,
    ROLLBACK_CUMULATIVE_USD,
    ROLLBACK_SINGLE_USD,
    cell_exit_action,
    cells,
)
from execution import c_exit_size_mult as size_mult

BUSY_TIMEOUT_MS = 30_000
DEFAULT_START_CASH = 10_000.0


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


GAP_STOP_MULT = 2.0


def is_gap_stop(pnl_usd: float, size_usd: float, sl_pct: float | None) -> bool:
    """Actual loss rate > 2x configured SL (e.g. SL5 but realized -64%..-99.9%)."""
    if not sl_pct or size_usd <= 0 or pnl_usd >= 0:
        return False
    return (-float(pnl_usd) / float(size_usd)) * 100.0 > GAP_STOP_MULT * float(sl_pct)


class CExitShadowLedger:
    """Four-tag shadow ledger in paper_trading_c_exit.db."""

    def __init__(
        self,
        path: str | Path,
        *,
        start_cash: float = DEFAULT_START_CASH,
        daily_loss_cap: float = DAILY_LOSS_CAP_USD,
    ) -> None:
        self.path = Path(path)
        self.start_cash = float(start_cash)
        self.daily_loss_cap = float(daily_loss_cap)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path),
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._init()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CExitShadowLedger":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS grid_account (
                tag TEXT PRIMARY KEY,
                cash_usd REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                grid_tag TEXT NOT NULL,
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
                meta_json TEXT,
                source_decision_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_c_exit_pos_tag_status
                ON positions (grid_tag, status);
            CREATE INDEX IF NOT EXISTS idx_c_exit_pos_open_pair
                ON positions (grid_tag, chain_id, pair_address, status);
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT,
                grid_tag TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                qty REAL NOT NULL,
                notional_usd REAL NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS shadow_cursor (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(positions)")}
        for col, ddl in (
            ("trail_pct", "REAL"),
            ("partial_tp_pct", "REAL"),
            ("partial_frac", "REAL"),
            ("partial_done", "INTEGER NOT NULL DEFAULT 0"),
            ("peak_price", "REAL"),
            ("orig_qty", "REAL"),
            ("proceeds_usd", "REAL NOT NULL DEFAULT 0"),
            ("size_mult", "REAL NOT NULL DEFAULT 1.0"),
            ("gap_stop", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {ddl}")
        size_mult.ensure_schema(self.conn)
        now = _utc()
        for cell in cells():
            row = self.conn.execute(
                "SELECT 1 FROM grid_account WHERE tag=?", (cell.tag,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO grid_account (tag, cash_usd, updated_at) VALUES (?,?,?)",
                    (cell.tag, self.start_cash, now),
                )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO shadow_meta (key, value, updated_at)
            VALUES ('daily_loss_cap_usd', ?, ?)
            """,
            (str(self.daily_loss_cap), now),
        )
        self.conn.execute(
            """
            INSERT OR IGNORE INTO shadow_meta (key, value, updated_at)
            VALUES ('halted', '0', ?)
            """,
            (now,),
        )

    def get_cash(self, tag: str) -> float:
        row = self.conn.execute(
            "SELECT cash_usd FROM grid_account WHERE tag=?", (tag,)
        ).fetchone()
        if row is None:
            raise KeyError(tag)
        return float(row["cash_usd"])

    def get_last_processed_id(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM shadow_cursor WHERE key='last_processed_id'"
        ).fetchone()
        if not row:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    def set_last_processed_id(self, decision_log_id: int) -> None:
        now = _utc()
        cur = self.get_last_processed_id()
        if int(decision_log_id) <= cur:
            return
        self.conn.execute(
            """
            INSERT INTO shadow_cursor (key, value, updated_at) VALUES ('last_processed_id', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (str(int(decision_log_id)), now),
        )

    def is_halted(self) -> bool:
        row = self.conn.execute(
            "SELECT value FROM shadow_meta WHERE key='halted'"
        ).fetchone()
        return bool(row and str(row["value"]) in ("1", "true", "yes"))

    def halt(self, reason: str) -> None:
        now = _utc()
        self.conn.execute(
            """
            INSERT INTO shadow_meta (key, value, updated_at) VALUES ('halted', '1', ?)
            ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at
            """,
            (now,),
        )
        self.conn.execute(
            """
            INSERT INTO shadow_meta (key, value, updated_at) VALUES ('halt_reason', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (reason, now),
        )

    def realized_pnl_total(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM positions WHERE status='closed'"
        ).fetchone()
        return float(row["s"])

    def realized_pnl_today_hkt(self) -> float:
        """Asia/Hong_Kong calendar day realized pnl (all tags)."""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:  # pragma: no cover
            ZoneInfo = None  # type: ignore
        if ZoneInfo is None:
            return 0.0
        now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        day = now.strftime("%Y-%m-%d")
        # closed_at stored as UTC Z; compare by converting roughly via date string
        # Use closed_at date in HKT: closed_at >= day 00:00 HKT as UTC.
        start_hkt = datetime(now.year, now.month, now.day, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        start_utc = start_hkt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(pnl_usd),0) AS s
            FROM positions
            WHERE status='closed' AND closed_at >= ?
            """,
            (start_utc,),
        ).fetchone()
        return float(row["s"])

    def daily_loss_breached(self) -> bool:
        pnl = self.realized_pnl_today_hkt()
        return pnl < -abs(self.daily_loss_cap)

    def open_for_all_grids(
        self,
        *,
        chain_id: str,
        pair_address: str,
        token_address: str | None,
        entry_price: float,
        size_usd: float,
        source_decision_id: int | None,
        meta: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Open the same signal into every grid tag (independent cash)."""
        if self.is_halted():
            return [{"ok": False, "error": "shadow_halted"}]
        if self.daily_loss_breached():
            self.halt("daily_loss_cap")
            return [{"ok": False, "error": "daily_loss_cap"}]
        out: list[dict[str, Any]] = []
        for cell in cells():
            try:
                pid = self._open_one(
                    tag=cell.tag,
                    chain_id=chain_id,
                    pair_address=pair_address,
                    token_address=token_address,
                    entry_price=entry_price,
                    size_usd=size_usd,
                    tp_pct=cell.tp_pct,
                    sl_pct=cell.sl_pct,
                    max_hold_min=cell.max_hold_min,
                    source_decision_id=source_decision_id,
                    meta=meta,
                    trail_pct=cell.trail_pct,
                    partial_tp_pct=cell.partial_tp_pct,
                    partial_frac=cell.partial_frac,
                )
                out.append({"ok": True, "tag": cell.tag, "position_id": pid})
            except Exception as exc:  # noqa: BLE001
                out.append({"ok": False, "tag": cell.tag, "error": str(exc)})
        return out

    def _open_one(
        self,
        *,
        tag: str,
        chain_id: str,
        pair_address: str,
        token_address: str | None,
        entry_price: float,
        size_usd: float,
        tp_pct: float,
        sl_pct: float,
        max_hold_min: float,
        source_decision_id: int | None,
        meta: dict[str, Any] | None,
        trail_pct: float | None = None,
        partial_tp_pct: float | None = None,
        partial_frac: float = 0.0,
        mult: float = 1.0,
    ) -> str:
        if entry_price <= 0 or size_usd <= 0:
            raise ValueError("price/size must be > 0")
        qty = size_usd / entry_price
        pid = str(uuid.uuid4())
        now = _utc()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cash = self.get_cash(tag)
            if size_usd > cash:
                raise ValueError(f"insufficient cash {cash} < {size_usd} tag={tag}")
            self.conn.execute(
                "UPDATE grid_account SET cash_usd = cash_usd - ?, updated_at = ? WHERE tag = ?",
                (float(size_usd), now, tag),
            )
            meta_obj = dict(meta or {})
            meta_obj["grid_tag"] = tag
            self.conn.execute(
                """
                INSERT INTO positions (
                    id, grid_tag, chain_id, pair_address, token_address, qty, entry_price,
                    size_usd, opened_at, status, tp_pct, sl_pct, max_hold_min, meta_json,
                    source_decision_id, trail_pct, partial_tp_pct, partial_frac,
                    peak_price, orig_qty, size_mult
                ) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    pid,
                    tag,
                    chain_id,
                    pair_address,
                    token_address,
                    qty,
                    entry_price,
                    size_usd,
                    now,
                    tp_pct,
                    sl_pct,
                    max_hold_min,
                    json.dumps(meta_obj),
                    source_decision_id,
                    trail_pct,
                    partial_tp_pct,
                    float(partial_frac or 0.0),
                    entry_price,
                    qty,
                    float(mult),
                ),
            )
            self.conn.execute(
                """
                INSERT INTO fills (position_id, grid_tag, side, price, qty, notional_usd, created_at, note)
                VALUES (?, ?, 'buy', ?, ?, ?, ?, 'open')
                """,
                (pid, tag, entry_price, qty, size_usd, now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self._rollback()
            raise
        return pid

    def list_open(self, tag: str | None = None) -> list[dict[str, Any]]:
        if tag:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open' AND grid_tag=?",
                (tag,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
        return [dict(r) for r in rows]

    def close_position(self, pid: str, *, price: float, reason: str) -> dict[str, Any]:
        now = _utc()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT * FROM positions WHERE id=?", (pid,)
            ).fetchone()
            if not row or row["status"] != "open":
                raise ValueError("position not open")
            qty = float(row["qty"])
            notional = qty * price
            proceeds_prev = float(row["proceeds_usd"] or 0.0)
            pnl = proceeds_prev + notional - float(row["size_usd"])
            tag = row["grid_tag"]
            gap = is_gap_stop(pnl, float(row["size_usd"]), row["sl_pct"])
            cur = self.conn.execute(
                """
                UPDATE positions SET status='closed', exit_price=?, closed_at=?,
                       exit_reason=?, pnl_usd=?, gap_stop=?
                WHERE id=? AND status='open'
                """,
                (price, now, reason, pnl, 1 if gap else 0, pid),
            )
            if cur.rowcount != 1:
                raise ValueError("position not open")
            self.conn.execute(
                "UPDATE grid_account SET cash_usd = cash_usd + ?, updated_at = ? WHERE tag = ?",
                (notional, now, tag),
            )
            self.conn.execute(
                """
                INSERT INTO fills (position_id, grid_tag, side, price, qty, notional_usd, created_at, note)
                VALUES (?, ?, 'sell', ?, ?, ?, ?, ?)
                """,
                (pid, tag, price, qty, notional, now, reason),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self._rollback()
            raise

        size_mult.after_close(self.conn, tag, pnl)
        # rollback checks after commit
        if pnl <= ROLLBACK_SINGLE_USD:
            self.halt(f"single_loss:{pnl:.2f}")
        elif self.realized_pnl_total() <= ROLLBACK_CUMULATIVE_USD:
            self.halt(f"cumulative:{self.realized_pnl_total():.2f}")
        elif self.daily_loss_breached():
            self.halt("daily_loss_cap")

        return {"id": pid, "pnl_usd": pnl, "exit_price": price, "reason": reason, "grid_tag": tag, "gap_stop": gap}

    def partial_close(self, pid: str, *, price: float, reason: str) -> dict[str, Any]:
        """Sell partial_frac of original qty once; remainder stays open."""
        now = _utc()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
            if not row or row["status"] != "open" or int(row["partial_done"] or 0):
                raise ValueError("partial not allowed")
            orig = float(row["orig_qty"] or row["qty"])
            sell_qty = min(orig * float(row["partial_frac"] or 0.0), float(row["qty"]))
            if sell_qty <= 0:
                raise ValueError("partial qty 0")
            notional = sell_qty * price
            tag = row["grid_tag"]
            self.conn.execute(
                """
                UPDATE positions SET qty = qty - ?, partial_done = 1,
                       proceeds_usd = COALESCE(proceeds_usd,0) + ?
                WHERE id=? AND status='open'
                """,
                (sell_qty, notional, pid),
            )
            self.conn.execute(
                "UPDATE grid_account SET cash_usd = cash_usd + ?, updated_at = ? WHERE tag = ?",
                (notional, now, tag),
            )
            self.conn.execute(
                """
                INSERT INTO fills (position_id, grid_tag, side, price, qty, notional_usd, created_at, note)
                VALUES (?, ?, 'sell', ?, ?, ?, ?, ?)
                """,
                (pid, tag, price, sell_qty, notional, now, reason),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self._rollback()
            raise
        return {"id": pid, "partial": True, "qty": sell_qty, "price": price, "grid_tag": tag}

    def tick(self, price_lookup, now: datetime | None = None) -> list[dict[str, Any]]:
        """Apply per-row SL / trailing / partial TP / TP / max_hold."""
        if self.is_halted():
            return []
        closed: list[dict[str, Any]] = []
        now = now or datetime.now(timezone.utc)
        for pos in self.list_open():
            px = price_lookup(pos)
            if px is None:
                continue
            px = float(px)
            peak = max(float(pos.get("peak_price") or pos["entry_price"]), px)
            if pos.get("trail_pct") is not None and peak > float(pos.get("peak_price") or 0):
                self.conn.execute(
                    "UPDATE positions SET peak_price=? WHERE id=?", (peak, pos["id"])
                )
            act = cell_exit_action(
                pos, px, now, peak_price=peak, partial_done=bool(pos.get("partial_done"))
            )
            if act is None:
                continue
            kind, reason = act
            try:
                if kind == "partial":
                    self.partial_close(pos["id"], price=px, reason=reason)
                    continue
                # sell fill ≈ mid (shadow skeleton)
                result = self.close_position(pos["id"], price=px, reason=reason)
                closed.append(result)
            except Exception:
                continue
            if self.is_halted():
                break
        return closed

    def status(self) -> dict[str, Any]:
        tags = {}
        for cell in cells():
            cash = self.get_cash(cell.tag)
            n_open = self.conn.execute(
                "SELECT COUNT(*) AS c FROM positions WHERE grid_tag=? AND status='open'",
                (cell.tag,),
            ).fetchone()["c"]
            n_closed = self.conn.execute(
                "SELECT COUNT(*) AS c FROM positions WHERE grid_tag=? AND status='closed'",
                (cell.tag,),
            ).fetchone()["c"]
            pnl = self.conn.execute(
                "SELECT COALESCE(SUM(pnl_usd),0) AS s FROM positions WHERE grid_tag=? AND status='closed'",
                (cell.tag,),
            ).fetchone()["s"]
            tags[cell.tag] = {
                "cash_usd": cash,
                "n_open": n_open,
                "n_closed": n_closed,
                "realized_pnl": float(pnl),
                "n_gap_stop": self.conn.execute(
                    "SELECT COUNT(*) c FROM positions WHERE grid_tag=? AND gap_stop=1",
                    (cell.tag,),
                ).fetchone()["c"],
                "size_mult": size_mult.get_state(self.conn, cell.tag),
            }
        return {
            "path": str(self.path),
            "halted": self.is_halted(),
            "cursor": self.get_last_processed_id(),
            "realized_pnl_total": self.realized_pnl_total(),
            "realized_pnl_today_hkt": self.realized_pnl_today_hkt(),
            "daily_loss_cap": self.daily_loss_cap,
            "tags": tags,
            "by_label": self.label_breakdown(),
        }

    def label_breakdown(self) -> list[dict[str, Any]]:
        """Entry label (liq_bucket × x_budget_regime) × grid_tag closed stats."""
        rows = self.conn.execute(
            """
            SELECT grid_tag,
                   COALESCE(json_extract(meta_json,'$.liq_bucket'),'-') AS liq_bucket,
                   COALESCE(json_extract(meta_json,'$.x_budget_regime'),'-') AS regime,
                   SUM(status='closed') AS n_closed, SUM(gap_stop) AS n_gap_stop, SUM(status='open') AS n_open,
                   COALESCE(SUM(CASE WHEN status='closed' THEN pnl_usd END),0) AS pnl
            FROM positions GROUP BY 1,2,3 ORDER BY 1,2,3
            """
        ).fetchall()
        return [dict(r) for r in rows]
