"""Binance spot klines → tech_boll factors, stored off the DEX indicator tables.

Symbol list comes from the collection-layer universe that already landed:

- ``exports/binance_spot_universe.db`` table ``symbols``
- ``exports/reports/binance_spot_usdt_trading_2026-11-05.json``

This module does not call ``/api/v3/exchangeInfo``. Klines are public REST
(``/api/v3/klines``) and need no API key. Factor math is the existing
``change_pct`` / ``volume_strength`` / ``compute_pandas_ta`` path; only the
price series changes (spot kline closes, trailing 1h quote volume as the
``volume_h1`` sample that function already reads).

Writes ``binance_klines`` and ``binance_indicators`` only.
``INDICATORS_WEIGHT_*`` stay untouched. No orders.

The eight ``binance_indicators`` columns added with the taker-buy kline fields
are read-only observations. This module does not change ``tech_boll_fade``
thresholds. Definitions and the consumer contract are in
``docs/binance_readonly_fields_2026-11-07.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from logging_setup import get_logger, setup_logging

from indicators.base import INTERVAL_SEC, utc_now_iso
from indicators.library_apply import apply_library_fields
from indicators.technical import (
    change_pct,
    volatility_stdev_returns,
    volume_7d_avg,
    volume_strength,
)
from indicators.technical_lib import compute_pandas_ta

log = get_logger("indicators.binance_spot")

DEFAULT_BASE = "https://api.binance.com"
DEFAULT_INTERVAL = "5m"
DEFAULT_KLINE_LIMIT = 500
# One public request. 1000×5m is about 3.5d, short of 7d. Older stored rows
# are kept; this module does not page with startTime.
KLINE_FETCH_CAP = 1000
_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000
_5M_MS = 300_000
# Latest closed bar plus the 11 before it. The bar 60m earlier is outside.
_HOUR_BARS = 12
# 24h of 5m bars immediately before the latest closed bar. That bar is excluded.
_PRIOR_DAY_BARS = 288
# Trailing day that includes the latest bar (prior 288 + current).
_TRAILING_DAY_BARS = _PRIOR_DAY_BARS + 1
# 7d of 5m steps. Both endpoints are required, so the grid is 2017 bars.
_WEEK_STEPS = 7 * 24 * 12
_WEEK_BARS = _WEEK_STEPS + 1
_INTERVALS = frozenset({"1m", "3m", "5m", "15m", "30m", "1h"})
_NOT_A_BASE = frozenset(
    {"USDT", "USD", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDP"}
)
_SYMBOL_COLS = ("symbol", "binance_symbol", "spot_symbol")
_BASE_COLS = ("base_asset", "base", "baseasset")
_QUOTE_COLS = ("quote_asset", "quote", "quoteasset")
_STATUS_COLS = ("status", "trading_status")
_JSON_LIST_KEYS = ("symbols", "rows", "universe", "data")
_JSON_META_KEYS = frozenset(
    {
        "generated_at",
        "generatedat",
        "as_of",
        "asof",
        "count",
        "source",
        "quote",
        "quote_asset",
        "status",
        "note",
        "notes",
        "market",
        "venue",
        "exchange",
    }
)

_INDICATOR_COLS = (
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
    "change_7d",
    "volume_7d_avg",
    "taker_buy_quote_volume_5m",
    "buy_ratio_5m",
    "buy_ratio_1h",
    "avg_trade_usd_5m",
    "trade_size_ratio",
    "lower_wick_ratio",
)

_KLINE_WRITE_COLS = (
    "symbol",
    "interval",
    "open_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "close_time_ms",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
)
_KLINE_ADD_COLUMNS = (
    ("taker_buy_volume", "REAL"),
    ("taker_buy_quote_volume", "REAL"),
)
_INDICATOR_ADD_COLUMNS = (
    ("change_7d", "REAL"),
    ("volume_7d_avg", "REAL"),
    ("taker_buy_quote_volume_5m", "REAL"),
    ("buy_ratio_5m", "REAL"),
    ("buy_ratio_1h", "REAL"),
    ("avg_trade_usd_5m", "REAL"),
    ("trade_size_ratio", "REAL"),
    ("lower_wick_ratio", "REAL"),
)

_SQL_PATH = Path(__file__).resolve().parents[1] / "models" / "binance_indicator_tables.sql"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_UNIVERSE_DB_NAME = "binance_spot_universe.db"
_UNIVERSE_JSON_NAME = "binance_spot_usdt_trading_2026-11-05.json"

JsonFetcher = Callable[[str], Any]


@dataclass(frozen=True)
class SpotSymbol:
    symbol: str
    base_asset: str
    quote_asset: str = "USDT"
    status: str = "TRADING"


@dataclass(frozen=True)
class SpotKline:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    close_time_ms: int
    trades: int | None = None
    taker_buy_volume: float | None = None
    taker_buy_quote_volume: float | None = None


def repo_root() -> Path:
    return _REPO_ROOT


def universe_candidate_paths(root: Path | None = None) -> list[Path]:
    """Landed universe files, first hit wins. Env paths come before the defaults."""
    base = root or repo_root()
    paths: list[Path] = []
    env_db = os.environ.get("BINANCE_SPOT_UNIVERSE_DB", "").strip()
    env_json = os.environ.get("BINANCE_SPOT_UNIVERSE_JSON", "").strip()
    if env_db:
        paths.append(Path(env_db))
    paths.append(base / "exports" / _UNIVERSE_DB_NAME)
    paths.append(base / "crawler" / "exports" / _UNIVERSE_DB_NAME)
    if env_json:
        paths.append(Path(env_json))
    paths.append(base / "exports" / "reports" / _UNIVERSE_JSON_NAME)
    paths.append(base / "crawler" / "exports" / "reports" / _UNIVERSE_JSON_NAME)
    return paths


def _is_base(text: str) -> bool:
    if text in _NOT_A_BASE:
        return False
    if len(text) < 2 or len(text) > 20:
        return False
    return text.isalnum()


def canonical_spot_usdt(
    symbol: str,
    base: str | None = None,
    quote: str | None = None,
) -> tuple[str, str] | None:
    """Return ``(BTCUSDT, BTC)`` for a spot USDT name, or None."""
    sym = symbol.strip().upper()
    if not sym or ":" in sym or any(ch.isspace() for ch in sym):
        return None
    b = (base or "").strip().upper()
    q = (quote or "").strip().upper()
    for sep in ("/", "-", "_"):
        if sep in sym:
            left, _, right = sym.partition(sep)
            if right != "USDT":
                return None
            if not b:
                b = left
            if not q:
                q = "USDT"
            sym = f"{left}USDT"
            break
    if q and q != "USDT":
        return None
    if b:
        if not _is_base(b) or sym != f"{b}USDT":
            return None
        return sym, b
    if not sym.endswith("USDT") or sym == "USDT":
        return None
    derived = sym[: -len("USDT")]
    if not _is_base(derived):
        return None
    return sym, derived


def _status_ok(status: str | None) -> bool:
    if status is None or str(status).strip() == "":
        return True
    return str(status).strip().upper() == "TRADING"


def _append_symbol(
    out: list[SpotSymbol],
    seen: set[str],
    symbol: str,
    base: str | None,
    quote: str | None,
    status: str | None,
) -> None:
    if not _status_ok(status):
        return
    parsed = canonical_spot_usdt(symbol, base, quote)
    if parsed is None or parsed[0] in seen:
        return
    seen.add(parsed[0])
    out.append(SpotSymbol(parsed[0], parsed[1], "USDT", "TRADING"))


def _rows_as_dicts(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [str(item[0]) for item in cur.description or ()]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _col_map(cols: Sequence[str]) -> dict[str, str]:
    return {name.lower(): name for name in cols}


def _pick(cols: dict[str, str], names: Sequence[str]) -> str | None:
    for name in names:
        found = cols.get(name)
        if found:
            return found
    return None


def _ident(name: str) -> str:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"unsafe column {name!r}")
    return '"' + name + '"'


def symbols_from_connection(conn: sqlite3.Connection) -> list[SpotSymbol]:
    """TRADING spot USDT rows from table ``symbols``. Missing table → empty."""
    try:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'symbols' AND type IN ('table', 'view')"
        ).fetchone()
    except sqlite3.Error as exc:
        log.warning("universe table scan failed: %s", exc)
        return []
    if found is None:
        return []
    info = conn.execute("PRAGMA table_info(symbols)").fetchall()
    raw_cols = [str(row[1]) for row in info]
    cols = _col_map(raw_cols)
    sym_col = _pick(cols, _SYMBOL_COLS)
    if sym_col is None:
        log.warning("universe symbols table has no symbol column cols=%s", raw_cols)
        return []
    base_col = _pick(cols, _BASE_COLS)
    quote_col = _pick(cols, _QUOTE_COLS)
    status_col = _pick(cols, _STATUS_COLS)
    select = [sym_col]
    if base_col:
        select.append(base_col)
    if quote_col:
        select.append(quote_col)
    if status_col:
        select.append(status_col)
    try:
        rows = _rows_as_dicts(
            conn,
            f"SELECT {', '.join(_ident(name) for name in select)} FROM symbols",
        )
    except sqlite3.Error as exc:
        log.warning("universe symbols read failed: %s", exc)
        return []
    out: list[SpotSymbol] = []
    seen: set[str] = set()
    for row in rows:
        _append_symbol(
            out,
            seen,
            "" if row.get(sym_col) is None else str(row.get(sym_col)),
            None if base_col is None or row.get(base_col) is None else str(row.get(base_col)),
            None if quote_col is None or row.get(quote_col) is None else str(row.get(quote_col)),
            None if status_col is None or row.get(status_col) is None else str(row.get(status_col)),
        )
    out.sort(key=lambda item: item.symbol)
    return out


def _ro_connect(path: Path) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(path.resolve().as_posix(), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def symbols_from_sqlite(path: Path) -> list[SpotSymbol]:
    conn = _ro_connect(path)
    try:
        return symbols_from_connection(conn)
    finally:
        conn.close()


def _json_items(payload: Any) -> tuple[list[Any], str | None]:
    if isinstance(payload, list):
        return payload, None
    if not isinstance(payload, dict):
        return [], None
    envelope = payload.get("status")
    envelope_status = envelope if isinstance(envelope, str) else None
    for key in _JSON_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value, envelope_status
    items: list[Any] = []
    for key, value in payload.items():
        if key.lower() in _JSON_META_KEYS:
            continue
        if isinstance(value, dict):
            row = dict(value)
            row.setdefault("symbol", key)
            items.append(row)
        elif isinstance(key, str) and key.upper().endswith("USDT"):
            items.append(key)
    return items, envelope_status


def symbols_from_json(path: Path) -> list[SpotSymbol]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    items, envelope_status = _json_items(payload)
    out: list[SpotSymbol] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            _append_symbol(out, seen, item, None, None, envelope_status)
            continue
        if not isinstance(item, dict):
            continue
        raw_symbol = item.get("symbol") or item.get("binance_symbol") or item.get("spot_symbol") or ""
        base = item.get("base_asset", item.get("base", item.get("baseAsset")))
        quote = item.get("quote_asset", item.get("quote", item.get("quoteAsset")))
        status = item.get("status", envelope_status)
        _append_symbol(
            out,
            seen,
            "" if raw_symbol is None else str(raw_symbol),
            None if base is None else str(base),
            None if quote is None else str(quote),
            None if status is None else str(status),
        )
    out.sort(key=lambda item: item.symbol)
    return out


def load_primary_universe(
    paths: Sequence[Path] | None = None,
) -> tuple[list[SpotSymbol], Path | None]:
    """First landed file that yields at least one TRADING USDT symbol.

    Does not call the exchange. An empty result means the artifact is missing
    or unreadable — callers must not invent a symbol list.
    """
    candidates = list(paths) if paths is not None else universe_candidate_paths()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            if path.suffix.lower() == ".json":
                rows = symbols_from_json(path)
            else:
                rows = symbols_from_sqlite(path)
        except (OSError, sqlite3.Error, json.JSONDecodeError, UnicodeError, ValueError) as exc:
            log.warning("universe read failed path=%s err=%s", path, exc)
            continue
        if not rows:
            log.warning("universe file had no TRADING USDT symbols path=%s", path)
            continue
        log.info("binance primary universe count=%s path=%s", len(rows), path)
        return rows, path
    return [], None


def kline_url(
    symbol: str,
    *,
    interval: str = DEFAULT_INTERVAL,
    limit: int = DEFAULT_KLINE_LIMIT,
    base: str = DEFAULT_BASE,
) -> str:
    query = urllib.parse.urlencode(
        {"symbol": symbol, "interval": interval, "limit": str(int(limit))}
    )
    return f"{base.rstrip('/')}/api/v3/klines?{query}"


def fetch_json(url: str, *, timeout: float = 20.0) -> Any:
    """Public GET. No API key header."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "trade-binance-spot-indicators/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"binance kline GET failed: {exc}") from exc
    return json.loads(payload.decode("utf-8"))


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _optional_float(raw: Sequence[Any], index: int) -> float | None:
    """Index past the end, blank, or non-numeric → None. Does not reject the row."""
    if len(raw) <= index or raw[index] in (None, ""):
        return None
    return _float_or_none(raw[index])


