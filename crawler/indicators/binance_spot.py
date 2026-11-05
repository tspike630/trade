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

Crowding columns and ``change_5m_z`` are shadow readings on the same rows.
They are not inputs to ``volume_strength`` and not entry gates. See
``docs/crowding_spot_shadow_columns_2026-11-05.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
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
from indicators.technical import change_pct, volatility_stdev_returns, volume_strength
from indicators.technical_lib import compute_pandas_ta

log = get_logger("indicators.binance_spot")

DEFAULT_BASE = "https://api.binance.com"
DEFAULT_INTERVAL = "5m"
DEFAULT_KLINE_LIMIT = 500
_HOUR_MS = 3_600_000
_DAY_MS = 86_400_000
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
    "avg_trade_size",
    "trades_spike_ratio",
    "taker_buy_quote_ratio",
    "change_5m_z",
)

# /api/v3/klines, 0-based. Index 9 is taker-buy *base* volume and is not stored.
_TAKER_BUY_QUOTE_VOLUME_INDEX = 10
# Latest bar is the numerator only. Prior 12 stored bars of the same series
# are the mean. The live job writes 5m, so those 12 bars are one hour.
TRADES_SPIKE_PRIOR_BARS = 12
# Prior 5m returns only. The latest return is the numerator and is not in σ.
CHANGE_5M_Z_PRIOR_RETURNS = 20
# Percentage points. At or below this, σ is treated as zero and z stays NULL.
_CHANGE_5M_Z_SIGMA_EPS = 1e-12

_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "binance_klines": (("taker_buy_quote_volume", "REAL"),),
    "binance_indicators": (
        ("avg_trade_size", "REAL"),
        ("trades_spike_ratio", "REAL"),
        ("taker_buy_quote_ratio", "REAL"),
        ("change_5m_z", "REAL"),
    ),
}

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


