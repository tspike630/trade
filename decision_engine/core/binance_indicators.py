"""Read API for Binance spot tech_boll rows written by indicators.binance_spot.

``live_bnb_boll_fade`` should scan these rows instead of DEX ``token_indicators``.
The table is absent until the kline job has run; callers can treat that as
"side path not installed" and keep the DEX-map bypass. Once the table exists,
it is the book — an empty or stale result is not filled from DEX rows.

``liquidity_usd`` on the returned row is ``quote_volume_24h`` so the existing
``DECISION_MIN_LIQ_USD`` floor still has a USD figure. That is not a new score
and not ``local_score``. ``INDICATORS_WEIGHT_*`` are not read or written.

``OBSERVATION_FIELDS`` are copied when a later indicator build has stored
them. This module does not calculate those columns and does not write the
database. See ``OBSERVATION_READ_CONTRACT``.
"""

from __future__ import annotations

import math
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
    "kline_close_time_ms",
    "tier",
    "tech_updated_at",
    "updated_at",
)

# Present only when a later indicator build or a test added them. #164's
# avg_trade_size is not on main; reading it when the column exists avoids a
# second definition. Missing columns are omitted from the SELECT.
_OPTIONAL_COLS = (
    "kline_low",
    "low",
    "trade_size_ratio",
    "avg_trade_size",
    "avg_trade_size_24h",
    "bar_quote_volume",
    "bar_trades",
    "baseline_quote_volume",
    "baseline_trades",
)

# Nullable observation columns. Upstream writes them; this reader only copies.
# ``trade_size_ratio`` is also in ``_OPTIONAL_COLS`` so an older reader path
# still selects it. The shaped row always carries every name below.
OBSERVATION_FIELDS = (
    "change_7d",
    "volume_7d_avg",
    "taker_buy_quote_volume_5m",
    "buy_ratio_5m",
    "buy_ratio_1h",
    "avg_trade_usd_5m",
    "trade_size_ratio",
    "lower_wick_ratio",
)

OBSERVATION_READ_CONTRACT = """
Observation columns are nullable pass-throughs. A missing column, NULL, a
blank, or a non-finite value stays None. This reader does not compute them
and does not write the database.

change_7d and volume_7d_avg are NULL unless the upstream job has genuine
complete local 7d closed-bar history. A short history is NULL. Do not scale
change_24h by 7 or average a partial window.

taker_buy_quote_volume_5m is the raw kline taker-buy quote (Binance REST
index 10). The base taker field is REST index 9 and is not a reader input.
buy_ratio_5m is that bar's taker quote over quote volume, derived upstream.
buy_ratio_1h is NULL unless there are 12 contiguous closed 5m bars and every
one of them has complete taker quote data. A partial hour is NULL.

avg_trade_usd_5m is the closed 5m bar's quote volume over its trade count.
trade_size_ratio compares that bar with a 24h baseline. The baseline is
sum(quote_volume)/sum(trades) over a fully covered window, not the mean of per-bar averages.
Incomplete coverage is NULL upstream.

lower_wick_ratio keeps the requested field name. The stored formula is
(close-low)/(high-low). That is the close's location inside the high-low
range, not the anatomical lower wick (min(open, close)-low)/(high-low).
This reader does not rewrite a stored value into the anatomical wick.

These fields are not a new entry gate. tech_boll_fade thresholds are
unchanged. A NULL trade_size_ratio still falls through to the existing
avg_trade_size / kline path; a stored finite number remains that coordinate.
"""


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

    Each ``OBSERVATION_FIELDS`` name is always on the shaped row. Absent
    columns and NULL stay None. This function does not insert those columns
    and does not derive a 7d, 12-bar, or trade-size window.
    """
    if not binance_indicators_ready(conn):
        return []
    present = _present_columns(conn)
    cols = _readable_columns(present)
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
    shaped = {
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
        "factor_source": "binance_indicators",
        "venue": BINANCE_VENUE,
        "market": BINANCE_MARKET,
        "universe": CEX_UNIVERSE,
        "source_chain_id": None,
        "source_token_address": None,
    }
    for key in ("kline_close_time_ms", *_OPTIONAL_COLS):
        if key in raw and raw.get(key) is not None:
            shaped[key] = raw.get(key)
    for key in OBSERVATION_FIELDS:
        if key not in raw:
            shaped[key] = None
            continue
        shaped[key] = _optional_number(raw.get(key))
    return shaped


def _readable_columns(present: set[str]) -> list[str]:
    """SELECT list: known names that exist, observation names included once."""
    chosen: list[str] = []
    seen: set[str] = set()
    for name in (*_READ_COLS, *_OPTIONAL_COLS, *OBSERVATION_FIELDS):
        if name not in present or name in seen:
            continue
        chosen.append(name)
        seen.add(name)
    return chosen


def _optional_number(value: Any) -> float | int | None:
    """Finite number, or None. Does not turn a missing value into 0."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        return number
    return None