def parse_kline_row(raw: Any) -> SpotKline | None:
    """One Binance ``/api/v3/klines`` element. Malformed rows are skipped.

    A row of 8 or 9 fields (no taker columns) still parses. Index 9 is
    taker buy base volume and index 10 is taker buy quote volume. A bad
    taker cell becomes None and does not drop the OHLC row.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 8:
        return None
    try:
        open_time = int(raw[0])
        close_time = int(raw[6])
    except (TypeError, ValueError):
        return None
    op = _float_or_none(raw[1])
    hi = _float_or_none(raw[2])
    lo = _float_or_none(raw[3])
    close = _float_or_none(raw[4])
    volume = _float_or_none(raw[5])
    quote = _float_or_none(raw[7])
    if None in (op, hi, lo, close, volume, quote):
        return None
    if op <= 0 or close <= 0 or hi <= 0 or lo <= 0:
        return None
    trades: int | None = None
    if len(raw) > 8 and raw[8] not in (None, ""):
        try:
            trades = int(raw[8])
        except (TypeError, ValueError):
            trades = None
    return SpotKline(
        open_time_ms=open_time,
        open=op,
        high=hi,
        low=lo,
        close=close,
        volume=volume,
        quote_volume=quote,
        close_time_ms=close_time,
        trades=trades,
        taker_buy_volume=_optional_float(raw, 9),
        taker_buy_quote_volume=_optional_float(raw, 10),
    )


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def price_series_from_klines(klines: Sequence[SpotKline]) -> list[dict[str, Any]]:
    """Close series shaped for ``change_pct`` / ``volume_strength``.

    ``volume_h1`` is the trailing 1h quote-volume sum ending at that bar
    (close time strictly after ``bar.close - 1h``). That is the sample
    ``volume_strength`` already reads; the ratio is unchanged.

    ``volume_h24`` is set only when the bar ends a continuous run of 289
    5m bars (the prior 288 plus this one, open span exactly 24h). It is the
    sum of those bars' quote volume. A shorter prefix or a gap leaves it
    None so ``volume_7d_avg`` cannot treat a partial day as a trailing day.
    The walk is linear; it does not rescan the prefix per bar.
    """
    ordered = sorted(klines, key=lambda bar: (bar.close_time_ms, bar.open_time_ms))
    series: list[dict[str, Any]] = []
    hour_start = 0
    hour_total = 0.0
    day_start = 0
    day_total = 0.0
    run = 0
    for index, bar in enumerate(ordered):
        quote = float(bar.quote_volume)
        hour_total += quote
        cutoff = bar.close_time_ms - _HOUR_MS
        while hour_start <= index and ordered[hour_start].close_time_ms <= cutoff:
            hour_total -= float(ordered[hour_start].quote_volume)
            hour_start += 1
        if index > 0 and bar.open_time_ms == ordered[index - 1].open_time_ms + _5M_MS:
            run += 1
        else:
            run = 1
            day_start = index
            day_total = 0.0
        day_total += quote
        while index - day_start + 1 > _TRAILING_DAY_BARS:
            day_total -= float(ordered[day_start].quote_volume)
            day_start += 1
        volume_h24: float | None = None
        if run >= _TRAILING_DAY_BARS and index - day_start + 1 == _TRAILING_DAY_BARS:
            volume_h24 = day_total
        series.append(
            {
                "price_usd": float(bar.close),
                "recorded_at": _iso_ms(bar.close_time_ms),
                "volume_h1": hour_total,
                "volume_h24": volume_h24,
            }
        )
    return series


def quote_volume_24h(klines: Sequence[SpotKline]) -> float | None:
    """Sum of bar quote volume in the last 24h. Not a DEX pool balance."""
    if not klines:
        return None
    last = max(bar.close_time_ms for bar in klines)
    cutoff = last - _DAY_MS
    total = 0.0
    seen = False
    for bar in klines:
        if bar.close_time_ms < cutoff:
            continue
        total += float(bar.quote_volume)
        seen = True
    if not seen:
        return None
    return total


def _closed_bars(klines: Sequence[SpotKline], now_ms: int | None) -> list[SpotKline]:
    """Closed bars only (``close_time_ms <= now``), one row per open time.

    A later duplicate open replaces an earlier one. The forming bar, whose
    close is still in the future, is left out of every field below.
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    by_open: dict[int, SpotKline] = {}
    for bar in klines:
        if int(bar.close_time_ms) > now:
            continue
        by_open[int(bar.open_time_ms)] = bar
    return [by_open[key] for key in sorted(by_open)]