def parse_kline_row(raw: Any) -> SpotKline | None:
    """One Binance ``/api/v3/klines`` element. Malformed rows are skipped."""
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
    taker_quote: float | None = None
    if len(raw) > _TAKER_BUY_QUOTE_VOLUME_INDEX:
        taker_quote = _float_or_none(raw[_TAKER_BUY_QUOTE_VOLUME_INDEX])
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
        taker_buy_quote_volume=taker_quote,
    )


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def price_series_from_klines(klines: Sequence[SpotKline]) -> list[dict[str, Any]]:
    """Close series shaped for ``change_pct`` / ``volume_strength``.

    ``volume_h1`` is the trailing 1h quote-volume sum ending at that bar.
    That is the sample ``volume_strength`` already reads; the ratio is unchanged.
    """
    ordered = sorted(klines, key=lambda bar: (bar.close_time_ms, bar.open_time_ms))
    series: list[dict[str, Any]] = []
    for index, bar in enumerate(ordered):
        cutoff = bar.close_time_ms - _HOUR_MS
        total = 0.0
        seen = False
        for prev in ordered[: index + 1]:
            if prev.close_time_ms <= cutoff or prev.close_time_ms > bar.close_time_ms:
                continue
            total += float(prev.quote_volume)
            seen = True
        series.append(
            {
                "price_usd": float(bar.close),
                "recorded_at": _iso_ms(bar.close_time_ms),
                "volume_h1": total if seen else None,
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


def _avg_trade_size(bar: SpotKline) -> float | None:
    """Current bar quote_volume / trades. Empty or non-positive trades → None."""
    trades = bar.trades
    if trades is None or trades <= 0:
        return None
    quote = float(bar.quote_volume)
    if quote != quote or quote < 0:
        return None
    return quote / float(trades)


def _taker_buy_quote_ratio(bar: SpotKline) -> float | None:
    """Current bar taker_buy_quote_volume / quote_volume. Not clipped to 1."""
    taker = bar.taker_buy_quote_volume
    if taker is None or taker != taker or taker < 0:
        return None
    quote = float(bar.quote_volume)
    if quote != quote or quote <= 0:
        return None
    return float(taker) / quote


def _trades_spike_ratio(ordered: Sequence[SpotKline]) -> float | None:
    """Latest trades / mean of the previous ``TRADES_SPIKE_PRIOR_BARS`` bars.

    The latest bar is not inside the mean. A missing trade count anywhere in
    that window, a short series, or a non-positive mean stays None. A latest
    count of 0 with a positive mean is 0, not an error.
    """
    need = TRADES_SPIKE_PRIOR_BARS + 1
    if len(ordered) < need:
        return None
    last = ordered[-1]
    if last.trades is None or last.trades < 0:
        return None
    prior = ordered[-need:-1]
    if len(prior) != TRADES_SPIKE_PRIOR_BARS:
        return None
    samples: list[float] = []
    for bar in prior:
        if bar.trades is None or bar.trades < 0:
            return None
        samples.append(float(bar.trades))
    mean = sum(samples) / float(len(samples))
    if mean <= 0:
        return None
    return float(last.trades) / mean


def crowding_shadow_from_klines(klines: Sequence[SpotKline]) -> dict[str, float | None]:
    """Shadow crowding readings for the newest bar. Not an entry gate.

    ``avg_trade_size`` and ``taker_buy_quote_ratio`` use that bar alone.
    ``trades_spike_ratio`` uses it over the previous 12 stored bars.
    Empty input returns three Nones and does not raise.
    """
    empty: dict[str, float | None] = {
        "avg_trade_size": None,
        "trades_spike_ratio": None,
        "taker_buy_quote_ratio": None,
    }
    if not klines:
        return empty
    ordered = sorted(klines, key=lambda bar: (bar.close_time_ms, bar.open_time_ms))
    last = ordered[-1]
    return {
        "avg_trade_size": _avg_trade_size(last),
        "trades_spike_ratio": _trades_spike_ratio(ordered),
        "taker_buy_quote_ratio": _taker_buy_quote_ratio(last),
    }


def _bar_return_pct(prev_close: float, close: float) -> float | None:
    """Simple return in percentage points, the same unit as ``change_5m``."""
    if prev_close <= 0 or close <= 0:
        return None
    if prev_close != prev_close or close != close:
        return None
    return (close - prev_close) / prev_close * 100.0


def change_5m_z_from_klines(klines: Sequence[SpotKline]) -> float | None:
    """Latest 5m return / sample σ of the previous 20 bar returns.

    Both sides are percentage points, matching ``change_5m`` (``-2`` is −2%,
    not −0.02). On a regular 5m grid the numerator is the same number as
    ``change_5m``. σ is ``statistics.stdev`` of those prior 20 returns only.
    The current bar is not inside σ. This is not ``volatility_24h``.

    None when the series has fewer than 20 prior returns, any close is
    unusable, or σ is about 0 (``<= 1e-12`` percentage points).
    """
    if not klines:
        return None
    ordered = sorted(klines, key=lambda bar: (bar.close_time_ms, bar.open_time_ms))
    returns: list[float] = []
    for prev, bar in zip(ordered, ordered[1:]):
        step = _bar_return_pct(float(prev.close), float(bar.close))
        if step is None:
            return None
        returns.append(step)
    need = CHANGE_5M_Z_PRIOR_RETURNS + 1
    if len(returns) < need:
        return None
    current = returns[-1]
    prior = returns[-need:-1]
    if len(prior) != CHANGE_5M_Z_PRIOR_RETURNS:
        return None
    try:
        sigma = statistics.stdev(prior)
    except statistics.StatisticsError:
        return None
    if sigma != sigma or sigma <= _CHANGE_5M_Z_SIGMA_EPS:
        return None
    return current / sigma


def factors_from_klines(klines: Sequence[SpotKline]) -> dict[str, Any] | None:
    """Same tech_boll family as paper: change_*, volume_strength, BOLL(20, 2σ) on close.

    ``volume_strength`` is still ``indicators.technical.volume_strength`` on the
    trailing-1h quote series. Crowding fields and ``change_5m_z`` are attached
    afterwards and are not arguments to that function. BOLL stays 20, 2σ.
    """
    series = price_series_from_klines(klines)
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
        "quote_volume_24h": quote_volume_24h(klines),
        "sample_count": len(series),
        "kline_close_time_ms": max(bar.close_time_ms for bar in klines),
    }
    out.update(fields)
    out.update(crowding_shadow_from_klines(klines))
    out["change_5m_z"] = change_5m_z_from_klines(klines)
    return out


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create binance_* tables, then ADD COLUMN on ones that already exist.

    Does not migrate token_indicators, futures side tables, or any other
    table in the file. Repeat calls are no-ops once the columns are present.
    """
    script = _SQL_PATH.read_text(encoding="utf-8")
    conn.executescript(script)
    for table, columns in _ADDED_COLUMNS.items():
        existing = _table_columns(conn, table)
        if not existing:
            continue
        for name, typ in columns:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.commit()


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
    ensure_schema(conn)
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
            bar.taker_buy_quote_volume,
        )
        for bar in klines
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO binance_klines (
            symbol, interval, open_time_ms, open, high, low, close,
            volume, quote_volume, close_time_ms, trades, taker_buy_quote_volume
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def upsert_indicator(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    ensure_schema(conn)
    placeholders = ", ".join("?" for _ in _INDICATOR_COLS)
    names = ", ".join(_INDICATOR_COLS)
    conn.execute(
        f"INSERT OR REPLACE INTO binance_indicators ({names}) VALUES ({placeholders})",
        tuple(row.get(col) for col in _INDICATOR_COLS),
    )


def persist_klines_and_factors(
    conn: sqlite3.Connection,
    meta: SpotSymbol,
    klines: Sequence[SpotKline],
    *,
    interval: str = DEFAULT_INTERVAL,
    updated_at: str | None = None,
) -> dict[str, Any] | None:
    """Write one symbol's klines and tech_boll row. DEX tables are not touched."""
    factors = factors_from_klines(klines)
    if factors is None:
        return None
    now = updated_at or utc_now_iso()
    upsert_klines(conn, meta.symbol, interval, klines)
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
    limit = max(20, min(int(kline_limit), 1000))
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
