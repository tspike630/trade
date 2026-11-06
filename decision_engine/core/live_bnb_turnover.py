"""New-buy gate for the two Binance spot purposes.

A buy has to clear two checks the indicator cache does not:

1. The symbol is in the current primary universe snapshot
   (``symbols`` / ``BINANCE_SPOT_UNIVERSE_*``). That snapshot is the feed's
   TRADING spot-USDT book. A name the feed removed stays out, even when an
   old ``binance_indicators`` row is still on disk.
2. A fresh 24h USDT quote turnover is at least
   ``BINANCE_SPOT_MIN_QUOTE_VOLUME`` (default 3_000_000, the same env and
   default the universe refresh uses). The value is Binance spot ticker
   ``quoteVolume``: ``ccxt_ticks.quote_volume_24h`` with ``ts``, or universe
   ``symbols.quoteVolume_24h`` with ``quoteVolume_collected_at``. The
   timestamp is the collection time written by a successful ticker refresh.
   A ``quoteVolume_24h`` with no collected-at fails closed. File mtime, a
   migration clock, base volume, ``liquidity_usd``, a 5m bar, and
   ``binance_indicators.quote_volume_24h`` are not this quote.

Missing, non-finite, negative, undated, or stale quotes fail closed. Sells
are not filtered: a held name such as one dropped for low turnover still
gets its protective exit. This module does not cancel orders.

After those gates, each purpose keeps at most
``LIVE_BNB_TOP_BUYS`` new buys (default 2). A tick with one qualifier emits
that one buy and does not invent a second. Execution still holds one open
position per purpose; this cap does not open a second fill.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from logging_setup import get_logger

from decision_engine import config as de_config
from decision_engine.core.binance_indicators import binance_indicators_fresh_sec
from decision_engine.core.bnb_native import load_trading_usdt_symbols
from decision_engine.core.bnb_spot_map import classify_listed_symbol, default_listing_db_paths

log = get_logger("decision_engine.live_bnb_turnover")

ENV_MIN_QUOTE_VOLUME = "BINANCE_SPOT_MIN_QUOTE_VOLUME"
ENV_TOP_BUYS = "LIVE_BNB_TOP_BUYS"

SOURCE_TICKER = "ccxt_ticks.quote_volume_24h"
SOURCE_UNIVERSE = "binance_spot_universe.quoteVolume_24h"
# Same spelling the universe snapshot already uses, plus the collection clock.
UNIVERSE_QUOTE_COLUMN = "quoteVolume_24h"
UNIVERSE_COLLECTED_AT_COLUMN = "quoteVolume_collected_at"

REASON_UNIVERSE_UNAVAILABLE = "universe_unavailable"
REASON_UNIVERSE_NOT_ELIGIBLE = "universe_not_eligible"
REASON_SYMBOL_INVALID = "symbol_invalid"
REASON_TURNOVER_MISSING = "turnover_missing"
REASON_TURNOVER_INVALID = "turnover_invalid"
REASON_TURNOVER_STALE = "turnover_stale"
REASON_TURNOVER_TIME_MISSING = "turnover_time_missing"
REASON_TURNOVER_BELOW = "turnover_below_min"

# Lower-cased. ``quoteVolume_24h`` is the production column. Generic
# ``updated_at`` is not a collection time and does not qualify.
_QUOTE_COLUMNS = ("quotevolume_24h", "quote_volume_24h")
_TIME_COLUMNS = ("quotevolume_collected_at",)
_FUTURE_SKEW_MS = 120_000
_SAMPLE = 15
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_STAMP_KEYS = (
    "quote_turnover_24h_usdt",
    "quote_turnover_24h_source",
    "quote_turnover_24h_asof",
    "universe_member",
)


def min_quote_volume_usdt() -> float:
    """Floor shared with the universe refresh. Blank or unusable env keeps 3_000_000."""
    raw = os.environ.get(ENV_MIN_QUOTE_VOLUME)
    if raw is None or str(raw).strip() == "":
        return float(de_config.DEFAULT_BINANCE_SPOT_MIN_QUOTE_VOLUME)
    text = str(raw).strip().replace("_", "")
    try:
        value = float(text)
    except (TypeError, ValueError):
        log.warning("%s unusable %r; using 3000000", ENV_MIN_QUOTE_VOLUME, raw)
        return float(de_config.DEFAULT_BINANCE_SPOT_MIN_QUOTE_VOLUME)
    if not math.isfinite(value) or value <= 0:
        log.warning("%s unusable %r; using 3000000", ENV_MIN_QUOTE_VOLUME, raw)
        return float(de_config.DEFAULT_BINANCE_SPOT_MIN_QUOTE_VOLUME)
    return value


def top_buys_per_tick() -> int:
    """Maximum new buys one Binance purpose may emit. Default 2. Never pads."""
    raw = os.environ.get(ENV_TOP_BUYS)
    if raw is None or str(raw).strip() == "":
        return int(de_config.DEFAULT_LIVE_BNB_TOP_BUYS)
    text = str(raw).strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        log.warning("%s unusable %r; using default 2", ENV_TOP_BUYS, raw)
        return int(de_config.DEFAULT_LIVE_BNB_TOP_BUYS)
    if value < 1:
        log.warning("%s unusable %r; using default 2", ENV_TOP_BUYS, raw)
        return int(de_config.DEFAULT_LIVE_BNB_TOP_BUYS)
    return value


def turnover_fresh_ms() -> int:
    """Age window for a quote. A non-positive indicator window does not disable it."""
    seconds = binance_indicators_fresh_sec()
    if seconds <= 0:
        seconds = 3600
    return int(seconds) * 1000


def _finite_quote(value: Any) -> tuple[float | None, bool]:
    """``(number, invalid)``. Absent is ``(None, False)``. Negative / NaN / inf are invalid."""
    if value is None or value == "":
        return None, False
    if isinstance(value, bool):
        return None, True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, True
    if not math.isfinite(number) or number < 0:
        return None, True
    return number, False


def _asof_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        number = float(value)
        if number > 10_000_000_000:
            return int(number)
        if number > 1_000_000_000:
            return int(number * 1000)
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh(asof_ms: int, now_ms: int, window_ms: int) -> bool:
    if asof_ms > now_ms + _FUTURE_SKEW_MS:
        return False
    return now_ms - asof_ms <= window_ms


def _symbol_of(row: dict[str, Any]) -> str | None:
    for key in ("binance_symbol", "token_address", "symbol"):
        raw = row.get(key)
        if isinstance(raw, str) and raw.strip():
            spot = classify_listed_symbol(raw)
            if spot is not None:
                return spot
    return None


def _ident(name: str) -> str | None:
    if _IDENT.fullmatch(name):
        return '"' + name + '"'
    return None


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    out: dict[str, str] = {}
    for row in rows:
        name = str(row[1])
        out[name.lower()] = name
    return out


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _ro_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/") + "?mode=ro"


def _universe_paths(indicator_db: Path) -> list[Path]:
    """Same snapshot files ``load_trading_usdt_symbols`` consults, plus ticker side dbs."""
    paths: list[Path] = []
    env_db = os.environ.get("BINANCE_SPOT_UNIVERSE_DB", "").strip()
    if env_db:
        paths.append(Path(env_db))
    ignore = os.environ.get("LIVE_BNB_IGNORE_REPO_UNIVERSE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not ignore and not env_db:
        repo = Path(__file__).resolve().parents[2]
        paths.extend(
            (
                repo / "exports" / "binance_spot_universe.db",
                indicator_db.parent / "exports" / "binance_spot_universe.db",
                indicator_db.parent / "binance_spot_universe.db",
                repo / "crawler" / "exports" / "binance_spot_universe.db",
            )
        )
    paths.extend(default_listing_db_paths(indicator_db))
    out: list[Path] = []
    seen: set[Path] = set()
    indicator = indicator_db.resolve()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == indicator or resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _open_ro(path: Path) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        log.warning("turnover db open failed path=%s err=%s", path, exc)
        return None
    return conn


class _Obs:
    __slots__ = ("usdt", "asof_ms", "invalid", "time_missing")

    def __init__(
        self,
        usdt: float | None,
        asof_ms: int | None,
        *,
        invalid: bool,
        time_missing: bool,
    ):
        self.usdt = usdt
        self.asof_ms = asof_ms
        self.invalid = invalid
        self.time_missing = time_missing


def _obs_from_value(usdt_raw: Any, asof_raw: Any) -> _Obs | None:
    number, invalid = _finite_quote(usdt_raw)
    asof = _asof_ms(asof_raw)
    if invalid:
        return _Obs(None, asof, invalid=True, time_missing=False)
    if number is None:
        return None
    if asof is None:
        return _Obs(number, None, invalid=False, time_missing=True)
    return _Obs(number, asof, invalid=False, time_missing=False)


def _prefer_newer(current: _Obs | None, found: _Obs | None) -> _Obs | None:
    if found is None:
        return current
    if current is None:
        return found
    if found.asof_ms is None:
        return current if current.asof_ms is not None else found
    if current.asof_ms is None or found.asof_ms >= current.asof_ms:
        return found
    return current


def _read_ticks(conn: sqlite3.Connection) -> dict[str, _Obs]:
    try:
        names = _tables(conn)
    except sqlite3.Error as exc:
        log.warning("ticker table probe failed: %s", exc)
        return {}
    if "ccxt_ticks" not in names:
        return {}
    cols = _columns(conn, "ccxt_ticks")
    symbol_col = cols.get("symbol")
    quote_col = cols.get("quote_volume_24h")
    ts_col = cols.get("ts")
    exchange_col = cols.get("exchange")
    if not symbol_col or not quote_col or not ts_col or not exchange_col:
        return {}
    ident = [_ident(symbol_col), _ident(quote_col), _ident(ts_col), _ident(exchange_col)]
    if any(item is None for item in ident):
        return {}
    sql = (
        f"SELECT {ident[0]}, {ident[1]}, {ident[2]} FROM ccxt_ticks "
        f"WHERE lower({ident[3]}) = 'binance'"
    )
    try:
        fetched = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        log.warning("ticker read failed: %s", exc)
        return {}
    best: dict[str, _Obs] = {}
    for symbol, quote_raw, ts_raw in fetched:
        spot = classify_listed_symbol(None if symbol is None else str(symbol))
        if spot is None:
            continue
        found = _obs_from_value(quote_raw, ts_raw)
        if found is None:
            continue
        best[spot] = _prefer_newer(best.get(spot), found) or found
    return best


def _read_universe_quotes(conn: sqlite3.Connection) -> dict[str, _Obs]:
    try:
        names = _tables(conn)
    except sqlite3.Error as exc:
        log.warning("universe quote probe failed: %s", exc)
        return {}
    if "symbols" not in names:
        return {}
    cols = _columns(conn, "symbols")
    symbol_col = cols.get("symbol") or cols.get("binance_symbol")
    quote_col = next((cols[name] for name in _QUOTE_COLUMNS if name in cols), None)
    time_col = next((cols[name] for name in _TIME_COLUMNS if name in cols), None)
    if symbol_col is None or quote_col is None:
        return {}
    selected = [symbol_col, quote_col]
    if time_col is not None:
        selected.append(time_col)
    idents = [_ident(name) for name in selected]
    if any(item is None for item in idents):
        return {}
    sql = f"SELECT {', '.join(idents)} FROM symbols"
    try:
        fetched = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        log.warning("universe quote read failed: %s", exc)
        return {}
    best: dict[str, _Obs] = {}
    for row in fetched:
        symbol = row[0]
        quote_raw = row[1]
        asof_raw = row[2] if time_col is not None else None
        spot = classify_listed_symbol(None if symbol is None else str(symbol))
        if spot is None:
            continue
        found = _obs_from_value(quote_raw, asof_raw)
        if found is None:
            continue
        best[spot] = _prefer_newer(best.get(spot), found) or found
    return best


def _merge_obs(into: dict[str, _Obs], found: dict[str, _Obs]) -> None:
    for spot, obs in found.items():
        into[spot] = _prefer_newer(into.get(spot), obs) or obs


def _load_quotes(
    conn: sqlite3.Connection, indicator_db: Path
) -> tuple[dict[str, _Obs], dict[str, _Obs]]:
    tickers = _read_ticks(conn)
    universe_quotes = _read_universe_quotes(conn)
    for path in _universe_paths(indicator_db):
        side = _open_ro(path)
        if side is None:
            continue
        try:
            _merge_obs(tickers, _read_ticks(side))
            _merge_obs(universe_quotes, _read_universe_quotes(side))
        finally:
            side.close()
    return tickers, universe_quotes


def _classify(obs: _Obs | None, now_ms: int, window_ms: int) -> str:
    if obs is None:
        return "absent"
    if obs.invalid:
        return "invalid"
    if obs.time_missing or obs.asof_ms is None:
        return "time_missing"
    if not _fresh(obs.asof_ms, now_ms, window_ms):
        return "stale"
    return "fresh"


def _clears(amount: float, floor: float) -> bool:
    return amount >= floor


def evaluate_membership_and_turnover(
    row: dict[str, Any],
    *,
    primary: bool,
    members: set[str],
    tickers: dict[str, _Obs],
    universe_quotes: dict[str, _Obs],
    now_ms: int,
    window_ms: int,
    floor: float,
) -> tuple[dict[str, Any] | None, str]:
    """Return stamp fields, or ``(None, reason)``."""
    spot = _symbol_of(row)
    if spot is None:
        return None, REASON_SYMBOL_INVALID
    if not primary:
        return None, REASON_UNIVERSE_UNAVAILABLE
    if spot not in members:
        return None, REASON_UNIVERSE_NOT_ELIGIBLE
    ticker_state = _classify(tickers.get(spot), now_ms, window_ms)
    universe_state = _classify(universe_quotes.get(spot), now_ms, window_ms)
    chosen: _Obs | None = None
    source = ""
    if ticker_state == "fresh":
        chosen = tickers[spot]
        source = SOURCE_TICKER
    elif universe_state == "fresh":
        chosen = universe_quotes[spot]
        source = SOURCE_UNIVERSE
    elif ticker_state == "stale" or universe_state == "stale":
        return None, REASON_TURNOVER_STALE
    elif ticker_state == "invalid" or universe_state == "invalid":
        return None, REASON_TURNOVER_INVALID
    elif ticker_state == "time_missing" or universe_state == "time_missing":
        return None, REASON_TURNOVER_TIME_MISSING
    else:
        return None, REASON_TURNOVER_MISSING
    if chosen is None or chosen.usdt is None or chosen.asof_ms is None:
        return None, REASON_TURNOVER_MISSING
    if not _clears(chosen.usdt, floor):
        return None, REASON_TURNOVER_BELOW
    return {
        "quote_turnover_24h_usdt": chosen.usdt,
        "quote_turnover_24h_source": source,
        "quote_turnover_24h_asof": _iso(chosen.asof_ms),
        "universe_member": True,
        "binance_symbol": row.get("binance_symbol") or spot,
    }, ""


def filter_live_bnb_hot(
    conn: sqlite3.Connection,
    indicator_db: Path,
    rows: list[dict[str, Any]],
    *,
    now_ms: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop scan rows that cannot support a new buy. The scan list is not shortened first."""
    clock = int(time_ms() if now_ms is None else now_ms)
    window_ms = turnover_fresh_ms()
    floor = min_quote_volume_usdt()
    members, primary = load_trading_usdt_symbols(conn, indicator_db)
    tickers, universe_quotes = _load_quotes(conn, indicator_db)
    kept: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            reasons[REASON_SYMBOL_INVALID] += 1
            continue
        fields, reason = evaluate_membership_and_turnover(
            row,
            primary=primary,
            members=members,
            tickers=tickers,
            universe_quotes=universe_quotes,
            now_ms=clock,
            window_ms=window_ms,
            floor=floor,
        )
        spot = _symbol_of(row) or str(row.get("token_address") or "")
        if fields is None:
            reasons[reason] += 1
            bucket = samples.setdefault(reason, [])
            if len(bucket) < _SAMPLE:
                bucket.append(spot)
            continue
        copied = dict(row)
        copied.update(fields)
        kept.append(copied)
    report = {
        "scanned": len(rows),
        "passed": len(kept),
        "floor_usdt": floor,
        "universe_primary": primary,
        "universe_size": len(members),
        "reasons": dict(sorted(reasons.items())),
        "samples": {key: samples[key] for key in sorted(samples)},
    }
    log.info(
        "live_bnb_turnover scanned=%s passed=%s floor=%s universe_primary=%s "
        "universe_size=%s reasons=%s samples=%s",
        report["scanned"],
        report["passed"],
        floor,
        int(primary),
        len(members),
        report["reasons"],
        report["samples"],
    )
    return kept, report


