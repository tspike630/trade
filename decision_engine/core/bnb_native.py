"""In-process 5m kline factors for ``live_bnb_boll_fade``.

The engine calls this only when ``binance_indicators`` does not exist.
That table (PR #160) wins outright when it is present.

Candles are read from ``binance_klines`` (``interval``, ``open_time_ms``,
``quote_volume``) and then ``ccxt_ohlcv``. A collection snapshot
(``symbols`` / the TRADING JSON / ``BINANCE_SPOT_UNIVERSE_*``) restricts
which symbols are computed. An empty snapshot fails closed. With no
snapshot, every spot USDT id already in the kline tables is eligible.

``LIVE_BNB_KLINE_FETCH=1`` may GET public klines for snapshot symbols
that have no bars. ``LIVE_BNB_KLINE_FETCH_MAX`` (default 40) is a request
budget, not an entry gate. No API key.

``liquidity_usd`` is the trailing-24h quote notional so the existing
``DECISION_MIN_LIQ_USD`` floor still has a number. It is not a new gate.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote

from logging_setup import get_logger

from decision_engine.core.bnb_kline_fetch import fetch_public_klines
from decision_engine.core.bnb_spot_map import (
    BINANCE_QUOTE,
    BINANCE_VENUE,
    CEX_UNIVERSE,
    classify_listed_symbol,
    default_listing_db_paths,
    load_binance_spot_usdt_symbols,
)

log = get_logger("decision_engine.bnb_native")

SYMBOLS_TABLE = "symbols"
KLINE_TABLE = "binance_klines"
CCXT_KLINE_TABLE = "ccxt_ohlcv"
KLINE_INTERVAL = "5m"
KLINE_MS = 5 * 60 * 1000
CHANGE_5M_SEC = 5 * 60
VOL_WINDOW_SEC = 24 * 60 * 60
HOUR_SEC = 60 * 60
BOLL_LENGTH = 20
BOLL_STD = 2.0
FETCH_LIMIT = 300
_FETCH_MAX_DEFAULT = 40

_SYMBOL_COL_CANDIDATES = ("symbol", "binance_symbol", "spot_symbol")
_STATUS_COL_CANDIDATES = ("status", "trading_status")
_BASE_COL_CANDIDATES = ("base_asset", "base", "baseAsset")
_QUOTE_COL_CANDIDATES = ("quote_asset", "quote", "quoteAsset")
_SPOT_COL_CANDIDATES = ("is_spot", "isSpotTradingAllowed", "spot")

Candle = tuple[int, float, float, float, float, float, float | None]
KlineFetcher = Callable[[str], list[Candle]]

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE_SPOT = frozenset({"0", "false", "no", "off"})
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def kline_fetch_enabled() -> bool:
    return _env_flag("LIVE_BNB_KLINE_FETCH")


def _public_fetch(symbol: str) -> list[Candle]:
    return fetch_public_klines(symbol, interval=KLINE_INTERVAL, limit=FETCH_LIMIT)


def _fetch_max() -> int:
    raw = os.environ.get("LIVE_BNB_KLINE_FETCH_MAX", "").strip()
    if not raw:
        return _FETCH_MAX_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        return _FETCH_MAX_DEFAULT
    if n < 0:
        return 0
    return n


def _ro_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/") + "?mode=ro"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not _IDENT.match(table):
        return []
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row[1]) for row in rows if _IDENT.match(str(row[1]))]


def _pick(cols: Sequence[str], candidates: Sequence[str]) -> str | None:
    have = set(cols)
    for name in candidates:
        if name in have and _IDENT.match(name):
            return name
    return None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _spot_from_parts(
    symbol: Any,
    base: Any,
    quote: Any,
    status: Any,
    is_spot: Any,
) -> str | None:
    if status is not None and str(status).strip():
        if str(status).strip().upper() != "TRADING":
            return None
    if is_spot is not None and str(is_spot).strip():
        if str(is_spot).strip().lower() in _FALSE_SPOT:
            return None
    quote_text = None if quote is None else str(quote).strip()
    if quote_text and quote_text.upper() != BINANCE_QUOTE:
        return None
    base_text = None if base is None or str(base).strip() == "" else str(base)
    symbol_text = None if symbol is None or str(symbol).strip() == "" else str(symbol)
    if symbol_text is None and base_text is not None:
        symbol_text = f"{base_text}{BINANCE_QUOTE}"
    if symbol_text is None:
        return None
    if base_text:
        return classify_listed_symbol(
            symbol_text,
            base_text,
            quote_text or BINANCE_QUOTE,
        )
    return classify_listed_symbol(symbol_text)


def symbols_from_json_payload(payload: Any) -> set[str]:
    """TRADING spot USDT ids from the collection JSON snapshot."""
    rows: list[Any]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        found: list[Any] | None = None
        for key in ("symbols", "rows", "data", "universe"):
            value = payload.get(key)
            if isinstance(value, list):
                found = value
                break
        if found is None:
            return set()
        rows = found
    else:
        return set()
    out: set[str] = set()
    for item in rows:
        if isinstance(item, str):
            spot = _spot_from_parts(item, None, None, None, None)
        elif isinstance(item, dict):
            spot = _spot_from_parts(
                item.get("symbol") or item.get("binance_symbol") or item.get("spot_symbol"),
                item.get("base_asset", item.get("base", item.get("baseAsset"))),
                item.get("quote_asset", item.get("quote", item.get("quoteAsset"))),
                item.get("status", item.get("trading_status")),
                item.get("is_spot", item.get("isSpotTradingAllowed", item.get("spot"))),
            )
        else:
            spot = None
        if spot is not None:
            out.add(spot)
    return out


def symbols_from_connection(conn: sqlite3.Connection) -> tuple[bool, set[str]]:
    """``(table_present, TRADING spot USDT ids)`` from ``symbols``."""
    try:
        names = _table_names(conn)
    except sqlite3.Error as exc:
        log.warning("symbols table scan failed: %s", exc)
        return False, set()
    if SYMBOLS_TABLE not in names:
        return False, set()
    cols = _columns(conn, SYMBOLS_TABLE)
    symbol_col = _pick(cols, _SYMBOL_COL_CANDIDATES)
    if symbol_col is None:
        log.warning("symbols table has no symbol column")
        return True, set()
    status_col = _pick(cols, _STATUS_COL_CANDIDATES)
    base_col = _pick(cols, _BASE_COL_CANDIDATES)
    quote_col = _pick(cols, _QUOTE_COL_CANDIDATES)
    spot_col = _pick(cols, _SPOT_COL_CANDIDATES)
    select = [symbol_col]
    for col in (status_col, base_col, quote_col, spot_col):
        if col is not None:
            select.append(col)
    sql = f"SELECT {', '.join(select)} FROM {SYMBOLS_TABLE}"
    try:
        fetched = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        log.warning("symbols read failed: %s", exc)
        return True, set()
    out: set[str] = set()
    for row in fetched:
        values = list(row)
        symbol = values[0]
        cursor = 1

        def _take(present: bool) -> Any:
            nonlocal cursor
            if not present:
                return None
            value = values[cursor]
            cursor += 1
            return value

        status = _take(status_col is not None)
        base = _take(base_col is not None)
        quote = _take(quote_col is not None)
        is_spot = _take(spot_col is not None)
        spot = _spot_from_parts(symbol, base, quote, status, is_spot)
        if spot is not None:
            out.add(spot)
    return True, out


def _load_json_symbols(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("universe json unreadable path=%s err=%s", path, exc)
        return set()
    return symbols_from_json_payload(payload)


def _open_ro(path: Path) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        log.warning("universe db open failed path=%s err=%s", path, exc)
        return None
    conn.row_factory = sqlite3.Row
    return conn


def load_trading_usdt_symbols(
    conn: sqlite3.Connection,
    indicator_db: Path,
) -> tuple[set[str], bool]:
    """Return ``(symbols, primary_snapshot)``.

    ``primary_snapshot`` is true when a collection ``symbols`` table or
    JSON file was the source. An empty primary snapshot does not fall
    through to seeded listings.
    """
    ignore_repo = _env_flag("LIVE_BNB_IGNORE_REPO_UNIVERSE")
    repo = Path(__file__).resolve().parents[2]
    found: set[str] = set()
    primary = False

    table_on_conn, from_conn = symbols_from_connection(conn)
    if table_on_conn:
        primary = True
        found |= from_conn

    db_paths: list[Path] = []
    json_paths: list[Path] = []
    env_db = os.environ.get("BINANCE_SPOT_UNIVERSE_DB", "").strip()
    env_json = os.environ.get("BINANCE_SPOT_UNIVERSE_JSON", "").strip()
    if env_db:
        primary = True
        db_paths.append(Path(env_db))
    if env_json:
        primary = True
        json_paths.append(Path(env_json))
    if not ignore_repo and not env_db and not env_json:
        for path in (
            repo / "exports" / "binance_spot_universe.db",
            indicator_db.parent / "binance_spot_universe.db",
            repo / "crawler" / "exports" / "binance_spot_universe.db",
        ):
            if path.is_file() and path.resolve() != indicator_db.resolve():
                primary = True
                db_paths.append(path)
        for path in (
            repo / "exports" / "reports" / "binance_spot_usdt_trading_2026-11-05.json",
            indicator_db.parent / "binance_spot_usdt_trading_2026-11-05.json",
        ):
            if path.is_file():
                primary = True
                json_paths.append(path)

    seen_db: set[Path] = set()
    indicator_resolved = indicator_db.resolve()
    for path in db_paths:
        try:
            resolved = path.resolve()
        except OSError:
            primary = True
            continue
        if resolved in seen_db:
            continue
        seen_db.add(resolved)
        if not path.is_file():
            log.warning("universe db missing path=%s", path)
            continue
        if resolved == indicator_resolved:
            continue
        side = _open_ro(path)
        if side is None:
            continue
        try:
            present, got = symbols_from_connection(side)
            if present:
                found |= got
        finally:
            side.close()

    for path in json_paths:
        if not path.is_file():
            log.warning("universe json missing path=%s", path)
            continue
        found |= _load_json_symbols(path)

    if primary:
        return found, True
    seeded = load_binance_spot_usdt_symbols(
        conn,
        extra_paths=default_listing_db_paths(indicator_db),
    )
    return seeded, False


def _side_db_paths(indicator_db: Path) -> list[Path]:
    repo = Path(__file__).resolve().parents[2]
    candidates = [
        indicator_db.parent / "binance_spot_universe.db",
        indicator_db.parent / "binance_indicators.db",
        indicator_db.parent / "ccxt.db",
        repo / "exports" / "binance_spot_universe.db",
        repo / "exports" / "binance_indicators.db",
        repo / "ccxt.db",
        repo / "crawler" / "ccxt.db",
        repo / "crawler" / "exports" / "binance_spot_universe.db",
    ]
    env_db = os.environ.get("BINANCE_SPOT_UNIVERSE_DB", "").strip()
    if env_db:
        candidates.insert(0, Path(env_db))
    indicator = indicator_db.resolve()
    out: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == indicator or resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _iter_conns(
    conn: sqlite3.Connection,
    indicator_db: Path,
) -> list[tuple[str, sqlite3.Connection, bool]]:
    """``(label, connection, close_when_done)``. Adapter conn is first."""
    out: list[tuple[str, sqlite3.Connection, bool]] = [("indicator", conn, False)]
    for path in _side_db_paths(indicator_db):
        side = _open_ro(path)
        if side is not None:
            out.append((path.name, side, True))
    return out


def _row_get(row: sqlite3.Row | tuple[Any, ...], key: str, cols: Sequence[str]) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key]
    return row[list(cols).index(key)]


def _parse_candle_row(
    row: Any,
    cols: Sequence[str],
) -> Candle | None:
    def _col(*names: str) -> Any:
        for name in names:
            if name in cols:
                return _row_get(row, name, cols)
        return None

    ts = _finite(_col("open_time_ms", "ts", "open_time", "open_ms"))
    close = _finite(_col("close", "c"))
    if ts is None or close is None:
        return None
    op = _finite(_col("open", "o"))
    hi = _finite(_col("high", "h"))
    lo = _finite(_col("low", "l"))
    vol = _finite(_col("volume", "base_volume", "v"))
    quote_vol = _finite(_col("quote_volume", "quoteVolume", "q"))
    return (
        int(ts),
        op if op is not None else close,
        hi if hi is not None else close,
        lo if lo is not None else close,
        close,
        vol if vol is not None else 0.0,
        quote_vol,
    )


def load_klines(
    conn: sqlite3.Connection,
    indicator_db: Path,
    symbols: set[str] | None,
) -> tuple[dict[str, list[Candle]], dict[str, str]]:
    """5m candles keyed by ``BASEUSDT``, plus the table name that supplied them.

    ``symbols is None`` keeps every spot USDT id in the tables. A set
    restricts the read to that universe. ``binance_klines`` (PR #160:
    ``interval``, ``open_time_ms``) is read before ``ccxt_ohlcv``.
    """
    buckets: dict[str, dict[int, Candle]] = {}
    sources: dict[str, str] = {}
    for _label, side, close in _iter_conns(conn, indicator_db):
        try:
            names = _table_names(side)
            for table in (KLINE_TABLE, CCXT_KLINE_TABLE):
                if table not in names:
                    continue
                _read_kline_table(side, table, buckets, sources, symbols)
        except sqlite3.Error as exc:
            log.warning("kline read failed: %s", exc)
        finally:
            if close:
                side.close()
    out: dict[str, list[Candle]] = {}
    for spot, by_ts in buckets.items():
        if by_ts:
            out[spot] = [by_ts[ts] for ts in sorted(by_ts)]
    return out, sources


def _read_kline_table(
    conn: sqlite3.Connection,
    table: str,
    buckets: dict[str, dict[int, Candle]],
    sources: dict[str, str],
    restrict: set[str] | None,
) -> None:
    cols = _columns(conn, table)
    symbol_col = _pick(cols, _SYMBOL_COL_CANDIDATES)
    if symbol_col is None or "close" not in cols and "c" not in cols:
        return
    where: list[str] = []
    if "exchange" in cols:
        where.append("lower(exchange) = 'binance'")
    interval_col = _pick(cols, ("interval", "timeframe"))
    sql = f"SELECT {', '.join(cols)} FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    for raw in conn.execute(sql):
        if interval_col is not None:
            interval = _row_get(raw, interval_col, cols)
            if interval is not None and str(interval).strip().lower() not in ("", "5m"):
                continue
        symbol = _row_get(raw, symbol_col, cols)
        spot = classify_listed_symbol(None if symbol is None else str(symbol))
        if spot is None:
            continue
        if restrict is not None and spot not in restrict:
            continue
        candle = _parse_candle_row(raw, cols)
        if candle is None:
            continue
        if spot not in sources:
            sources[spot] = table
        buckets.setdefault(spot, {})
        buckets[spot].setdefault(candle[0], candle)


def _closed(candles: Sequence[Candle], now_ms: int) -> list[Candle]:
    kept = [bar for bar in candles if int(bar[0]) + KLINE_MS <= int(now_ms)]
    kept.sort(key=lambda bar: bar[0])
    return kept


def _change_5m(closes: Sequence[tuple[float, float]]) -> float | None:
    """Same rule as ``indicators.technical.change_pct`` for a 300s lookback.

    ``closes`` is ``(close_epoch_sec, price)`` ascending. Skew is
    ``max(lookback * 0.5, 120s)`` capped at 6h, which is 150s here.
    """
    priced = [(ep, px) for ep, px in closes if px > 0]
    if not priced:
        return None
    ep_now, p_now = priced[-1]
    target = ep_now - CHANGE_5M_SEC
    skew = 150.0
    best_before: tuple[float, float] | None = None
    best: tuple[float, float] | None = None
    for ep, px in priced:
        if ep <= target and (best_before is None or ep >= best_before[0]):
            best_before = (ep, px)
        skew_abs = abs(ep - target)
        if skew_abs <= skew and (best is None or skew_abs < best[0]):
            best = (skew_abs, px)
    p_then: float | None = None
    if best_before is not None and (target - best_before[0]) <= skew:
        p_then = best_before[1]
    elif best is not None:
        p_then = best[1]
    if p_then is None or p_then <= 0:
        return None
    return ((p_now - p_then) / p_then) * 100.0


def _volume_strength(points: Sequence[tuple[float, float]]) -> float | None:
    """``current / mean`` over the trailing 24h. Matches ``volume_strength``.

    Each point is ``(close_epoch_sec, trailing_1h_base_volume)``.
    """
    if not points:
        return None
    last_ep, current = points[-1]
    if current is None or current < 0:
        return None
    cutoff = last_ep - VOL_WINDOW_SEC
    vols = [vol for ep, vol in points if ep >= cutoff and vol >= 0]
    if not vols:
        return None
    mean_v = sum(vols) / len(vols)
    if mean_v <= 0:
        return None
    return current / mean_v


def _boll_lower(closes: Sequence[float]) -> float | None:
    """SMA(20) − 2 × population stdev. Same parameters as ``bbands(length=20, std=2)``."""
    if len(closes) < BOLL_LENGTH:
        return None
    window = list(closes[-BOLL_LENGTH:])
    mid = sum(window) / BOLL_LENGTH
    var = sum((px - mid) ** 2 for px in window) / BOLL_LENGTH
    return mid - BOLL_STD * math.sqrt(var)


def _trailing_hour_volume(bars: Sequence[Candle], index: int) -> float:
    close_ms = int(bars[index][0]) + KLINE_MS
    start = close_ms - HOUR_SEC * 1000
    total = 0.0
    j = index
    while j >= 0:
        bar_close = int(bars[j][0]) + KLINE_MS
        if bar_close <= start:
            break
        total += float(bars[j][5])
        j -= 1
    return total


def _quote_notional_24h(bars: Sequence[Candle]) -> float | None:
    if not bars:
        return None
    last_close = int(bars[-1][0]) + KLINE_MS
    start = last_close - VOL_WINDOW_SEC * 1000
    total = 0.0
    seen = False
    for bar in bars:
        if int(bar[0]) + KLINE_MS <= start:
            continue
        quote = bar[6]
        if quote is not None and quote >= 0:
            total += quote
            seen = True
            continue
        px = bar[4]
        vol = bar[5]
        if px > 0 and vol >= 0:
            total += vol * px
            seen = True
    if not seen:
        return None
    return total


def factors_from_klines(
    candles: Sequence[Candle],
    *,
    now_ms: int,
) -> dict[str, Any] | None:
    """Strategy fields from closed 5m bars. Missing history → None (skip the symbol)."""
    bars = _closed(candles, now_ms)
    if len(bars) < 2:
        return None
    series: list[tuple[float, float, float]] = []
    for index, bar in enumerate(bars):
        close_sec = (int(bar[0]) + KLINE_MS) / 1000.0
        series.append((close_sec, float(bar[4]), _trailing_hour_volume(bars, index)))
    change = _change_5m([(ep, px) for ep, px, _vol in series])
    strength = _volume_strength([(ep, vol) for ep, _px, vol in series])
    lower = _boll_lower([px for _ep, px, _vol in series])
    price = series[-1][1]
    if change is None or strength is None or lower is None:
        return None
    return {
        "change_5m": change,
        "volume_strength": strength,
        "boll_lower": lower,
        "price_usd": price,
        "liquidity_usd": _quote_notional_24h(bars),
        "kline_interval": KLINE_INTERVAL,
    }


def _identity(spot: str, factors: dict[str, Any], *, factor_source: str) -> dict[str, Any]:
    base = spot[: -len(BINANCE_QUOTE)]
    row: dict[str, Any] = {
        "chain_id": BINANCE_VENUE,
        "token_address": spot,
        "primary_pair_address": f"{base}/{BINANCE_QUOTE}",
        "binance_symbol": spot,
        "ccxt_symbol": f"{base}/{BINANCE_QUOTE}",
        "venue": BINANCE_VENUE,
        "market": "spot",
        "quote_asset": BINANCE_QUOTE,
        "base_asset": base,
        "universe": CEX_UNIVERSE,
        "factor_source": factor_source,
    }
    row.update(factors)
    return row


def limit_kline_scan(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Most negative ``change_5m`` first. Same scan window as ``binance_indicators``, not a gate."""

    def _key(row: dict[str, Any]) -> tuple[int, float]:
        change = _finite(row.get("change_5m"))
        if change is None:
            return (1, 0.0)
        return (0, change)

    ordered = sorted(rows, key=_key)
    return ordered[: max(1, int(limit))]


