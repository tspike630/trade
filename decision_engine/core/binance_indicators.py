"""Read API for Binance spot tech_boll rows written by indicators.binance_spot.

``live_bnb_boll_fade`` should scan these rows instead of DEX ``token_indicators``.
The table is absent until the kline job has run; callers can treat that as
"side path not installed" and keep the DEX-map bypass. Once the table exists,
it is the book — an empty or stale result is not filled from DEX rows.

``liquidity_usd`` on the returned row is ``quote_volume_24h`` so the existing
``DECISION_MIN_LIQ_USD`` floor still has a USD figure. That is not a new score
and not ``local_score``. ``INDICATORS_WEIGHT_*`` are not read or written.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from logging_setup import get_logger

from decision_engine.core.bnb_spot_map import (
    BINANCE_MARKET,
    BINANCE_QUOTE,
    BINANCE_VENUE,
    CEX_UNIVERSE,
)

log = get_logger("decision_engine.binance_indicators")

FactorSource = Literal["auto", "binance", "dex_map"]

_SORT_SQL = {
    "change_5m": "CASE WHEN change_5m IS NULL THEN 1 ELSE 0 END, change_5m ASC",
    "volume_strength": "CASE WHEN volume_strength IS NULL THEN 1 ELSE 0 END, volume_strength DESC",
    "quote_volume_24h": "CASE WHEN quote_volume_24h IS NULL THEN 1 ELSE 0 END, quote_volume_24h DESC",
}

_READ_COLS = (
    "symbol",
    "base_asset",
    "quote_asset",
    "status",
    "interval",
    "price_usd",
    "change_5m",
    "change_15m",
    "change_1h",
    "change_4h",
    "change_24h",
    "volatility_24h",
    "volume_strength",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "quote_volume_24h",
    "sample_count",
    "tier",
    "tech_updated_at",
    "updated_at",
)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_live_bnb_factor_source(raw: str | None) -> FactorSource:
    """``LIVE_BNB_FACTOR_SOURCE``: auto (default), binance, or dex_map."""
    text = (raw or "").strip().lower()
    if text in ("", "auto"):
        return "auto"
    if text == "binance":
        return "binance"
    if text in ("dex_map", "dex-map", "dex"):
        return "dex_map"
    log.warning("unknown LIVE_BNB_FACTOR_SOURCE=%s; using auto", raw)
    return "auto"


def binance_indicators_fresh_sec() -> int:
    raw = os.environ.get("BINANCE_INDICATORS_FRESH_SEC", "3600")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 3600
    return value if value > 0 else 0


def resolve_indicator_sort(raw: str | None) -> str:
    text = (raw or "change_5m").strip().lower()
    if text not in _SORT_SQL:
        log.warning("unknown binance indicator sort %s; using change_5m", raw)
        return "change_5m"
    return text


def binance_indicators_ready(conn: sqlite3.Connection) -> bool:
    """True when the side table exists. Does not create it."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'binance_indicators'"
        ).fetchone()
    except sqlite3.Error as exc:
        log.warning("binance_indicators probe failed: %s", exc)
        return False
    return row is not None


def _present_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute("PRAGMA table_info(binance_indicators)").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def list_binance_hot_tokens(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    sort: str | None = None,
    fresh_sec: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Hot rows shaped for ``TechBollFadeStrategy`` and the Binance spot stamp.

    Ordering is a scan window, not an entry gate. The strategy still applies
    its own change / volume / band thresholds. Missing table → ``[]``.
    """
    if not binance_indicators_ready(conn):
        return []
    present = _present_columns(conn)
    cols = [name for name in _READ_COLS if name in present]
    if "symbol" not in cols:
        return []
    chosen = resolve_indicator_sort(
        sort if sort is not None else os.environ.get("BINANCE_INDICATORS_SORT")
    )
    window = binance_indicators_fresh_sec() if fresh_sec is None else int(fresh_sec)
    where = ["1 = 1"]
    params: list[Any] = []
    if "status" in present:
        where.append("(status IS NULL OR UPPER(status) = 'TRADING')")
    if window > 0 and "tech_updated_at" in present:
        moment = now or datetime.now(timezone.utc)
        cutoff = _iso_z(moment - timedelta(seconds=window))
        where.append("tech_updated_at IS NOT NULL AND tech_updated_at >= ?")
        params.append(cutoff)
    sql = (
        f"SELECT {', '.join(cols)} FROM binance_indicators "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {_SORT_SQL[chosen]} "
        "LIMIT ?"
    )
    params.append(max(1, int(limit)))
    try:
        cur = conn.execute(sql, params)
        names = [str(item[0]) for item in cur.description or ()]
        fetched = [dict(zip(names, row)) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        log.warning("binance_indicators read failed: %s", exc)
        return []
    rows: list[dict[str, Any]] = []
    for raw in fetched:
        shaped = _shape_row(raw)
        if shaped is not None:
            rows.append(shaped)
    log.info(
        "list_binance_hot_tokens sort=%s fresh_sec=%s limit=%s got=%s",
        chosen,
        window,
        limit,
        len(rows),
    )
    return rows


def _shape_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol or ":" in symbol or not symbol.endswith("USDT") or symbol == "USDT":
        return None
    base = str(raw.get("base_asset") or "").strip().upper()
    if not base:
        base = symbol[: -len("USDT")]
    quote = str(raw.get("quote_asset") or BINANCE_QUOTE).strip().upper() or BINANCE_QUOTE
    if quote != BINANCE_QUOTE:
        return None
    ccxt_symbol = f"{base}/{BINANCE_QUOTE}"
    quote_volume = raw.get("quote_volume_24h")
    return {
        "chain_id": BINANCE_VENUE,
        "token_address": symbol,
        "symbol": base,
        "base_asset": base,
        "quote_asset": quote,
        "status": raw.get("status") or "TRADING",
        "interval": raw.get("interval"),
        "price_usd": raw.get("price_usd"),
        "price": raw.get("price_usd"),
        "change_5m": raw.get("change_5m"),
        "change_15m": raw.get("change_15m"),
        "change_1h": raw.get("change_1h"),
        "change_4h": raw.get("change_4h"),
        "change_24h": raw.get("change_24h"),
        "volatility_24h": raw.get("volatility_24h"),
        "volume_strength": raw.get("volume_strength"),
        "rsi_14": raw.get("rsi_14"),
        "macd": raw.get("macd"),
        "macd_signal": raw.get("macd_signal"),
        "macd_hist": raw.get("macd_hist"),
        "boll_upper": raw.get("boll_upper"),
        "boll_mid": raw.get("boll_mid"),
        "boll_lower": raw.get("boll_lower"),
        "quote_volume_24h": quote_volume,
        "liquidity_usd": quote_volume,
        "sample_count": raw.get("sample_count"),
        "tier": raw.get("tier") or "hot",
        "tech_updated_at": raw.get("tech_updated_at"),
        "updated_at": raw.get("updated_at"),
        "primary_pair_address": ccxt_symbol,
        "binance_symbol": symbol,
        "ccxt_symbol": ccxt_symbol,
        "venue": BINANCE_VENUE,
        "market": BINANCE_MARKET,
        "universe": CEX_UNIVERSE,
        "source_chain_id": None,
        "source_token_address": None,
    }
