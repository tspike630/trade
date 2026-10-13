"""Read-only SQLite adapter over token_indicators (and optional pair snapshots)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from logging_setup import get_logger

log = get_logger("decision_engine.data_adapter")

# Columns we care about for Step 1 strategies
_INDICATOR_COLS = (
    "chain_id",
    "token_address",
    "change_5m",
    "change_15m",
    "change_1h",
    "change_4h",
    "change_24h",
    "volatility_24h",
    "volume_strength",
    "jev_raw_score",
    "jev_score",
    "jev_signal",
    "jev_confidence",
    "jev_strength",
    "tier",
    "primary_pair_address",
    "updated_at",
    # 3-3: technical columns. Missing columns are dropped by _available_cols.
    # token_indicators stores bands as boll_upper / boll_lower (not boll_up / boll_low).
    "rsi_14",
    "macd",
    "macd_signal",
    "boll_mid",
    "boll_upper",
    "boll_lower",
)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


class IndicatorDataAdapter:
    """
    Open DB read-only (URI mode=ro). Never writes indicator / crawl tables.
    decision_log writes go through SignalBus with a separate RW connection.
    """

    def __init__(self, db_path: str | Path, *, use_ro_uri: bool = True):
        self.db_path = Path(db_path).resolve()
        self._use_ro_uri = use_ro_uri
        self._conn: sqlite3.Connection | None = None
        self._open()

    def _open(self) -> None:
        if not self.db_path.is_file():
            log.warning("db missing path=%s (queries will return empty)", self.db_path)
            # Still open; SQLite creates empty file for non-URI — avoid that with URI
            # Use ro URI which fails if file missing; fall back to empty in-memory
            try:
                uri = f"file:{self.db_path}?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True, timeout=10.0)
            except sqlite3.OperationalError:
                self._conn = sqlite3.connect(":memory:")
                log.warning("opened in-memory stub; no indicator rows available")
                return
        else:
            try:
                if self._use_ro_uri:
                    uri = f"file:{self.db_path}?mode=ro"
                    self._conn = sqlite3.connect(uri, uri=True, timeout=10.0)
                else:
                    self._conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            except sqlite3.OperationalError as exc:
                log.warning("ro URI failed (%s); falling back to normal SELECT-only conn", exc)
                self._conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        assert self._conn is not None
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        log.info("data_adapter open ro db=%s", self.db_path)

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "IndicatorDataAdapter":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None

    def _available_cols(self, table: str) -> tuple[str, ...]:
        """PRAGMA filter: only columns that actually exist on the table."""
        if not self._table_exists(table):
            return ()
        try:
            cols = {
                str(r["name"])
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        except sqlite3.OperationalError:
            return ()
        return tuple(c for c in _INDICATOR_COLS if c in cols)

    def count_indicators(self) -> int:
        if not self._table_exists("token_indicators"):
            return 0
        row = self.conn.execute("SELECT COUNT(*) AS n FROM token_indicators").fetchone()
        return int(row["n"]) if row else 0

    def list_hot_tokens(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        Prefer tier='hot' ordered by jev_strength DESC NULLS LAST;
        if no hot rows, fall back to top by jev_strength globally.
        """
        if not self._table_exists("token_indicators"):
            log.warning("token_indicators table missing")
            return []
        cols = ", ".join(self._available_cols("token_indicators"))
        if not cols:
            log.warning("token_indicators has no usable columns")
            return []
        limit = max(1, int(limit))
        sql_hot = f"""
            SELECT {cols}
            FROM token_indicators
            WHERE tier = 'hot'
            ORDER BY COALESCE(jev_strength, -1) DESC, updated_at DESC
            LIMIT ?
        """
        rows = self.conn.execute(sql_hot, (limit,)).fetchall()
        if not rows:
            sql_any = f"""
                SELECT {cols}
                FROM token_indicators
                ORDER BY COALESCE(jev_strength, -1) DESC, updated_at DESC
                LIMIT ?
            """
            rows = self.conn.execute(sql_any, (limit,)).fetchall()
            log.info("list_hot_tokens: no hot tier; fallback top-%s by jev_strength (%s rows)", limit, len(rows))
        else:
            log.info("list_hot_tokens: hot tier limit=%s got=%s", limit, len(rows))
        return [dict(r) for r in rows]

    def get_indicators(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        if not self._table_exists("token_indicators"):
            return None
        cols = ", ".join(self._available_cols("token_indicators"))
        if not cols:
            return None
        row = self.conn.execute(
            f"SELECT {cols} FROM token_indicators WHERE chain_id=? AND token_address=? LIMIT 1",
            (chain_id, token_address),
        ).fetchone()
        return _row_to_dict(row)

    def get_pair_snapshot(
        self, chain_id: str, pair_address: str
    ) -> dict[str, Any] | None:
        """Optional: latest pair row + light price/liq from raw_json if present."""
        if not self._table_exists("pairs"):
            return None
        row = self.conn.execute(
            """
            SELECT chain_id, pair_address, token_address, raw_json, updated_at
            FROM pairs
            WHERE chain_id=? AND pair_address=?
            LIMIT 1
            """,
            (chain_id, pair_address),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        raw = out.get("raw_json")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                out["price_usd"] = parsed.get("priceUsd")
                liq = parsed.get("liquidity") or {}
                if isinstance(liq, dict):
                    out["liquidity_usd"] = liq.get("usd")
                else:
                    out["liquidity_usd"] = None
            except (json.JSONDecodeError, TypeError):
                out["price_usd"] = None
                out["liquidity_usd"] = None
        return out
