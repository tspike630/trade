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
    "taker_buy_quote_volume",
    "taker_buy_ratio",
    "sample_count",
    "kline_close_time_ms",
    "tier",
    "tech_updated_at",
    "updated_at",
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
    taker_buy_base_volume: float | None = None
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
    taker_buy_base: float | None = None
    if len(raw) > 9 and raw[9] not in (None, ""):
        parsed_tb = _float_or_none(raw[9])
        if parsed_tb is not None and parsed_tb >= 0:
            taker_buy_base = parsed_tb
    taker_buy_quote: float | None = None
    if len(raw) > 10 and raw[10] not in (None, ""):
        parsed_tq = _float_or_none(raw[10])
        if parsed_tq is not None and parsed_tq >= 0:
            taker_buy_quote = parsed_tq
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
        taker_buy_base_volume=taker_buy_base,
        taker_buy_quote_volume=taker_buy_quote,
    )


def taker_buy_ratio_from_kline(bar: SpotKline) -> float | None:
    """Active taker-buy notional share in ``[0, 1]`` for one kline bar."""
    if bar.taker_buy_quote_volume is not None and bar.quote_volume > 0:
        ratio = bar.taker_buy_quote_volume / bar.quote_volume
        if 0.0 <= ratio <= 1.0 + 1e-6:
            return min(1.0, max(0.0, ratio))
    if bar.taker_buy_base_volume is not None and bar.volume > 0:
        ratio = bar.taker_buy_base_volume / bar.volume
        if 0.0 <= ratio <= 1.0 + 1e-6:
            return min(1.0, max(0.0, ratio))
    return None


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


def _closed_klines(
    klines: Sequence[SpotKline],
    *,
    now_ms: int | None = None,
) -> list[SpotKline]:
    if now_ms is None:
        return list(klines)
    cutoff = int(now_ms)
    return [bar for bar in klines if int(bar.close_time_ms) <= cutoff]


def factors_from_klines(
    klines: Sequence[SpotKline],
    *,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Same tech_boll family as paper: change_*, volume_strength, BOLL(20, 2σ) on close."""
    closed = _closed_klines(klines, now_ms=now_ms)
    series = price_series_from_klines(closed)
    if not series:
        return None
    last_bar = max(closed, key=lambda bar: (bar.close_time_ms, bar.open_time_ms))
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
        "taker_buy_quote_volume": last_bar.taker_buy_quote_volume,
        "taker_buy_ratio": taker_buy_ratio_from_kline(last_bar),
        "sample_count": len(series),
        "kline_close_time_ms": last_bar.close_time_ms,
    }
    out.update(fields)
    return out


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    present = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in present:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create binance_* tables. Does not migrate token_indicators."""
    script = _SQL_PATH.read_text(encoding="utf-8")
    conn.executescript(script)
    _ensure_column(conn, "binance_klines", "taker_buy_base_volume", "REAL")
    _ensure_column(conn, "binance_klines", "taker_buy_quote_volume", "REAL")
    _ensure_column(conn, "binance_indicators", "taker_buy_quote_volume", "REAL")
    _ensure_column(conn, "binance_indicators", "taker_buy_ratio", "REAL")


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
            bar.taker_buy_base_volume,
            bar.taker_buy_quote_volume,
        )
        for bar in klines
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO binance_klines (
            symbol, interval, open_time_ms, open, high, low, close,
            volume, quote_volume, close_time_ms, trades,
            taker_buy_base_volume, taker_buy_quote_volume
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Write one symbol's closed klines and tech_boll row. DEX tables are not touched."""
    closed = _closed_klines(klines, now_ms=now_ms)
    factors = factors_from_klines(closed)
    if factors is None:
        return None
    now = updated_at or utc_now_iso()
    upsert_klines(conn, meta.symbol, interval, closed)
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
        clock_ms = int(time.time() * 1000)
        bars = [
            bar
            for row in payload
            if (bar := parse_kline_row(row)) is not None and bar.close_time_ms <= clock_ms
        ]
        if not bars:
            stats["skipped"] += 1
            continue
        try:
            stored = persist_klines_and_factors(conn, meta, bars, interval=interval, now_ms=clock_ms)
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
