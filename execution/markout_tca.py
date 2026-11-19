"""Closed-Loop Post-Fill 5s/60s Markout TCA Store & Pre-Trade Toxicity Feedback (Step 2A).

Records post-fill mid-price evolution at 5-second and 60-second horizons into SQLite
(``tca_markout_observations``) and exposes adaptive pre-trade size/quote adjustments
so toxic symbols experiencing chronic adverse selection automatically throttle size
or widen passive maker offsets.

Sign convention:
- ``side_sign = +1`` for ``BUY``, ``-1`` for ``SELL``.
- ``markout_5s_bps = side_sign * ((mid_5s - fill_price) / fill_price) * 10_000``
  - Negative ``markout_*_bps < 0`` means the market moved *against* our fill
    immediately after execution (adverse selection / informed toxic flow).
  - Positive ``markout_*_bps > 0`` means favorable post-fill drift.
- ``adverse_cost_5s_bps = -markout_5s_bps`` (positive = adverse slippage cost).

Default runtime state: pre-trade feedback is OFF unless ``LIVE_BNB_MARKOUT_FEEDBACK_ENABLED=1``.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from execution.slippage_report import summarize

ENV_MARKOUT_FEEDBACK_ENABLED = "LIVE_BNB_MARKOUT_FEEDBACK_ENABLED"
HORIZON_5S_MS = 5_000
HORIZON_60S_MS = 60_000
TOXIC_THRESHOLD_BPS = -5.0
EXTREME_TOXIC_BLOCK_BPS = -25.0


def markout_feedback_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_MARKOUT_FEEDBACK_ENABLED) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def compute_markout_bps(*, side: str, fill_price: float, future_mid_price: float) -> float | None:
    if (
        not math.isfinite(fill_price)
        or not math.isfinite(future_mid_price)
        or fill_price <= 0.0
        or future_mid_price <= 0.0
    ):
        return None
    clean_side = str(side or "").strip().upper()
    if clean_side == "BUY":
        sign = 1.0
    elif clean_side == "SELL":
        sign = -1.0
    else:
        return None
    return round(sign * ((future_mid_price - fill_price) / fill_price) * 10_000.0, 4)


@dataclass(frozen=True)
class MarkoutAdaptiveAdjustment:
    symbol: str
    samples: int
    mean_markout_5s_bps: float | None
    mean_markout_60s_bps: float | None
    toxic_fill_rate_5s: float | None
    size_multiplier: float
    extra_maker_offset_bps: float
    block_reason: str | None
    applied: bool


class MarkoutTcaStore:
    """SQLite persistence & horizon resolver for 5s and 60s post-fill markouts."""

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS tca_markout_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_log_id INTEGER,
        order_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        purpose TEXT NOT NULL DEFAULT 'live_bnb_boll_fade',
        side TEXT NOT NULL,
        order_type TEXT NOT NULL DEFAULT 'MARKET',
        fill_price REAL NOT NULL,
        fill_qty REAL NOT NULL,
        fill_ts_ms INTEGER NOT NULL,
        mid_at_fill REAL,
        mid_5s REAL,
        mid_60s REAL,
        markout_5s_bps REAL,
        markout_60s_bps REAL,
        UNIQUE(symbol, order_id)
    );
    CREATE INDEX IF NOT EXISTS idx_tca_markout_symbol_ts
        ON tca_markout_observations (symbol, fill_ts_ms DESC);
    """

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(self.SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    def record_fill(
        self,
        *,
        symbol: str,
        order_id: str,
        side: str,
        fill_price: float,
        fill_qty: float,
        fill_ts_ms: int,
        decision_log_id: int | None = None,
        purpose: str = "live_bnb_boll_fade",
        order_type: str = "MARKET",
        mid_at_fill: float | None = None,
        mid_5s: float | None = None,
        mid_60s: float | None = None,
    ) -> None:
        clean_sym = str(symbol or "").strip().upper()
        clean_side = str(side or "").strip().upper()
        if not clean_sym or clean_side not in {"BUY", "SELL"} or fill_price <= 0 or fill_qty <= 0:
            raise ValueError("invalid_markout_fill_record")
        m5 = (
            compute_markout_bps(side=clean_side, fill_price=fill_price, future_mid_price=mid_5s)
            if mid_5s is not None
            else None
        )
        m60 = (
            compute_markout_bps(side=clean_side, fill_price=fill_price, future_mid_price=mid_60s)
            if mid_60s is not None
            else None
        )
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.execute(
                """
                INSERT INTO tca_markout_observations (
                    decision_log_id, order_id, symbol, purpose, side, order_type,
                    fill_price, fill_qty, fill_ts_ms, mid_at_fill,
                    mid_5s, mid_60s, markout_5s_bps, markout_60s_bps
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, order_id) DO UPDATE SET
                    mid_5s = COALESCE(excluded.mid_5s, tca_markout_observations.mid_5s),
                    mid_60s = COALESCE(excluded.mid_60s, tca_markout_observations.mid_60s),
                    markout_5s_bps = COALESCE(excluded.markout_5s_bps, tca_markout_observations.markout_5s_bps),
                    markout_60s_bps = COALESCE(excluded.markout_60s_bps, tca_markout_observations.markout_60s_bps)
                """,
                (
                    decision_log_id,
                    str(order_id).strip(),
                    clean_sym,
                    str(purpose or "live_bnb_boll_fade").strip(),
                    clean_side,
                    str(order_type or "MARKET").strip().upper(),
                    float(fill_price),
                    float(fill_qty),
                    int(fill_ts_ms),
                    float(mid_at_fill) if mid_at_fill is not None else None,
                    float(mid_5s) if mid_5s is not None else None,
                    float(mid_60s) if mid_60s is not None else None,
                    m5,
                    m60,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def observe_mid_price(self, *, symbol: str, mid_price: float, ts_ms: int) -> int:
        """Resolve pending 5s and 60s markout slots for ``symbol`` once horizon elapses."""
        clean_sym = str(symbol or "").strip().upper()
        if not clean_sym or not math.isfinite(mid_price) or mid_price <= 0:
            return 0
        now_ms = int(ts_ms)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        updated = 0
        try:
            pending = conn.execute(
                """
                SELECT id, side, fill_price, fill_ts_ms, mid_5s, mid_60s
                FROM tca_markout_observations
                WHERE symbol = ? AND (mid_5s IS NULL OR mid_60s IS NULL)
                """,
                (clean_sym,),
            ).fetchall()
            for row in pending:
                elapsed = now_ms - int(row["fill_ts_ms"])
                fill_px = float(row["fill_price"])
                side = str(row["side"])
                new_m5 = None
                new_m60 = None
                set_clauses: list[str] = []
                vals: list[Any] = []
                if row["mid_5s"] is None and elapsed >= HORIZON_5S_MS:
                    new_m5 = compute_markout_bps(
                        side=side, fill_price=fill_px, future_mid_price=mid_price
                    )
                    set_clauses.extend(["mid_5s = ?", "markout_5s_bps = ?"])
                    vals.extend([float(mid_price), new_m5])
                if row["mid_60s"] is None and elapsed >= HORIZON_60S_MS:
                    new_m60 = compute_markout_bps(
                        side=side, fill_price=fill_px, future_mid_price=mid_price
                    )
                    set_clauses.extend(["mid_60s = ?", "markout_60s_bps = ?"])
                    vals.extend([float(mid_price), new_m60])
                if set_clauses:
                    vals.append(int(row["id"]))
                    conn.execute(
                        f"UPDATE tca_markout_observations SET {', '.join(set_clauses)} WHERE id = ?",
                        vals,
                    )
                    updated += 1
            if updated:
                conn.commit()
        finally:
            conn.close()
        return updated


def read_markout_summary_ro(ledger_path: Path) -> dict[str, Any]:
    """Read-only summary of 5s/60s markout toxicity from ``ledger_path``."""
    empty = {
        "observations_total": 0,
        "markout_5s_bps": summarize([]),
        "markout_60s_bps": summarize([]),
        "toxic_fill_rate_5s": None,
        "by_symbol": {},
    }
    if not ledger_path.is_file():
        return empty
    conn = sqlite3.connect(f"file:{ledger_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "tca_markout_observations" not in tables:
            return empty
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM tca_markout_observations ORDER BY fill_ts_ms ASC, id ASC"
            ).fetchall()
        ]
    finally:
        conn.close()

    m5_all: list[float] = []
    m60_all: list[float] = []
    by_sym_raw: dict[str, dict[str, list[float]]] = {}

    for row in rows:
        sym = str(row.get("symbol") or "UNKNOWN")
        bucket = by_sym_raw.setdefault(sym, {"m5": [], "m60": []})
        v5 = row.get("markout_5s_bps")
        if isinstance(v5, (int, float)) and not isinstance(v5, bool) and math.isfinite(float(v5)):
            m5_all.append(float(v5))
            bucket["m5"].append(float(v5))
        v60 = row.get("markout_60s_bps")
        if isinstance(v60, (int, float)) and not isinstance(v60, bool) and math.isfinite(float(v60)):
            m60_all.append(float(v60))
            bucket["m60"].append(float(v60))

    by_symbol: dict[str, Any] = {}
    for sym in sorted(by_sym_raw):
        s5 = by_sym_raw[sym]["m5"]
        s60 = by_sym_raw[sym]["m60"]
        tox_rate = (
            round(sum(1 for x in s5 if x <= TOXIC_THRESHOLD_BPS) / len(s5), 4) if s5 else None
        )
        by_symbol[sym] = {
            "n_5s": len(s5),
            "mean_markout_5s_bps": round(sum(s5) / len(s5), 3) if s5 else None,
            "mean_markout_60s_bps": round(sum(s60) / len(s60), 3) if s60 else None,
            "toxic_fill_rate_5s": tox_rate,
        }

    global_tox_rate = (
        round(sum(1 for x in m5_all if x <= TOXIC_THRESHOLD_BPS) / len(m5_all), 4)
        if m5_all
        else None
    )
    return {
        "observations_total": len(rows),
        "markout_5s_bps": summarize(m5_all),
        "markout_60s_bps": summarize(m60_all),
        "toxic_fill_rate_5s": global_tox_rate,
        "by_symbol": by_symbol,
    }


def compute_markout_adaptive_adjustment(
    ledger_path: Path,
    *,
    symbol: str,
    lookback_fills: int = 20,
    min_samples: int = 3,
    enabled: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> MarkoutAdaptiveAdjustment:
    """Closed-loop pre-trade size & passive offset feedback from symbol markout history."""
    clean_sym = str(symbol or "").strip().upper()
    is_on = markout_feedback_enabled(environ) if enabled is None else bool(enabled)
    m5_vals: list[float] = []
    m60_vals: list[float] = []

    if ledger_path.is_file() and clean_sym:
        conn = sqlite3.connect(
            f"file:{ledger_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0
        )
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "tca_markout_observations" in tables:
                rows = conn.execute(
                    """
                    SELECT markout_5s_bps, markout_60s_bps
                    FROM tca_markout_observations
                    WHERE symbol = ? AND markout_5s_bps IS NOT NULL
                    ORDER BY fill_ts_ms DESC, id DESC
                    LIMIT ?
                    """,
                    (clean_sym, max(1, int(lookback_fills))),
                ).fetchall()
                for r in rows:
                    v5 = r["markout_5s_bps"]
                    if isinstance(v5, (int, float)) and math.isfinite(float(v5)):
                        m5_vals.append(float(v5))
                    v60 = r["markout_60s_bps"]
                    if isinstance(v60, (int, float)) and math.isfinite(float(v60)):
                        m60_vals.append(float(v60))
        finally:
            conn.close()

    n = len(m5_vals)
    mean_5s = round(sum(m5_vals) / n, 3) if n > 0 else None
    mean_60s = round(sum(m60_vals) / len(m60_vals), 3) if m60_vals else None
    tox_rate = (
        round(sum(1 for x in m5_vals if x <= TOXIC_THRESHOLD_BPS) / n, 4) if n > 0 else None
    )

    if not is_on or n < int(min_samples) or mean_5s is None:
        return MarkoutAdaptiveAdjustment(
            symbol=clean_sym,
            samples=n,
            mean_markout_5s_bps=mean_5s,
            mean_markout_60s_bps=mean_60s,
            toxic_fill_rate_5s=tox_rate,
            size_multiplier=1.0,
            extra_maker_offset_bps=0.0,
            block_reason=None,
            applied=False,
        )

    effective_markout = (
        min(mean_5s, mean_60s) if mean_60s is not None else mean_5s
    )
    if effective_markout <= EXTREME_TOXIC_BLOCK_BPS and (tox_rate or 0.0) >= 0.60:
        return MarkoutAdaptiveAdjustment(
            symbol=clean_sym,
            samples=n,
            mean_markout_5s_bps=mean_5s,
            mean_markout_60s_bps=mean_60s,
            toxic_fill_rate_5s=tox_rate,
            size_multiplier=0.0,
            extra_maker_offset_bps=15.0,
            block_reason=f"markout_toxicity_halt:{clean_sym}",
            applied=True,
        )

    if effective_markout <= TOXIC_THRESHOLD_BPS:
        severity = min(1.0, abs(effective_markout - TOXIC_THRESHOLD_BPS) / 20.0)
        size_mult = round(max(0.50, 1.0 - 0.50 * severity), 4)
        offset_bps = round(min(12.0, 2.0 + 10.0 * severity), 2)
        return MarkoutAdaptiveAdjustment(
            symbol=clean_sym,
            samples=n,
            mean_markout_5s_bps=mean_5s,
            mean_markout_60s_bps=mean_60s,
            toxic_fill_rate_5s=tox_rate,
            size_multiplier=size_mult,
            extra_maker_offset_bps=offset_bps,
            block_reason=None,
            applied=True,
        )

    return MarkoutAdaptiveAdjustment(
        symbol=clean_sym,
        samples=n,
        mean_markout_5s_bps=mean_5s,
        mean_markout_60s_bps=mean_60s,
        toxic_fill_rate_5s=tox_rate,
        size_multiplier=1.0,
        extra_maker_offset_bps=0.0,
        block_reason=None,
        applied=True,
    )