def _continuous_tail(ordered: Sequence[SpotKline], count: int) -> bool:
    """True when the last ``count`` bars are a 5m grid with no hole."""
    if count <= 0 or len(ordered) < count:
        return False
    window = ordered[-count:]
    origin = int(window[0].open_time_ms)
    for index, bar in enumerate(window):
        if int(bar.open_time_ms) != origin + index * _5M_MS:
            return False
    return True


def _lower_wick_ratio(bar: SpotKline) -> float | None:
    """Where the close sits in the high-low range. Not the lower wick.

    ``(close - low) / (high - low)``. A flat bar (``high == low``) is 0.
    ``high < low`` is NULL. The value is not clamped to ``[0, 1]``.
    A true lower wick would be ``(min(open, close) - low) / (high - low)``;
    this column does not use that.
    """
    high = float(bar.high)
    low = float(bar.low)
    if high < low:
        return None
    if high == low:
        return 0.0
    return (float(bar.close) - low) / (high - low)


def _buy_ratio_5m(bar: SpotKline) -> float | None:
    """Latest closed bar: taker buy quote / quote volume. Zero quote is NULL."""
    taker = bar.taker_buy_quote_volume
    quote = bar.quote_volume
    if taker is None or quote is None or taker < 0 or quote <= 0:
        return None
    return float(taker) / float(quote)


