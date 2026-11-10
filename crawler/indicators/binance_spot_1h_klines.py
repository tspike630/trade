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

Run from the repo root:

```bash
PYTHONPATH=crawler python -m indicators.binance_spot_1h_klines --once --db crawler/dexscreener.db
PYTHONPATH=crawler python -m indicators.binance_spot_1h_klines --loop --sleep-sec 300 --db crawler/dexscreener.db
```

``--db`` is the indicator sqlite that already holds ``binance_klines``.
One process holds ``<db>.binance-1h-klines.lock``. A second start exits 3.
The 5m writer is a different process and is not stopped by this lock.
"""

from __future__ import annotations

import argparse
import fcntl
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from indicators.binance_spot import (
    SpotKline,
    SpotSymbol,
    ensure_schema,
    fetch_json,
    filter_universe,
    kline_url,
    load_primary_universe,
    parse_kline_row,
    upsert_klines,
)

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


def contiguous_closed_1h(
    bars: Sequence[SpotKline],
    *,
    now_ms: int,
) -> tuple[list[SpotKline], str | None]:
    """Trailing run of valid closed hours. Shorter than 21 is not stored."""
    valid = [
        bar
        for bar in sorted(bars, key=lambda item: item.open_time_ms)
        if validate_closed_1h_bar(bar, now_ms) is None
    ]
    if not valid:
        return [], "no_closed_1h"
    tail = [valid[-1]]
    for bar in reversed(valid[:-1]):
        if tail[0].open_time_ms - bar.open_time_ms != HOUR_MS:
            break
        tail.insert(0, bar)
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


def run_hourly_collector(
    db_path: Path,
    symbols: Sequence[SpotSymbol],
    *,
    fetcher: Callable[[str], Any],
    now_ms: int | None = None,
    loop: bool = False,
    sleep_sec: int = DEFAULT_SLEEP_SEC,
    max_rounds: int = 0,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    """One pass, or a loop. A symbol failure does not delete stored klines."""
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
                try:
                    stats = ingest_closed_1h_klines(
                        conn,
                        symbols,
                        fetcher=fetcher,
                        now_ms=current_ms,
                        sleeper=pause,
                    )
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    stats = {"symbols": 0, "klines": 0, "failed": 1, "errors": [{"symbol": "*", "error": str(exc)}], "indicators": 0}
                last = 0 if not stats["errors"] else PARTIAL_EXIT
                print(
                    f"binance_1h_klines symbols={stats['symbols']} klines={stats['klines']} "
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
    symbols, source = load_primary_universe()
    if not symbols or source is None:
        print("binance primary universe missing", file=sys.stderr)
        return PARTIAL_EXIT
    chosen = filter_universe(symbols, only=args.symbol or None)
    if not chosen:
        print("no universe symbols selected", file=sys.stderr)
        return PARTIAL_EXIT
    now_ms = int(args.now_ms) if int(args.now_ms) > 0 else int(time.time() * 1000)
    return run_hourly_collector(
        Path(args.db),
        chosen,
        fetcher=fetch_json,
        now_ms=now_ms,
        loop=bool(args.loop),
        sleep_sec=int(args.sleep_sec),
        max_rounds=int(args.max_rounds),
    )


if __name__ == "__main__":
    raise SystemExit(main())
