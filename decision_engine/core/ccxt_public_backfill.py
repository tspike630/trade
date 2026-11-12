"""Public CCXT 5m backfill. No API key and no invented bars.

OKX and Bybit are USDT spot. Coinbase Exchange is USD spot. A USD close is
not stored as USDT and is not paired with a USDT bar. Missing minutes stay
missing. ``first_seen`` is not written, because it is not an official listing.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from decision_engine.core.vision_backfill import CORE_BASES, RateLimiter

STEP_MS = 300_000
SCHEMA_NOTE = "ccxt_public_5m_v1"

VENUES: dict[str, dict[str, str]] = {
    "okx": {"quote": "USDT", "template": "{base}/USDT"},
    "bybit": {"quote": "USDT", "template": "{base}/USDT"},
    "coinbaseexchange": {"quote": "USD", "template": "{base}/USD"},
}


@dataclass(frozen=True)
class PageResult:
    rows: list[list[float]]
    error: str | None = None


Fetcher = Callable[[str, int, int], PageResult]


def ensure_ccxt_ohlcv(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ccxt_ohlcv (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            ts INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL,
            UNIQUE (exchange, symbol, timeframe, ts)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ccxt_backfill_status (
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            rows INTEGER NOT NULL,
            start_ms INTEGER,
            end_ms INTEGER,
            PRIMARY KEY (exchange, symbol)
        )
        """
    )