def time_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def stamp_turnover(signals: list[dict[str, Any]], hot: list[dict[str, Any]]) -> None:
    """Copy a validated quote onto buys. Rows that failed the filter have nothing to copy."""
    by_token: dict[tuple[str, str], dict[str, Any]] = {}
    for row in hot:
        by_token[(str(row.get("chain_id") or ""), str(row.get("token_address") or ""))] = row
    for sig in signals:
        if str(sig.get("action") or "").lower() != "buy":
            continue
        row = by_token.get(
            (str(sig.get("chain_id") or ""), str(sig.get("token_address") or ""))
        )
        if row is None:
            continue
        for key in _STAMP_KEYS:
            if row.get(key) is not None:
                sig[key] = row.get(key)


def _rank_key(sig: dict[str, Any]) -> tuple[bool, float, float, str]:
    strength, invalid = _finite_quote(sig.get("volume_strength"))
    if invalid:
        strength = None
    turnover, _bad = _finite_quote(sig.get("quote_turnover_24h_usdt"))
    spot = _symbol_of(sig) or ""
    if strength is None:
        return (True, 0.0, -(turnover or 0.0), spot)
    return (False, -strength, -(turnover or 0.0), spot)


def _buy_still_valid(sig: dict[str, Any], *, now_ms: int, window_ms: int, floor: float) -> str | None:
    if sig.get("universe_member") is not True:
        return REASON_UNIVERSE_NOT_ELIGIBLE
    number, invalid = _finite_quote(sig.get("quote_turnover_24h_usdt"))
    if invalid:
        return REASON_TURNOVER_INVALID
    if number is None:
        return REASON_TURNOVER_MISSING
    asof = _asof_ms(sig.get("quote_turnover_24h_asof"))
    if asof is None:
        return REASON_TURNOVER_TIME_MISSING
    if not _fresh(asof, now_ms, window_ms):
        return REASON_TURNOVER_STALE
    if not _clears(number, floor):
        return REASON_TURNOVER_BELOW
    if not sig.get("quote_turnover_24h_source"):
        return REASON_TURNOVER_MISSING
    return None


