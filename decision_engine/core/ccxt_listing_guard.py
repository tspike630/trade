"""Read-only ccxt listing and spread guard.

``ccxt_listings.first_seen_ts`` is the first time this side channel saw a
symbol. It is not an official listing time. Without an authoritative
timestamp the coin is neither new nor mature, and no buy is blocked.

Five-minute bars become one closed 1h bar only when the same exchange and
the same symbol have 12 consecutive closed 5m opens. A gap or a bar that
has not closed does not count. Binance and ccxt series are not stitched.

A cross-venue wick is shadow-only unless the quote, the clock, the
freshness, and a fee field are all present. Stale, missing, locked, or
broken databases do not block buys. State 1 mean reversion is never blocked.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from decision_engine.core.news_shock_gate import open_readonly, repo_root, table_columns

STEP_MS = 5 * 60 * 1000
BARS_PER_HOUR = 12
MIN_CLOSED_1H = 20
FRESH_MS = 30 * 60 * 1000
# The 2026-11-10 local audit found no Binance series with 20 closed 1h
# (max 7 of 52) and no cross-venue pair with 20 matches inside 5 seconds.
# Short history is missing coverage, not proof of a new listing. Both
# blocks stay off until a current book and a real listing clock exist.
CHASE_BLOCK_ENABLED = False
SPREAD_BLOCK_ENABLED = False
WICK_SYNC_MS = 5_000

CHASE_REGIMES = frozenset(
    {
        "short_squeeze_breakout",
        "cross_asset_momentum_pullback",
    }
)
STATE1 = "positive_gamma_mean_reversion"

_OHLCV = frozenset({"exchange", "symbol", "timeframe", "ts", "open", "close"})
_LISTINGS = frozenset({"exchange", "symbol", "first_seen_ts"})
_TICKS = frozenset({"exchange", "symbol", "ts", "last_price"})


@dataclass(frozen=True)
class CcxtGuardSnapshot:
    availability: str
    detail: str | None
    stale: bool
    db_path: str | None
    listings: tuple[dict[str, Any], ...]
    bars: tuple[dict[str, Any], ...]
    ticks: tuple[dict[str, Any], ...]
    fee_column: bool


def resolve_ccxt_path(explicit: str | Path | None = None) -> Path | None:
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    env = os.environ.get("CCXT_DB_PATH", "").strip()
    if env:
        return Path(env)
    candidate = repo_root() / "ccxt.db"
    return candidate


def _empty(availability: str, detail: str | None, path: Path | None) -> CcxtGuardSnapshot:
    return CcxtGuardSnapshot(
        availability=availability,
        detail=detail,
        stale=True,
        db_path=None if path is None else str(path),
        listings=(),
        bars=(),
        ticks=(),
        fee_column=False,
    )


def load_ccxt_guard(
    path: str | Path | None = None,
    *,
    symbols: Sequence[str] | None = None,
    now_ms: int | None = None,
    timeout_sec: float = 1.0,
) -> CcxtGuardSnapshot:
    source = resolve_ccxt_path(path)
    if source is None:
        return _empty("not_configured", None, None)
    conn, failed, detail = open_readonly(source, timeout_sec)
    if conn is None:
        return _empty(failed or "error", detail, source)
    clock = int(datetime.now(timezone.utc).timestamp() * 1000) if now_ms is None else int(now_ms)
    wanted = [str(item).strip() for item in (symbols or []) if str(item).strip()]
    try:
        ohlcv_cols = table_columns(conn, "ccxt_ohlcv")
        listing_cols = table_columns(conn, "ccxt_listings")
        tick_cols = table_columns(conn, "ccxt_ticks")
        if ohlcv_cols is None or not _OHLCV.issubset(ohlcv_cols):
            return _empty("schema_error", "ccxt_ohlcv missing required columns", source)
        if listing_cols is None or not _LISTINGS.issubset(listing_cols):
            return _empty("schema_error", "ccxt_listings missing required columns", source)
        if tick_cols is None or not _TICKS.issubset(tick_cols):
            return _empty("schema_error", "ccxt_ticks missing required columns", source)
        fee_column = "fee_bps" in tick_cols or "taker_fee_bps" in tick_cols
        official_sql = (
            ", official_listed_at_ms" if "official_listed_at_ms" in listing_cols else ""
        )
        if wanted:
            marks = ",".join("?" for _ in wanted)
            listing_rows = conn.execute(
                f"""
                SELECT exchange, symbol, first_seen_ts{official_sql}
                FROM ccxt_listings
                WHERE symbol IN ({marks})
                """,
                wanted,
            ).fetchall()
            bar_rows = conn.execute(
                f"""
                SELECT exchange, symbol, timeframe, ts
                FROM ccxt_ohlcv
                WHERE timeframe = '5m' AND symbol IN ({marks})
                """,
                wanted,
            ).fetchall()
            tick_rows = conn.execute(
                f"""
                SELECT exchange, symbol, ts, last_price, bid, ask
                FROM ccxt_ticks
                WHERE symbol IN ({marks}) AND ts >= ?
                """,
                (*wanted, clock - FRESH_MS),
            ).fetchall()
        else:
            listing_rows = []
            bar_rows = []
            tick_rows = []
        listings = [dict(row) for row in listing_rows]
        bars = [dict(row) for row in bar_rows]
        ticks = [dict(row) for row in tick_rows]
        newest = max((int(row["ts"]) for row in ticks), default=0)
        bar_newest = max((int(row["ts"]) for row in bars), default=0)
        latest = max(newest, bar_newest)
        stale = latest <= 0 or clock - latest > FRESH_MS
    except sqlite3.Error as exc:
        message = str(exc).lower()
        kind = "locked" if "locked" in message else "timeout" if "interrupt" in message or "busy" in message else "error"
        return _empty(kind, str(exc), source)
    finally:
        conn.close()
    return CcxtGuardSnapshot(
        availability="ok",
        detail=None,
        stale=stale,
        db_path=str(source),
        listings=tuple(listings),
        bars=tuple(bars),
        ticks=tuple(ticks),
        fee_column=fee_column,
    )


def count_contiguous_closed_hours(
    open_times_ms: Sequence[int],
    *,
    now_ms: int,
    listed_at_ms: int | None,
) -> tuple[int, str]:
    """Count closed 1h bars built from exact 12-bar runs.

    Returns ``(hours, status)`` where status is ``contiguous``, ``gapped``,
    or ``none``. A run that does not start at the listing, or that stops
    before the latest closed slot, is gapped and must not be called new
    or mature.
    """
    closed = sorted({int(item) for item in open_times_ms if int(item) + STEP_MS <= now_ms})
    if listed_at_ms is None or not closed:
        return 0, "none"
    if closed[0] - int(listed_at_ms) > STEP_MS:
        return 0, "gapped"
    expected = closed[0]
    for stamp in closed:
        if stamp != expected:
            return 0, "gapped"
        expected += STEP_MS
    if now_ms - (closed[-1] + STEP_MS) > STEP_MS:
        return 0, "gapped"
    return len(closed) // BARS_PER_HOUR, "contiguous"


def _listing_for(snapshot: CcxtGuardSnapshot, symbol: str, exchange: str) -> dict[str, Any] | None:
    matches = [
        row
        for row in snapshot.listings
        if str(row.get("symbol") or "") == symbol and str(row.get("exchange") or "").lower() == exchange
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _opens(snapshot: CcxtGuardSnapshot, symbol: str, exchange: str) -> list[int]:
    return [
        int(row["ts"])
        for row in snapshot.bars
        if str(row.get("symbol") or "") == symbol
        and str(row.get("exchange") or "").lower() == exchange
        and str(row.get("timeframe") or "") == "5m"
    ]


def assess_symbol(
    snapshot: CcxtGuardSnapshot,
    symbol: str,
    *,
    exchange: str,
    now_ms: int,
) -> dict[str, Any]:
    base = {
        "symbol": symbol,
        "exchange": exchange,
        "listing_class": "unknown",
        "block_chase": False,
        "stale": snapshot.stale,
        "availability": snapshot.availability,
    }
    if snapshot.availability != "ok" or snapshot.stale:
        base["reason"] = snapshot.availability if snapshot.availability != "ok" else "stale_ccxt"
        return base
    listing = _listing_for(snapshot, symbol, exchange)
    if listing is None:
        base["reason"] = "no_listing_row"
        return base
    official = listing.get("official_listed_at_ms")
    if official is None or official == "":
        base["reason"] = "first_seen_is_not_official"
        base["first_seen_ts"] = listing.get("first_seen_ts")
        return base
    hours, status = count_contiguous_closed_hours(
        _opens(snapshot, symbol, exchange),
        now_ms=now_ms,
        listed_at_ms=int(official),
    )
    base["closed_1h"] = hours
    base["bar_status"] = status
    if status != "contiguous":
        base["reason"] = "incomplete_5m_run"
        base["listing_class"] = "unknown"
        return base
    if hours < MIN_CLOSED_1H:
        # A short run, including Binance's observed maximum of 7 closed 1h,
        # does not identify a new coin. The file can simply be incomplete.
        base["listing_class"] = "unknown"
        base["reason"] = "coverage_insufficient_not_a_new_listing"
        base["block_chase"] = False
        return base
    base["listing_class"] = "mature"
    base["reason"] = "enough_closed_1h"
    base["block_chase"] = False
    return base


def spread_shadow(snapshot: CcxtGuardSnapshot, symbol: str, *, now_ms: int) -> dict[str, Any]:
    """Compare venues only as a shadow. Blocking stays off without fees and a fresh sync."""
    report = {
        "symbol": symbol,
        "spread_block": False,
        "spread_block_enabled": SPREAD_BLOCK_ENABLED,
        "coverage": "insufficient",
    }
    if snapshot.availability != "ok" or snapshot.stale or not snapshot.fee_column:
        report["reason"] = "stale_or_missing_fee_or_db"
        return report
    fresh = [
        row
        for row in snapshot.ticks
        if str(row.get("symbol") or "") == symbol and now_ms - int(row.get("ts") or 0) <= FRESH_MS
    ]
    by_exchange: dict[str, dict[str, Any]] = {}
    for row in fresh:
        by_exchange[str(row.get("exchange") or "").lower()] = row
    if len(by_exchange) < 2:
        report["reason"] = "fewer_than_two_fresh_venues"
        return report
    quotes = {str(row.get("symbol") or "").split("/")[-1] if "/" in str(row.get("symbol") or "") else "" for row in by_exchange.values()}
    if len(quotes) != 1 or "" in quotes:
        report["reason"] = "quote_not_aligned"
        return report
    stamps = [int(row["ts"]) for row in by_exchange.values()]
    skew = max(stamps) - min(stamps)
    if skew > WICK_SYNC_MS:
        report["reason"] = "not_a_5s_sync_60s_polls_are_not_wicks"
        return report
    # One fresh pair of quotes is not the audit's >=20 matches within 5s.
    report["reason"] = "single_snapshot_is_not_20_synced_prints"
    report["spread_block"] = False
    return report


def apply_ccxt_listing_guard(
    signals: Sequence[Mapping[str, Any]],
    snapshot: CcxtGuardSnapshot | None,
    *,
    now_ms: int | None = None,
    exchange: str = "binance",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Record listing and spread coverage. This build does not drop buys.

    A short closed-1h run is missing coverage, not a new listing. State 1
    stays a mean-reversion buy. ``CHASE_BLOCK_ENABLED`` and
    ``SPREAD_BLOCK_ENABLED`` are both false.
    """
    clock = int(datetime.now(timezone.utc).timestamp() * 1000) if now_ms is None else int(now_ms)
    if snapshot is None:
        snapshot = _empty("not_configured", None, None)
    kept: list[dict[str, Any]] = []
    dropped = 0
    for sig in signals:
        row = dict(sig)
        action = str(row.get("action") or "").strip().lower()
        regime = str(row.get("market_regime") or "")
        symbol = str(row.get("ccxt_symbol") or row.get("binance_symbol") or row.get("symbol") or "")
        assessment = assess_symbol(snapshot, symbol, exchange=exchange, now_ms=clock)
        spread = spread_shadow(snapshot, symbol, now_ms=clock)
        row["ccxt_guard_availability"] = snapshot.availability
        row["ccxt_guard_detail"] = snapshot.detail
        row["ccxt_listing_class"] = assessment["listing_class"]
        row["ccxt_listing_reason"] = assessment.get("reason")
        row["ccxt_stale"] = snapshot.stale
        row["ccxt_spread_block"] = False
        row["ccxt_spread_coverage"] = spread["coverage"]
        structural = bool(assessment["block_chase"]) and regime in CHASE_REGIMES and action == "buy"
        if regime == STATE1:
            structural = False
            row["ccxt_state1_preserved"] = True
        if snapshot.stale or snapshot.availability != "ok" or not CHASE_BLOCK_ENABLED:
            structural = False
        row["ccxt_chase_block"] = structural
        row["ccxt_chase_block_enabled"] = CHASE_BLOCK_ENABLED
        block = structural
        if block:
            dropped += 1
            continue
        kept.append(row)
    return kept, {
        "availability": snapshot.availability,
        "stale": snapshot.stale,
        "dropped_chase_buys": dropped,
        "spread_block_enabled": SPREAD_BLOCK_ENABLED,
        "chase_block_enabled": CHASE_BLOCK_ENABLED,
        "cited_audit_recomputed_here": False,
    }