def closed_bar_opens(start_ms: int, end_ms: int, step_ms: int = STEP_MS) -> list[int]:
    """Open times of bars that are fully closed inside ``[start_ms, end_ms]``.

    The bar that contains ``end_ms`` is still forming and is not an expected
    slot, so it is not reported as missing.
    """
    if step_ms <= 0 or end_ms <= start_ms:
        return []
    if start_ms % step_ms == 0:
        first = start_ms
    else:
        first = ((start_ms // step_ms) + 1) * step_ms
    last = ((end_ms - step_ms) // step_ms) * step_ms
    if last < first:
        return []
    count = ((last - first) // step_ms) + 1
    return [first + index * step_ms for index in range(count)]


def missing_closed_opens(
    start_ms: int,
    end_ms: int,
    observed: Sequence[int],
    step_ms: int = STEP_MS,
) -> list[int]:
    have = {int(stamp) for stamp in observed}
    return [stamp for stamp in closed_bar_opens(start_ms, end_ms, step_ms) if stamp not in have]


def closed_pages(
    fetch_page: Fetcher,
    *,
    start_ms: int,
    end_ms: int,
    limit: int,
) -> tuple[list[list[float]], str | None]:
    """Walk ``since`` forward. Stop when the exchange does not advance."""
    cursor = int(start_ms)
    seen: set[int] = set()
    collected: list[list[float]] = []
    while cursor < end_ms:
        page = fetch_page("5m", cursor, limit)
        if page.error:
            return collected, page.error
        if not page.rows:
            break
        advanced = False
        for row in page.rows:
            open_ms = int(row[0])
            if open_ms < start_ms or open_ms in seen:
                continue
            if open_ms + STEP_MS > end_ms:
                continue
            seen.add(open_ms)
            collected.append([float(item) for item in row[:6]])
            advanced = True
        last = int(page.rows[-1][0])
        nxt = last + STEP_MS
        if not advanced or nxt <= cursor:
            break
        cursor = nxt
    collected.sort(key=lambda item: item[0])
    return collected, None


def insert_closed_bars(
    conn: sqlite3.Connection,
    exchange: str,
    symbol: str,
    rows: Sequence[Sequence[float]],
) -> int:
    ensure_ccxt_ohlcv(conn)
    written = 0
    for row in rows:
        open_ms = int(row[0])
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO ccxt_ohlcv (
                exchange, symbol, timeframe, ts, open, high, low, close, volume, quote_volume
            ) VALUES (?, ?, '5m', ?, ?, ?, ?, ?, ?, NULL)
            """,
            (exchange, symbol, open_ms, row[1], row[2], row[3], row[4], row[5]),
        )
        written += int(cur.rowcount or 0)
    conn.commit()
    return written


def record_status(
    conn: sqlite3.Connection,
    exchange: str,
    symbol: str,
    status: str,
    detail: str | None,
    rows: int,
    start_ms: int,
    end_ms: int,
) -> None:
    ensure_ccxt_ohlcv(conn)
    conn.execute(
        """
        INSERT INTO ccxt_backfill_status (
            exchange, symbol, status, detail, rows, start_ms, end_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(exchange, symbol) DO UPDATE SET
            status=excluded.status,
            detail=excluded.detail,
            rows=excluded.rows,
            start_ms=excluded.start_ms,
            end_ms=excluded.end_ms
        """,
        (exchange, symbol, status, detail, rows, start_ms, end_ms),
    )
    conn.commit()


def symbol_for(exchange: str, base: str) -> str:
    return VENUES[exchange]["template"].format(base=base)


def quote_for(exchange: str) -> str:
    return VENUES[exchange]["quote"]


def make_ccxt_fetcher(exchange: Any, symbol: str, limiter: RateLimiter) -> Fetcher:
    def fetch_page(timeframe: str, since: int, limit: int) -> PageResult:
        limiter.wait()
        try:
            rows = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "403" in text or "Forbidden" in text or "CloudFront" in text:
                return PageResult([], f"blocked:{type(exc).__name__}")
            return PageResult([], f"{type(exc).__name__}:{text[:180]}")
        return PageResult(rows or [], None)

    return fetch_page


def resume_open_ms(
    conn: sqlite3.Connection,
    exchange: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> int:
    """First missing closed open. A stored prefix is not requested again."""
    ensure_ccxt_ohlcv(conn)
    stored = [
        int(row[0])
        for row in conn.execute(
            """
            SELECT ts FROM ccxt_ohlcv
            WHERE exchange = ? AND symbol = ? AND timeframe = '5m' AND ts >= ? AND ts < ?
            ORDER BY ts
            """,
            (exchange, symbol, start_ms, end_ms),
        )
    ]
    cursor = int(start_ms)
    for ts in stored:
        if ts != cursor:
            break
        cursor += STEP_MS
    return cursor


def _ccxt_coverage_status(
    *,
    error: str | None,
    expected: int,
    missing: list[int],
    stored: int,
    resume: int,
    end_ms: int,
    fetched: list[list[float]],
) -> tuple[str, str]:
    """Separate a real hole from an unsupported market. Empty is not success."""
    if error:
        blocked = error.startswith("blocked:") or "403" in error or "CloudFront" in error
        if stored:
            return "partial", "blocked" if blocked else "partial"
        return "error", "blocked" if blocked else "error"
    if expected <= 0 or not stored:
        return "missing", "missing"
    if missing:
        return "partial", "partial"
    if resume >= end_ms and not fetched:
        return "skipped_covered", "complete"
    return "ok", "complete"


def backfill_symbol(
    conn: sqlite3.Connection,
    exchange_id: str,
    base: str,
    *,
    start_ms: int,
    end_ms: int,
    fetch_page: Fetcher,
    limit: int = 100,
) -> dict[str, object]:
    symbol = symbol_for(exchange_id, base)
    resume = resume_open_ms(conn, exchange_id, symbol, start_ms, end_ms)
    fetched: list[list[float]] = []
    error = None
    if resume < end_ms:
        fetched, error = closed_pages(fetch_page, start_ms=resume, end_ms=end_ms, limit=limit)
    written = insert_closed_bars(conn, exchange_id, symbol, fetched) if fetched else 0
    stored_rows = [
        int(row[0])
        for row in conn.execute(
            """
            SELECT ts FROM ccxt_ohlcv
            WHERE exchange = ? AND symbol = ? AND timeframe = '5m' AND ts >= ? AND ts < ?
            ORDER BY ts
            """,
            (exchange_id, symbol, start_ms, end_ms),
        )
    ]
    expected_opens = closed_bar_opens(start_ms, end_ms)
    missing = [stamp for stamp in expected_opens if stamp not in set(stored_rows)]
    status, coverage = _ccxt_coverage_status(
        error=error,
        expected=len(expected_opens),
        missing=missing,
        stored=len(stored_rows),
        resume=resume,
        end_ms=end_ms,
        fetched=fetched,
    )
    record_status(conn, exchange_id, symbol, status, error, len(stored_rows), start_ms, end_ms)
    return {
        "exchange": exchange_id,
        "base": base,
        "symbol": symbol,
        "quote": quote_for(exchange_id),
        "rows": len(stored_rows),
        "fetched_rows": len(fetched),
        "resume_open_ms": resume,
        "inserted": written,
        "expected_closed_bars": len(expected_opens),
        "missing_bars": len(missing),
        "missing_timestamps_ms": missing,
        "status": status,
        "coverage": coverage,
        "detail": error,
        "history_covers_requested_start": resume > start_ms or (
            bool(fetched) and int(fetched[0][0]) <= start_ms + STEP_MS
        ),
    }


def core_bases() -> tuple[str, ...]:
    return CORE_BASES


def sleep_limiter(min_interval_s: float) -> RateLimiter:
    return RateLimiter(min_interval_s)


def utc_ms() -> int:
    return int(time.time() * 1000)
