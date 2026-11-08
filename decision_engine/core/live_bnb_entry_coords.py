"""Attach closed-bar coordinates for the live_bnb entry zone.

Reads ``binance_klines`` (then ``ccxt_ohlcv``) already on the indicator
connection. Does not create columns and does not refetch klines. ``#164``
is not on main: there is no stored ``trade_size_ratio``. When the row does
not already carry one, this fills it from ``quote_volume / trades`` using
the same current-bar versus prior-24h definition as
``live_bnb_entry_zone.trade_size_ratio_from_bars``.

``boll_mid`` already on the row is kept. It is computed from the closed
closes only when the row has none, with the same SMA(20) as the native
factor path. A row price that disagrees with the matched bar's close is
flagged ``kline_close_mismatch`` and is not rewritten.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from logging_setup import get_logger

from decision_engine.core.bnb_native import (
    BOLL_LENGTH,
    CCXT_KLINE_TABLE,
    KLINE_MS,
    KLINE_TABLE,
    boll_mid_from_closes,
)
from decision_engine.core.bnb_spot_map import classify_listed_symbol
from decision_engine.strategies.live_bnb_entry_zone import (
    ZoneBar,
    taker_buy_ratio_from_bar,
    trade_size_ratio_from_bars,
)

log = get_logger("decision_engine.live_bnb_entry_coords")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number

_PRICE_REL = 1e-6
_PRICE_ABS = 1e-8


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _pick(cols: set[str], names: Sequence[str]) -> str | None:
    for name in names:
        if name in cols:
            return name
    return None


def _same_price(left: float, right: float) -> bool:
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) <= max(_PRICE_ABS, _PRICE_REL * scale)


def _bar_matches(bar: ZoneBar, target_ms: int) -> bool:
    marks = (
        bar.close_ms,
        bar.open_ms,
        bar.open_ms + KLINE_MS,
        bar.open_ms + KLINE_MS - 1,
    )
    return target_ms in marks


def _read_table(
    conn: sqlite3.Connection,
    table: str,
    wanted: set[str],
) -> dict[str, dict[int, ZoneBar]]:
    cols = _columns(conn, table)
    symbol_col = _pick(cols, ("symbol", "binance_symbol", "spot_symbol"))
    open_col = _pick(cols, ("open_time_ms", "ts", "open_time", "open_ms"))
    low_col = _pick(cols, ("low", "l"))
    close_col = _pick(cols, ("close", "c"))
    if symbol_col is None or open_col is None or low_col is None or close_col is None:
        return {}
    vol_col = _pick(cols, ("volume", "base_volume", "v"))
    quote_col = _pick(cols, ("quote_volume", "quoteVolume", "q"))
    trades_col = _pick(cols, ("trades", "number_of_trades", "n"))
    taker_base_col = _pick(cols, ("taker_buy_base_volume", "takerBuyBaseVolume", "V"))
    taker_quote_col = _pick(cols, ("taker_buy_quote_volume", "takerBuyQuoteVolume", "Q"))
    close_time_col = _pick(cols, ("close_time_ms", "close_time"))
    interval_col = _pick(cols, ("interval", "timeframe"))
    exchange_col = "exchange" if "exchange" in cols else None
    selected = [symbol_col, open_col, low_col, close_col]
    for extra in (
        vol_col,
        quote_col,
        trades_col,
        taker_base_col,
        taker_quote_col,
        close_time_col,
        interval_col,
        exchange_col,
    ):
        if extra is not None and extra not in selected:
            selected.append(extra)
    sql = f"SELECT {', '.join(selected)} FROM {table}"
    buckets: dict[str, dict[int, ZoneBar]] = {}
    for raw in conn.execute(sql):
        row = dict(zip(selected, raw))
        if exchange_col is not None:
            exchange = str(row.get(exchange_col) or "").strip().lower()
            if exchange and exchange != "binance":
                continue
        if interval_col is not None:
            interval = row.get(interval_col)
            if interval is not None and str(interval).strip().lower() not in ("", "5m"):
                continue
        spot = classify_listed_symbol(
            None if row.get(symbol_col) is None else str(row.get(symbol_col))
        )
        if spot is None or (wanted and spot not in wanted):
            continue
        open_ms = _finite(row.get(open_col))
        low = _finite(row.get(low_col))
        close = _finite(row.get(close_col))
        if open_ms is None or low is None or close is None or low <= 0 or close <= 0:
            continue
        opened = int(open_ms)
        if close_time_col is not None and row.get(close_time_col) is not None:
            close_ms_raw = _finite(row.get(close_time_col))
            close_ms = int(close_ms_raw) if close_ms_raw is not None else opened + KLINE_MS
        else:
            close_ms = opened + KLINE_MS
        vol = _finite(row.get(vol_col)) if vol_col is not None else None
        quote = _finite(row.get(quote_col)) if quote_col is not None else None
        trades = _finite(row.get(trades_col)) if trades_col is not None else None
        taker_base = _finite(row.get(taker_base_col)) if taker_base_col is not None else None
        taker_quote = _finite(row.get(taker_quote_col)) if taker_quote_col is not None else None
        buckets.setdefault(spot, {})[opened] = ZoneBar(
            open_ms=opened,
            close_ms=close_ms,
            low=low,
            close=close,
            quote_volume=quote,
            trades=trades,
            volume=vol,
            taker_buy_base_volume=taker_base,
            taker_buy_quote_volume=taker_quote,
        )
    return buckets


def load_zone_bars(
    conn: sqlite3.Connection,
    symbols: set[str],
    *,
    now_ms: int,
) -> dict[str, list[ZoneBar]]:
    """Closed 5m bars keyed by ``BASEUSDT``. Missing tables → empty."""
    try:
        names = _tables(conn)
    except sqlite3.Error as exc:
        log.warning("entry-zone kline probe failed: %s", exc)
        return {}
    merged: dict[str, dict[int, ZoneBar]] = {}
    for table in (KLINE_TABLE, CCXT_KLINE_TABLE):
        if table not in names:
            continue
        try:
            found = _read_table(conn, table, symbols)
        except sqlite3.Error as exc:
            log.warning("entry-zone kline read %s failed: %s", table, exc)
            continue
        for spot, by_open in found.items():
            bucket = merged.setdefault(spot, {})
            for open_ms, bar in by_open.items():
                bucket.setdefault(open_ms, bar)
    out: dict[str, list[ZoneBar]] = {}
    for spot, by_open in merged.items():
        ordered = [by_open[key] for key in sorted(by_open)]
        closed = [bar for bar in ordered if bar.open_ms + KLINE_MS <= int(now_ms)]
        if closed:
            out[spot] = closed
    return out


def _symbol_of(row: dict[str, Any]) -> str | None:
    for key in ("binance_symbol", "token_address"):
        raw = row.get(key)
        if not isinstance(raw, str):
            continue
        spot = classify_listed_symbol(raw)
        if spot is not None:
            return spot
    return None


def _match_bar(bars: Sequence[ZoneBar], row: dict[str, Any]) -> ZoneBar | None:
    target = _finite(row.get("kline_close_time_ms"))
    if target is None:
        return bars[-1]
    target_ms = int(target)
    for bar in bars:
        if _bar_matches(bar, target_ms):
            return bar
    return None


def attach_live_entry_coordinates(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    """Fill missing entry-zone fields in place. Existing finite values stay."""
    if not rows:
        return rows
    symbols = {spot for row in rows if (spot := _symbol_of(row)) is not None}
    if not symbols:
        return rows
    try:
        bars_by_symbol = load_zone_bars(conn, symbols, now_ms=now_ms)
    except sqlite3.Error as exc:
        log.warning("entry-zone coordinates failed closed: %s", exc)
        return rows
    for row in rows:
        spot = _symbol_of(row)
        if spot is None:
            continue
        bars = bars_by_symbol.get(spot) or []
        if not bars:
            continue
        if row.get("kline_close_time_ms") is not None and _match_bar(bars, row) is None:
            row["entry_zone_coord_error"] = "kline_bar_missing"
            continue
        bar = _match_bar(bars, row)
        if bar is None:
            continue
        end = bars.index(bar)
        window = bars[: end + 1]
        price = _finite(row.get("price_usd"))
        if price is None:
            price = _finite(row.get("price"))
        if price is None:
            row["price_usd"] = bar.close
        elif not _same_price(price, bar.close):
            row["entry_zone_coord_error"] = "kline_close_mismatch"
            continue
        if _finite(row.get("kline_low")) is None and _finite(row.get("low")) is None:
            row["kline_low"] = bar.low
        if _finite(row.get("boll_mid")) is None and len(window) >= BOLL_LENGTH:
            mid = boll_mid_from_closes([item.close for item in window])
            if mid is not None:
                row["boll_mid"] = mid
        if _finite(row.get("taker_buy_ratio")) is None:
            tbr, tbr_source = taker_buy_ratio_from_bar(bar)
            if tbr is not None:
                row["taker_buy_ratio"] = tbr
                row["taker_buy_ratio_source"] = tbr_source
        if _finite(row.get("trade_size_ratio")) is not None:
            continue
        if (
            _finite(row.get("avg_trade_size")) is not None
            and _finite(row.get("avg_trade_size_24h")) is not None
        ):
            continue
        if _finite(row.get("avg_trade_size")) is not None:
            _fill_baseline_parts(row, window)
            if _finite(row.get("baseline_trades")) is not None:
                continue
        ratio, count = trade_size_ratio_from_bars(window)
        if ratio is not None:
            row["trade_size_ratio"] = ratio
            row["trade_size_ratio_source"] = "kline_quote_per_trade"
            row["trade_size_baseline_bars"] = count
    return rows


def _fill_baseline_parts(row: dict[str, Any], window: Sequence[ZoneBar]) -> None:
    """Leave ``avg_trade_size`` on the row and add the 24h denominator parts."""
    if len(window) < 2:
        return
    current = window[-1]
    cutoff = current.close_ms - 24 * 60 * 60 * 1000
    baseline = [
        bar
        for bar in window[:-1]
        if bar.open_ms < current.open_ms and bar.close_ms >= cutoff
    ]
    if any(bar.quote_volume is None or bar.trades is None for bar in baseline):
        return
    quote = sum(float(bar.quote_volume or 0.0) for bar in baseline)
    trades = sum(float(bar.trades or 0.0) for bar in baseline)
    if trades <= 0:
        return
    row["baseline_quote_volume"] = quote
    row["baseline_trades"] = trades
    row["trade_size_baseline_bars"] = len(baseline)