def cap_live_bnb_buys(
    signals: list[dict[str, Any]],
    *,
    now_ms: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep every sell. Keep at most ``top_buys_per_tick`` new buys, strongest first.

    Ranking runs after eligibility, risk, and cooldown have already removed
    rows. A rejected or cooled-down name is not in ``signals`` and cannot take
    the slot. Duplicate symbols keep the higher rank.
    """
    clock = int(time_ms() if now_ms is None else now_ms)
    window_ms = turnover_fresh_ms()
    floor = min_quote_volume_usdt()
    limit = top_buys_per_tick()
    reasons: Counter[str] = Counter()
    eligible: list[dict[str, Any]] = []
    for sig in signals:
        if str(sig.get("action") or "").lower() != "buy":
            continue
        reason = _buy_still_valid(sig, now_ms=clock, window_ms=window_ms, floor=floor)
        if reason is not None:
            reasons[reason] += 1
            continue
        eligible.append(sig)
    ranked = sorted(eligible, key=_rank_key)
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sig in ranked:
        spot = _symbol_of(sig) or str(sig.get("token_address") or "")
        if spot in seen:
            continue
        seen.add(spot)
        chosen.append(sig)
        if len(chosen) >= limit:
            break
    chosen_ids = {id(sig) for sig in chosen}
    kept: list[dict[str, Any]] = []
    for sig in signals:
        if str(sig.get("action") or "").lower() != "buy" or id(sig) in chosen_ids:
            kept.append(sig)
    ranked_symbols = [
        _symbol_of(sig) or str(sig.get("token_address") or "") for sig in ranked
    ]
    selected_symbols = [
        _symbol_of(sig) or str(sig.get("token_address") or "") for sig in chosen
    ]
    report = {
        "eligible": len(ranked),
        "limit": limit,
        "ranked": ranked_symbols,
        "selected": selected_symbols,
        "reasons": dict(sorted(reasons.items())),
        "buys_kept": len(chosen),
        "sells_kept": sum(
            1 for sig in kept if str(sig.get("action") or "").lower() != "buy"
        ),
    }
    log.info(
        "live_bnb_top_buys eligible=%s limit=%s ranked=%s selected=%s dropped=%s",
        report["eligible"],
        limit,
        ranked_symbols,
        selected_symbols,
        report["reasons"],
    )
    return kept, report