def _taker_fields_present(bar: SpotKline) -> bool:
    base = bar.taker_buy_volume
    quote = bar.taker_buy_quote_volume
    if base is None or quote is None:
        return False
    return base >= 0 and quote >= 0


def _buy_ratio_1h(ordered: Sequence[SpotKline]) -> float | None:
    """Sum of taker quote / sum of quote over 12 continuous closed 5m bars.

    The latest bar is included. Any missing taker base, taker quote, or quote
    volume in that window, or a quote sum of 0, is NULL. A shorter or gappy
    hour is NULL. The bar that opened 60m earlier is not in the window.
    """
    if not _continuous_tail(ordered, _HOUR_BARS):
        return None
    taker_sum = 0.0
    quote_sum = 0.0
    for bar in ordered[-_HOUR_BARS:]:
        if not _taker_fields_present(bar):
            return None
        quote = bar.quote_volume
        if quote is None or quote < 0:
            return None
        taker_quote = bar.taker_buy_quote_volume
        if taker_quote is None:
            return None
        taker_sum += float(taker_quote)
        quote_sum += float(quote)
    if quote_sum <= 0:
        return None
    return taker_sum / quote_sum


def _avg_trade_usd(bar: SpotKline) -> float | None:
    """``quote_volume / trades``. Missing or non-positive trades → None."""
    trades = bar.trades
    quote = bar.quote_volume
    if trades is None or quote is None or trades <= 0 or quote < 0:
        return None
    return float(quote) / float(trades)