def load_native_candidate_rows(
    conn: sqlite3.Connection,
    indicator_db: Path,
    *,
    now_ms: int | None = None,
    fetch_klines: KlineFetcher | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Candidate rows for ``TechBollFadeStrategy``. Empty feed → ``([], stats)``."""
    clock = int(time.time() * 1000) if now_ms is None else int(now_ms)
    symbols, primary = load_trading_usdt_symbols(conn, indicator_db)
    # Seeded DEX listings are not this book. No collection snapshot means
    # "whatever binance_klines already holds". An empty snapshot fails closed.
    restrict: set[str] | None = symbols if primary else None
    stats = {
        "listed": len(symbols) if primary else 0,
        "primary_snapshot": int(primary),
        "indicator_rows": 0,
        "kline_symbols": 0,
        "fetched": 0,
        "candidates": 0,
    }
    if primary and not symbols:
        log.info("binance_native fail closed: empty TRADING USDT snapshot")
        return [], stats

    klines, kline_sources = load_klines(conn, indicator_db, restrict)
    stats["kline_symbols"] = len(klines)
    if not primary:
        stats["listed"] = len(klines)

    fetcher = fetch_klines
    if fetcher is None and kline_fetch_enabled() and restrict:
        fetcher = _public_fetch
    fetched: set[str] = set()
    if fetcher is not None and restrict:
        budget = _fetch_max()
        for spot in sorted(restrict):
            if budget <= 0:
                break
            if klines.get(spot):
                continue
            try:
                candles = fetcher(spot) or []
            except Exception as exc:  # noqa: BLE001 — one symbol must not kill the tick
                log.warning("kline fetch raised symbol=%s err=%s", spot, exc)
                candles = []
            budget -= 1
            stats["fetched"] += 1
            if candles:
                klines[spot] = sorted(candles, key=lambda bar: int(bar[0]))
                fetched.add(spot)

    rows: list[dict[str, Any]] = []
    for spot in sorted(klines):
        computed = factors_from_klines(klines.get(spot) or [], now_ms=clock)
        if computed is None:
            continue
        if spot in fetched:
            source = "binance_kline_fetch"
        else:
            source = kline_sources.get(spot, KLINE_TABLE)
        rows.append(_identity(spot, computed, factor_source=source))
    stats["candidates"] = len(rows)
    if not rows:
        log.info(
            "binance_native fail closed: listed=%s indicator_rows=%s kline_symbols=%s fetched=%s",
            stats["listed"],
            stats["indicator_rows"],
            stats["kline_symbols"],
            stats["fetched"],
        )
    else:
        log.info(
            "binance_native listed=%s indicator_rows=%s kline_symbols=%s fetched=%s candidates=%s primary=%s",
            stats["listed"],
            stats["indicator_rows"],
            stats["kline_symbols"],
            stats["fetched"],
            stats["candidates"],
            stats["primary_snapshot"],
        )
    return rows, stats
