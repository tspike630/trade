"""Closed 1h Binance spot klines only. Does not write ``binance_indicators``.

The 5m factor job (``python -m indicators.binance_spot``) stays as it is.
``persist_klines_and_factors`` also writes ``binance_indicators``, whose
primary key is ``symbol`` alone, so an hourly run through that function would
replace the live 5m factor row. This collector calls ``upsert_klines`` only.

Symbols are the same landed TRADING spot-USDT universe as the 5m job.
No exchangeInfo scan and no invented stablecoin names. Bars come from
``GET /api/v3/klines?interval=1h``. They are not resampled from 5m.

``quote_volume`` is kline field index 7. A bar is stored only when
``close_time_ms - open_time_ms == 3599999``, ``close_time_ms <= now``, and
quote volume is finite and nonnegative. The current unclosed hour is dropped.
The latest stored run must be 21 contiguous closed hours so the decision
engine can compute ``volume_z`` later. This module does not define or write Z.

Repo checkout:

```bash
PYTHONPATH=crawler python -m indicators.binance_spot_1h_klines --once --db crawler/dexscreener.db
PYTHONPATH=crawler python -m indicators.binance_spot_1h_klines --loop --sleep-sec 300 --db crawler/dexscreener.db
```

Flattened deploy root (``indicators/`` next to ``exports/binance_spot_universe.db``):

```bash
cd /workspace/dexscreener-crawler
PYTHONPATH=. python -m indicators.binance_spot_1h_klines --once --db dexscreener.db
PYTHONPATH=. python -m indicators.binance_spot_1h_klines --loop --sleep-sec 300 --db dexscreener.db
```

``BINANCE_SPOT_UNIVERSE_DB`` still wins when it is set, and it is the only
file read. After the first existing candidate is chosen, a later missing,
unreadable, or empty file fails that round. The loop does not open the next
candidate. This module does not change the 5m wrapper.

``--db`` is the indicator sqlite that already holds ``binance_klines``.
One process holds ``<db>.binance-1h-klines.lock``. A second start exits 3.
The 5m writer is a different process and is not stopped by this lock.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Sequence

from indicators.binance_spot import (
    SpotKline,
    SpotSymbol,
    canonical_spot_usdt,
    ensure_schema,
    fetch_json,
    filter_universe,
    kline_url,
    parse_kline_row,
    symbols_from_connection,
    symbols_from_json,
    upsert_klines,
)

_UNIVERSE_DB_NAME = "binance_spot_universe.db"
_UNIVERSE_JSON_NAME = "binance_spot_usdt_trading_2026-11-05.json"

HOUR_MS = 3_600_000
BAR_DURATION_MS = 3_599_999
MIN_CLOSED_BARS = 21
FETCH_LIMIT = 30
KLINE_INTERVAL = "1h"
DEFAULT_SLEEP_SEC = 300
MAX_FETCH_ATTEMPTS = 3
BACKOFF_BASE_SEC = 0.4
BACKOFF_CAP_SEC = 8.0
LOCK_BUSY_EXIT = 3
PARTIAL_EXIT = 2


class KlineLockBusy(RuntimeError):
    """Another hourly kline collector holds the lock."""


def validate_closed_1h_bar(bar: SpotKline, now_ms: int) -> str | None:
    """Return a rejection reason, or None when the bar may be stored."""
    if int(bar.close_time_ms) - int(bar.open_time_ms) != BAR_DURATION_MS:
        return "duration"
    if int(bar.close_time_ms) > int(now_ms):
        return "unclosed"
    quote = bar.quote_volume
    if isinstance(quote, bool) or not isinstance(quote, (int, float)) or not math.isfinite(quote):
        return "quote"
    if quote < 0:
        return "quote"
    return None


def latest_closed_open_ms(now_ms: int) -> int:
    """Open time of the newest 1h bar whose close is already due."""
    latest = int(now_ms) - BAR_DURATION_MS
    if latest < 0:
        return 0
    return (latest // HOUR_MS) * HOUR_MS


def contiguous_closed_1h(
    bars: Sequence[SpotKline],
    *,
    now_ms: int,
) -> tuple[list[SpotKline], str | None]:
    """Closed hours ending at the latest hour that should already be closed.

    An older run of 21 bars is not a substitute. If that latest bar is missing
    or invalid, nothing is stored.
    """
    expected = latest_closed_open_ms(now_ms)
    by_open: dict[int, SpotKline] = {}
    for bar in bars:
        by_open[int(bar.open_time_ms)] = bar
    latest = by_open.get(expected)
    if latest is None:
        return [], "missing_latest_closed_hour"
    reason = validate_closed_1h_bar(latest, now_ms)
    if reason is not None:
        return [], f"invalid_latest_closed_hour:{reason}"
    tail = [latest]
    cursor = expected
    while True:
        prev = by_open.get(cursor - HOUR_MS)
        if prev is None or validate_closed_1h_bar(prev, now_ms) is not None:
            break
        tail.insert(0, prev)
        cursor -= HOUR_MS
    if len(tail) < MIN_CLOSED_BARS:
        return [], "need_21_contiguous_closed_1h"
    return tail, None


def _fetch_payload(
    fetcher: Callable[[str], Any],
    url: str,
    *,
    sleeper: Callable[[float], None],
) -> Any:
    delay = BACKOFF_BASE_SEC
    last_exc: Exception | None = None
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            return fetcher(url)
        except Exception as exc:  # noqa: BLE001 — bounded retry, then record
            last_exc = exc
            if attempt + 1 >= MAX_FETCH_ATTEMPTS:
                break
            sleeper(delay)
            delay = min(BACKOFF_CAP_SEC, delay * 2)
    assert last_exc is not None
    raise last_exc


def ingest_closed_1h_klines(
    conn: sqlite3.Connection,
    symbols: Sequence[SpotSymbol],
    *,
    fetcher: Callable[[str], Any],
    now_ms: int,
    sleeper: Callable[[float], None] | None = None,
    base_url: str = "https://api.binance.com",
) -> dict[str, Any]:
    """Upsert closed 1h klines. Does not write ``binance_indicators`` or delete rows."""
    pause = sleeper or (lambda _seconds: None)
    ensure_schema(conn)
    errors: list[dict[str, str]] = []
    stored_symbols = 0
    stored_bars = 0
    for meta in symbols:
        url = kline_url(meta.symbol, interval=KLINE_INTERVAL, limit=FETCH_LIMIT, base=base_url)
        try:
            payload = _fetch_payload(fetcher, url, sleeper=pause)
        except Exception as exc:  # noqa: BLE001
            errors.append({"symbol": meta.symbol, "error": f"fetch:{type(exc).__name__}:{exc}"})
            continue
        if not isinstance(payload, list):
            errors.append({"symbol": meta.symbol, "error": "payload_not_list"})
            continue
        parsed = [bar for row in payload if (bar := parse_kline_row(row)) is not None]
        accepted, reason = contiguous_closed_1h(parsed, now_ms=now_ms)
        if reason is not None or not accepted:
            errors.append({"symbol": meta.symbol, "error": reason or "rejected"})
            continue
        if any(bar.close_time_ms > now_ms for bar in accepted):
            errors.append({"symbol": meta.symbol, "error": "unclosed"})
            continue
        try:
            conn.execute("BEGIN")
            upsert_klines(conn, meta.symbol, KLINE_INTERVAL, accepted)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            errors.append({"symbol": meta.symbol, "error": f"sqlite:{exc}"})
            continue
        stored_symbols += 1
        stored_bars += len(accepted)
    return {
        "symbols": stored_symbols,
        "klines": stored_bars,
        "failed": len(errors),
        "errors": errors,
        "indicators": 0,
    }


def hourly_universe_paths(module_file: Path | None = None) -> list[Path]:
    """Universe files for a repo checkout and a flattened deploy root.

    ``indicators/binance_spot.py`` resolves the repo as its grandparent, which
    is correct in this checkout and wrong when the package sits directly in
    ``/workspace/dexscreener-crawler``. This list checks the directory that
    contains ``indicators/`` first.
    """
    here = (module_file or Path(__file__)).resolve()
    package_parent = here.parents[1]
    above = here.parents[2]
    paths: list[Path] = []
    env_db = os.environ.get("BINANCE_SPOT_UNIVERSE_DB", "").strip()
    env_json = os.environ.get("BINANCE_SPOT_UNIVERSE_JSON", "").strip()
    if env_db:
        paths.append(Path(env_db))
    paths.extend(
        (
            package_parent / "exports" / _UNIVERSE_DB_NAME,
            above / "exports" / _UNIVERSE_DB_NAME,
            above / "crawler" / "exports" / _UNIVERSE_DB_NAME,
        )
    )
    if env_json:
        paths.append(Path(env_json))
    paths.extend(
        (
            package_parent / "exports" / "reports" / _UNIVERSE_JSON_NAME,
            above / "exports" / "reports" / _UNIVERSE_JSON_NAME,
            above / "crawler" / "exports" / "reports" / _UNIVERSE_JSON_NAME,
        )
    )
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


class UniverseSourceError(RuntimeError):
    """The already selected universe file cannot be used this round."""


def authoritative_universe_path(module_file: Path | None = None) -> Path | None:
    """Pick the universe file once. A set env path wins even when it is absent.

    Later candidates are only for layout discovery: a missing flattened file
    may fall through to the repo checkout. An existing file is the selection,
    including when it is unreadable or empty.
    """
    env_db = os.environ.get("BINANCE_SPOT_UNIVERSE_DB", "").strip()
    if env_db:
        return Path(env_db)
    for path in hourly_universe_paths(module_file):
        if path.is_file():
            return path
    return None


def _symbols_from_sqlite_strict(path: Path) -> list[SpotSymbol]:
    """Read one sqlite universe. A malformed file raises instead of looking empty."""
    uri = "file:" + urllib.parse.quote(path.resolve().as_posix(), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return symbols_from_connection(conn)
    finally:
        conn.close()


def read_selected_universe(path: Path) -> tuple[list[SpotSymbol], Path]:
    """Read ``path`` only. Missing, unreadable, and empty are failures."""
    if not path.is_file():
        raise UniverseSourceError(f"universe_missing:{path}")
    try:
        if path.suffix.lower() == ".json":
            rows = symbols_from_json(path)
        else:
            rows = _symbols_from_sqlite_strict(path)
    except (OSError, sqlite3.Error, json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise UniverseSourceError(
            f"universe_read_error:{type(exc).__name__}:{exc}:{path}"
        ) from exc
    if not rows:
        raise UniverseSourceError(f"universe_empty:{path}")
    return rows, path


class StickyHourlyUniverse:
    """Re-read the selected universe file. Do not switch to another candidate."""

    def __init__(self, module_file: Path | None = None) -> None:
        self.module_file = module_file
        self.path: Path | None = None

    def __call__(self) -> tuple[list[SpotSymbol], Path | None]:
        env_db = os.environ.get("BINANCE_SPOT_UNIVERSE_DB", "").strip()
        if env_db:
            self.path = Path(env_db)
        elif self.path is None:
            self.path = authoritative_universe_path(self.module_file)
        if self.path is None:
            raise UniverseSourceError("universe_unavailable")
        return read_selected_universe(self.path)


def load_hourly_universe(
    module_file: Path | None = None,
) -> tuple[list[SpotSymbol], Path | None]:
    path = authoritative_universe_path(module_file)
    if path is None:
        return [], None
    try:
        return read_selected_universe(path)
    except UniverseSourceError:
        return [], None


def acquire_hourly_lock(db_path: Path):
    path = Path(str(db_path) + ".binance-1h-klines.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise KlineLockBusy(str(path)) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(Path(db_path)))
    handle.flush()
    return handle


def _round_symbols(
    symbols: Sequence[SpotSymbol],
    *,
    universe_loader: Callable[[], tuple[list[SpotSymbol], Path | None]] | None,
    symbol_allow: Sequence[str] | None,
) -> tuple[list[SpotSymbol], list[dict[str, str]], int | None]:
    """Return this round's symbols. A failed universe read does not reuse an older list."""
    if universe_loader is None:
        return list(symbols), [], len(symbols)
    try:
        rows, source = universe_loader()
    except UniverseSourceError as exc:
        return [], [{"symbol": "*", "error": str(exc)}], None
    except Exception as exc:  # noqa: BLE001 — do not keep the previous round's names
        return [], [{"symbol": "*", "error": f"universe_read_error:{type(exc).__name__}:{exc}"}], None
    if source is None:
        return [], [{"symbol": "*", "error": "universe_unavailable"}], None
    chosen = filter_universe(rows, only=symbol_allow)
    errors: list[dict[str, str]] = []
    if symbol_allow:
        present = {item.symbol for item in rows}
        for raw in symbol_allow:
            parsed = canonical_spot_usdt(raw)
            if parsed is not None and parsed[0] not in present:
                errors.append({"symbol": parsed[0], "error": "symbol_not_in_universe"})
    return chosen, errors, len(rows)