def _trade_size_ratio(ordered: Sequence[SpotKline]) -> float | None:
    """Latest average print / prior-288 ``sum(quote) / sum(trades)``.

    The current bar is not in the baseline. The 288 bars must be the
    continuous 5m grid immediately before it (open of the oldest is exactly
    24h before the current open, so its close is exactly 24h before the
    current close). A gap, a short window, a missing quote, or a non-positive
    trade count anywhere in those 288 bars is NULL. The baseline is not the
    mean of per-bar ``quote / trades``.
    """
    need = _PRIOR_DAY_BARS + 1
    if not _continuous_tail(ordered, need):
        return None
    current_avg = _avg_trade_usd(ordered[-1])
    if current_avg is None:
        return None
    quote_sum = 0.0
    trade_sum = 0.0
    for bar in ordered[-need:-1]:
        quote = bar.quote_volume
        trades = bar.trades
        if quote is None or trades is None or quote < 0 or trades <= 0:
            return None
        quote_sum += float(quote)
        trade_sum += float(trades)
    if trade_sum <= 0 or quote_sum <= 0:
        return None
    return current_avg / (quote_sum / trade_sum)


def _week_fields(
    ordered: Sequence[SpotKline],
    series: list[dict[str, Any]],
) -> dict[str, float | None]:
    """``change_pct`` / ``volume_7d_avg`` only on a continuous 7d 5m grid.

    Both helpers accept a 6h skew and ``volume_7d_avg`` skips empty days, so
    a hole or a window a few hours short of 7d can still produce a number.
    Those results are not stored. The grid is the last 2017 closed 5m bars
    (open span exactly 7d). ``volume_7d_avg`` can still return None on that
    grid: it needs a ``volume_h24`` reading within 6h of the 7d anchor, and
    a trailing day is only stamped once 289 continuous bars exist. That
    reading appears about 24h after the first stored bar.
    """
    empty = {"change_7d": None, "volume_7d_avg": None}
    if not _continuous_tail(ordered, _WEEK_BARS):
        return empty
    span = int(ordered[-1].open_time_ms) - int(ordered[-_WEEK_BARS].open_time_ms)
    if span != _WEEK_STEPS * _5M_MS:
        return empty
    return {
        "change_7d": change_pct(series, INTERVAL_SEC["change_7d"]),
        "volume_7d_avg": volume_7d_avg(series),
    }