def run_hourly_collector(
    db_path: Path,
    symbols: Sequence[SpotSymbol] = (),
    *,
    fetcher: Callable[[str], Any],
    now_ms: int | None = None,
    loop: bool = False,
    sleep_sec: int = DEFAULT_SLEEP_SEC,
    max_rounds: int = 0,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    universe_loader: Callable[[], tuple[list[SpotSymbol], Path | None]] | None = None,
    symbol_allow: Sequence[str] | None = None,
) -> int:
    """One pass, or a loop. Each round re-reads the universe when a loader is set."""
    try:
        lock = acquire_hourly_lock(db_path)
    except KlineLockBusy as exc:
        print(f"binance_1h_klines_lock_busy {exc}", file=sys.stderr)
        return LOCK_BUSY_EXIT
    pause = sleeper or time.sleep
    wall = clock or time.time
    rounds = 0
    last = 0
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            while True:
                started = wall()
                current_ms = int(now_ms) if now_ms is not None else int(wall() * 1000)
                round_symbols, uni_errors, universe_n = _round_symbols(
                    symbols,
                    universe_loader=universe_loader,
                    symbol_allow=symbol_allow,
                )
                try:
                    if uni_errors and not round_symbols:
                        stats = {
                            "symbols": 0,
                            "klines": 0,
                            "failed": len(uni_errors),
                            "errors": uni_errors,
                            "indicators": 0,
                        }
                    else:
                        stats = ingest_closed_1h_klines(
                            conn,
                            round_symbols,
                            fetcher=fetcher,
                            now_ms=current_ms,
                            sleeper=pause,
                        )
                        if uni_errors:
                            stats["errors"] = list(stats["errors"]) + uni_errors
                            stats["failed"] = len(stats["errors"])
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    stats = {"symbols": 0, "klines": 0, "failed": 1, "errors": [{"symbol": "*", "error": str(exc)}], "indicators": 0}
                last = 0 if not stats["errors"] else PARTIAL_EXIT
                print(
                    f"binance_1h_klines universe={universe_n if universe_n is not None else 'unavailable'} "
                    f"symbols={stats['symbols']} klines={stats['klines']} "
                    f"failed={stats['failed']} indicators={stats['indicators']} exit={last}"
                )
                for err in stats["errors"]:
                    print(
                        f"binance_1h_kline_error symbol={err['symbol']} {err['error']}",
                        file=sys.stderr,
                    )
                rounds += 1
                if not loop:
                    return last
                if max_rounds and rounds >= max_rounds:
                    return last
                elapsed = max(0.0, wall() - started)
                wait = max(0.0, float(sleep_sec) - elapsed)
                if last != 0:
                    wait += min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** min(rounds, 4)))
                pause(wait)
        finally:
            conn.close()
    finally:
        lock.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Store closed 1h Binance spot klines. Does not write binance_indicators or volume_z."
    )
    parser.add_argument("--db", required=True, help="Indicator sqlite that already has binance_klines")
    parser.add_argument("--once", action="store_true", help="Single pass (default when --loop is absent)")
    parser.add_argument("--loop", action="store_true", help="Repeat. One bad symbol does not stop the next round.")
    parser.add_argument("--sleep-sec", type=int, default=DEFAULT_SLEEP_SEC)
    parser.add_argument("--max-rounds", type=int, default=0)
    parser.add_argument("--symbol", action="append", default=[], help="Universe symbol to keep. Repeatable.")
    parser.add_argument("--now-ms", type=int, default=0, help="Test clock. 0 uses the current time. Not a bar timestamp.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.sleep_sec < 1:
        parser.error("--sleep-sec must be >= 1")
    # The allowlist is fixed. The universe file is read again every round so a
    # name that appears after startup is fetched, and one that disappears is not.
    allow = list(args.symbol) if args.symbol else None
    now_ms = int(args.now_ms) if int(args.now_ms) > 0 else None
    return run_hourly_collector(
        Path(args.db),
        (),
        fetcher=fetch_json,
        now_ms=now_ms,
        loop=bool(args.loop),
        sleep_sec=int(args.sleep_sec),
        max_rounds=int(args.max_rounds),
        universe_loader=StickyHourlyUniverse(),
        symbol_allow=allow,
    )


if __name__ == "__main__":
    raise SystemExit(main())