def _readonly_fields(
    ordered: Sequence[SpotKline],
    series: list[dict[str, Any]],
    interval: str,
) -> dict[str, Any]:
    """Eight nullable columns. Non-5m intervals stay NULL (the bar counts are 5m)."""
    latest = ordered[-1] if ordered else None
    out: dict[str, Any] = {
        "change_7d": None,
        "volume_7d_avg": None,
        "taker_buy_quote_volume_5m": None,
        "buy_ratio_5m": None,
        "buy_ratio_1h": None,
        "avg_trade_usd_5m": None,
        "trade_size_ratio": None,
        "lower_wick_ratio": None,
    }
    if interval != DEFAULT_INTERVAL or latest is None:
        return out
    out["taker_buy_quote_volume_5m"] = latest.taker_buy_quote_volume
    out["buy_ratio_5m"] = _buy_ratio_5m(latest)
    out["buy_ratio_1h"] = _buy_ratio_1h(ordered)
    out["avg_trade_usd_5m"] = _avg_trade_usd(latest)
    out["trade_size_ratio"] = _trade_size_ratio(ordered)
    out["lower_wick_ratio"] = _lower_wick_ratio(latest)
    out.update(_week_fields(ordered, series))
    return out


def factors_from_klines(
    klines: Sequence[SpotKline],
    *,
    now_ms: int | None = None,
    interval: str = DEFAULT_INTERVAL,
) -> dict[str, Any] | None:
    """Same tech_boll family as paper: change_*, volume_strength, BOLL(20, 2σ) on close.

    Micro and 7d columns use closed bars only. ``interval`` other than 5m
    leaves those eight columns NULL.
    """
    closed = _closed_bars(klines, now_ms)
    series = price_series_from_klines(closed)
    if not series:
        return None
    closes = [float(row["price_usd"]) for row in series]
    lib = compute_pandas_ta(closes)
    fields, _fresh = apply_library_fields(None, lib)
    out: dict[str, Any] = {
        "price_usd": closes[-1],
        "change_5m": change_pct(series, INTERVAL_SEC["change_5m"]),
        "change_15m": change_pct(series, INTERVAL_SEC["change_15m"]),
        "change_1h": change_pct(series, INTERVAL_SEC["change_1h"]),
        "change_4h": change_pct(series, INTERVAL_SEC["change_4h"]),
        "change_24h": change_pct(series, INTERVAL_SEC["change_24h"]),
        "volatility_24h": volatility_stdev_returns(series),
        "volume_strength": volume_strength(series),
        "quote_volume_24h": quote_volume_24h(closed),
        "sample_count": len(series),
        "kline_close_time_ms": max(bar.close_time_ms for bar in closed),
    }
    out.update(fields)
    out.update(_readonly_fields(closed, series, interval))
    return out


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[tuple[str, str]],
) -> None:
    have = _table_columns(conn, table)
    for name, typ in columns:
        if name in have:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create binance_* tables and add missing columns on tables that already exist.

    ``CREATE TABLE IF NOT EXISTS`` does not change an old table. New columns
    are added with ``ALTER TABLE ... ADD COLUMN`` only when absent. Rows and
    columns this migration does not own are left in place. A second call is
    a no-op. Does not migrate ``token_indicators``.
    """
    script = _SQL_PATH.read_text(encoding="utf-8")
    conn.executescript(script)
    _add_columns(conn, "binance_klines", _KLINE_ADD_COLUMNS)
    _add_columns(conn, "binance_indicators", _INDICATOR_ADD_COLUMNS)


def connect_rw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    ensure_schema(conn)
    return conn


def upsert_klines(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    klines: Sequence[SpotKline],
) -> int:
    """Insert or update kline rows. Columns this writer does not own stay put.

    Older open times that are not in ``klines`` are not deleted. A later read
    can therefore span more than one fetch window.
    """
    ensure_schema(conn)
    names = ", ".join(_KLINE_WRITE_COLS)
    placeholders = ", ".join("?" for _ in _KLINE_WRITE_COLS)
    updates = ", ".join(
        f"{name}=excluded.{name}"
        for name in _KLINE_WRITE_COLS
        if name not in ("symbol", "interval", "open_time_ms")
    )
    rows = [
        (
            symbol,
            interval,
            bar.open_time_ms,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.quote_volume,
            bar.close_time_ms,
            bar.trades,
            bar.taker_buy_volume,
            bar.taker_buy_quote_volume,
        )
        for bar in klines
    ]
    conn.executemany(
        f"""
        INSERT INTO binance_klines ({names}) VALUES ({placeholders})
        ON CONFLICT(symbol, interval, open_time_ms) DO UPDATE SET {updates}
        """,
        rows,
    )
    return len(rows)


def load_stored_klines(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
) -> list[SpotKline]:
    """Every stored bar for one symbol, oldest open first. Not capped at 1000."""
    ensure_schema(conn)
    fetched = conn.execute(
        """
        SELECT open_time_ms, open, high, low, close, volume, quote_volume,
               close_time_ms, trades, taker_buy_volume, taker_buy_quote_volume
        FROM binance_klines
        WHERE symbol = ? AND interval = ?
        ORDER BY open_time_ms ASC
        """,
        (symbol, interval),
    ).fetchall()
    loaded: list[SpotKline] = []
    for row in fetched:
        op = _float_or_none(row[1])
        hi = _float_or_none(row[2])
        lo = _float_or_none(row[3])
        close = _float_or_none(row[4])
        volume = _float_or_none(row[5])
        quote = _float_or_none(row[6])
        if None in (op, hi, lo, close, volume, quote):
            continue
        if op <= 0 or hi <= 0 or lo <= 0 or close <= 0:
            continue
        trades: int | None
        if row[8] is None:
            trades = None
        else:
            try:
                trades = int(row[8])
            except (TypeError, ValueError):
                trades = None
        loaded.append(
            SpotKline(
                open_time_ms=int(row[0]),
                open=op,
                high=hi,
                low=lo,
                close=close,
                volume=volume,
                quote_volume=quote,
                close_time_ms=int(row[7]),
                trades=trades,
                taker_buy_volume=_float_or_none(row[9]),
                taker_buy_quote_volume=_float_or_none(row[10]),
            )
        )
    return loaded


def upsert_indicator(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert or update one indicator row. Unlisted columns on the table stay put."""
    ensure_schema(conn)
    names = ", ".join(_INDICATOR_COLS)
    placeholders = ", ".join("?" for _ in _INDICATOR_COLS)
    updates = ", ".join(
        f"{name}=excluded.{name}" for name in _INDICATOR_COLS if name != "symbol"
    )
    conn.execute(
        f"""
        INSERT INTO binance_indicators ({names}) VALUES ({placeholders})
        ON CONFLICT(symbol) DO UPDATE SET {updates}
        """,
        tuple(row.get(col) for col in _INDICATOR_COLS),
    )


def persist_klines_and_factors(
    conn: sqlite3.Connection,
    meta: SpotSymbol,
    klines: Sequence[SpotKline],
    *,
    interval: str = DEFAULT_INTERVAL,
    updated_at: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Write one symbol's klines and tech_boll row. DEX tables are not touched.

    Factors are computed from every stored bar for this symbol and interval,
    not only the batch just fetched. Rows older than the fetch window stay
    in ``binance_klines``.
    """
    upsert_klines(conn, meta.symbol, interval, klines)
    merged = load_stored_klines(conn, meta.symbol, interval)
    factors = factors_from_klines(merged, now_ms=now_ms, interval=interval)
    if factors is None:
        conn.commit()
        return None
    now = updated_at or utc_now_iso()
    stored = {
        "symbol": meta.symbol,
        "base_asset": meta.base_asset,
        "quote_asset": meta.quote_asset,
        "status": meta.status,
        "interval": interval,
        "tier": "hot",
        "tech_updated_at": now,
        "updated_at": now,
        **factors,
    }
    upsert_indicator(conn, stored)
    conn.commit()
    return stored


def ingest_spot_klines(
    conn: sqlite3.Connection,
    symbols: Sequence[SpotSymbol],
    *,
    fetcher: JsonFetcher,
    interval: str = DEFAULT_INTERVAL,
    kline_limit: int = DEFAULT_KLINE_LIMIT,
    pause_sec: float = 0.0,
    base_url: str = DEFAULT_BASE,
) -> dict[str, int]:
    """Fetch public klines for ``symbols`` and write the side tables.

    ``symbols`` must already be the primary universe (or a subset of it).
    This function does not discover listings.
    """
    if interval not in _INTERVALS:
        raise ValueError(f"interval must be one of {sorted(_INTERVALS)}")
    # Single request. Do not walk startTime to backfill 7d; stored rows accumulate.
    limit = max(20, min(int(kline_limit), KLINE_FETCH_CAP))
    ensure_schema(conn)
    stats = {"symbols": 0, "klines": 0, "indicators": 0, "failed": 0, "skipped": 0}
    total = len(symbols)
    for index, meta in enumerate(symbols):
        url = kline_url(meta.symbol, interval=interval, limit=limit, base=base_url)
        try:
            payload = fetcher(url)
        except Exception as exc:  # noqa: BLE001 — one symbol must not abort the universe
            stats["failed"] += 1
            log.warning("kline fetch failed symbol=%s err=%s", meta.symbol, exc)
            continue
        if not isinstance(payload, list):
            stats["failed"] += 1
            log.warning("kline payload not a list symbol=%s", meta.symbol)
            continue
        bars = [bar for row in payload if (bar := parse_kline_row(row)) is not None]
        if not bars:
            stats["skipped"] += 1
            continue
        try:
            stored = persist_klines_and_factors(conn, meta, bars, interval=interval)
        except sqlite3.Error as exc:
            stats["failed"] += 1
            log.warning("kline write failed symbol=%s err=%s", meta.symbol, exc)
            continue
        if stored is None:
            stats["skipped"] += 1
            continue
        stats["symbols"] += 1
        stats["klines"] += len(bars)
        stats["indicators"] += 1
        if pause_sec > 0 and index + 1 < total:
            time.sleep(pause_sec)
    log.info(
        "binance kline ingest symbols=%s indicators=%s klines=%s failed=%s skipped=%s",
        stats["symbols"],
        stats["indicators"],
        stats["klines"],
        stats["failed"],
        stats["skipped"],
    )
    return stats


def filter_universe(
    symbols: Sequence[SpotSymbol],
    *,
    only: Sequence[str] | None = None,
    max_symbols: int | None = None,
) -> list[SpotSymbol]:
    """Subset of the landed universe. Names outside the universe are ignored."""
    chosen = list(symbols)
    if only:
        allow: set[str] = set()
        for raw in only:
            parsed = canonical_spot_usdt(raw)
            if parsed is not None:
                allow.add(parsed[0])
        chosen = [item for item in chosen if item.symbol in allow]
    if max_symbols is not None:
        chosen = chosen[: max(0, int(max_symbols))]
    return chosen


def _resolve_db(raw: str | None) -> Path | None:
    if raw:
        path = Path(raw)
        if path.is_file() or path.is_absolute():
            return path
        for base in (Path.cwd(), repo_root(), repo_root() / "crawler"):
            cand = base / path
            if cand.is_file():
                return cand
        return path
    for key in ("DECISION_DB_PATH", "DB_PATH"):
        env = os.environ.get(key, "").strip()
        if env:
            return Path(env)
    for cand in (
        repo_root() / "crawler" / "dexscreener.db",
        repo_root() / "dexscreener.db",
    ):
        if cand.is_file():
            return cand
    return None


def main(argv: Sequence[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Write tech_boll factors from Binance spot klines into binance_indicators. "
            "Symbols come from the landed primary universe, not exchangeInfo."
        )
    )
    parser.add_argument("--db", default="", help="SQLite file that receives binance_* tables")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--kline-limit", type=int, default=DEFAULT_KLINE_LIMIT)
    parser.add_argument("--max-symbols", type=int, default=0, help="0 = the whole universe")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Restrict to these universe symbols (repeatable). Others are not fetched.",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=float(os.environ.get("BINANCE_KLINE_PAUSE_SEC", "0.05") or "0"),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the universe count and exit without fetching klines",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    symbols, source = load_primary_universe()
    if not symbols or source is None:
        log.error(
            "binance primary universe missing; looked for exports/binance_spot_universe.db "
            "table symbols and exports/reports/%s",
            _UNIVERSE_JSON_NAME,
        )
        return 2
    chosen = filter_universe(
        symbols,
        only=args.symbol or None,
        max_symbols=args.max_symbols or None,
    )
    log.info("universe source=%s listed=%s selected=%s", source, len(symbols), len(chosen))
    if args.dry_run:
        print(f"universe={len(symbols)} selected={len(chosen)} source={source}")
        return 0
    if not chosen:
        log.error("no universe symbols selected")
        return 2
    db_path = _resolve_db(args.db or None)
    if db_path is None:
        log.error("pass --db (indicator sqlite that should gain binance_* tables)")
        return 2
    conn = connect_rw(db_path)
    try:
        stats = ingest_spot_klines(
            conn,
            chosen,
            fetcher=fetch_json,
            interval=args.interval,
            kline_limit=args.kline_limit,
            pause_sec=max(0.0, float(args.pause)),
        )
    finally:
        conn.close()
    print(
        f"indicators={stats['indicators']} klines={stats['klines']} "
        f"failed={stats['failed']} db={db_path}"
    )
    return 0 if stats["indicators"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
