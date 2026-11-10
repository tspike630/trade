"""Public CCXT 5m backfill into a stage database.

No API key is read or sent. Coinbase USD bars are labeled and are not
written as USDT. An open candle is not stored. Upsert into an existing
``ccxt.db`` happens only when the caller passes the target path.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from research.hist_calibration.canon import venue_markets

FIVE_MIN_MS = 300_000
TARGET_LOOKBACK_MS = 30 * 24 * 60 * 60 * 1000

_STAGE_DDL = """
CREATE TABLE IF NOT EXISTS hist_ccxt_ohlcv (
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quote TEXT NOT NULL,
    comparable_to_usdt INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    source TEXT NOT NULL,
    PRIMARY KEY (exchange, symbol, timeframe, ts)
);
CREATE TABLE IF NOT EXISTS ccxt_upsert_manifest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    action TEXT NOT NULL
);
"""

_TARGET_DDL = """
CREATE TABLE IF NOT EXISTS ccxt_ohlcv (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL,
    UNIQUE (exchange, symbol, timeframe, ts)
);
"""


def candle_is_closed(open_ms: int, now_ms: int, span_ms: int = FIVE_MIN_MS) -> bool:
    return int(open_ms) + int(span_ms) <= int(now_ms)


def ensure_stage(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_STAGE_DDL)
    return conn


def stage_candles(
    conn: sqlite3.Connection,
    *,
    exchange: str,
    symbol: str,
    quote: str,
    comparable_to_usdt: bool,
    candles: list[list[float]],
    now_ms: int,
) -> dict[str, int]:
    """Insert closed 5m bars. Repeating the same bar is a no-op."""
    written = skipped_open = 0
    for candle in candles:
        if len(candle) < 6:
            continue
        open_ms = int(candle[0])
        if not candle_is_closed(open_ms, now_ms):
            skipped_open += 1
            continue
        conn.execute(
            """
            INSERT INTO hist_ccxt_ohlcv (
                exchange, symbol, quote, comparable_to_usdt, timeframe, ts,
                open, high, low, close, volume, source
            ) VALUES (?, ?, ?, ?, '5m', ?, ?, ?, ?, ?, ?, 'ccxt_public')
            ON CONFLICT(exchange, symbol, timeframe, ts) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume
            """,
            (
                exchange,
                symbol,
                quote,
                1 if comparable_to_usdt else 0,
                open_ms,
                candle[1],
                candle[2],
                candle[3],
                candle[4],
                candle[5],
            ),
        )
        written += 1
    conn.commit()
    return {"written": written, "skipped_open": skipped_open}


def coverage(conn: sqlite3.Connection, *, exchange: str, symbol: str, now_ms: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT ts FROM hist_ccxt_ohlcv
        WHERE exchange=? AND symbol=? AND timeframe='5m' ORDER BY ts
        """,
        (exchange, symbol),
    ).fetchall()
    times = [int(row[0]) for row in rows]
    start = now_ms - TARGET_LOOKBACK_MS
    in_window = [ts for ts in times if start <= ts < now_ms and candle_is_closed(ts, now_ms)]
    expected = TARGET_LOOKBACK_MS // FIVE_MIN_MS
    gaps = 0
    for prev, nxt in zip(in_window, in_window[1:]):
        if nxt - prev > FIVE_MIN_MS:
            gaps += (nxt - prev) // FIVE_MIN_MS - 1
    return {
        "exchange": exchange,
        "symbol": symbol,
        "bars": len(in_window),
        "expected_closed_5m_in_30d": expected,
        "missing_steps": gaps,
        "oldest_ms": in_window[0] if in_window else None,
        "newest_ms": in_window[-1] if in_window else None,
        "complete": len(in_window) == expected and gaps == 0,
    }


def upsert_into_ccxt_db(stage: Path, target: Path) -> dict[str, Any]:
    """Copy staged closed bars into ``ccxt_ohlcv``. Records keys for rollback.

    Does not create ticks or listings. Does not touch any other table.
    """
    src = sqlite3.connect(f"file:{stage.resolve().as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    dst.executescript(_TARGET_DDL)
    dst.executescript(
        """
        CREATE TABLE IF NOT EXISTS ccxt_upsert_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            ts INTEGER NOT NULL,
            action TEXT NOT NULL
        );
        """
    )
    inserted = updated = 0
    try:
        rows = src.execute(
            """
            SELECT exchange, symbol, timeframe, ts, open, high, low, close, volume
            FROM hist_ccxt_ohlcv
            """
        )
        for exchange, symbol, timeframe, ts, open_, high, low, close, volume in rows:
            if not candle_is_closed(int(ts), int(time.time() * 1000)):
                continue
            existing = dst.execute(
                """
                SELECT open, high, low, close, volume FROM ccxt_ohlcv
                WHERE exchange=? AND symbol=? AND timeframe=? AND ts=?
                """,
                (exchange, symbol, timeframe, ts),
            ).fetchone()
            dst.execute(
                """
                INSERT INTO ccxt_ohlcv (
                    exchange, symbol, timeframe, ts, open, high, low, close, volume, quote_volume
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(exchange, symbol, timeframe, ts) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume
                """,
                (exchange, symbol, timeframe, ts, open_, high, low, close, volume),
            )
            action = "insert" if existing is None else "update"
            dst.execute(
                """
                INSERT INTO ccxt_upsert_manifest (exchange, symbol, timeframe, ts, action)
                VALUES (?, ?, ?, ?, ?)
                """,
                (exchange, symbol, timeframe, ts, action),
            )
            if existing is None:
                inserted += 1
            else:
                updated += 1
        dst.commit()
    finally:
        src.close()
        dst.close()
    manifest = target.with_suffix(target.suffix + ".upsert-manifest.json")
    payload = {"target": str(target), "stage": str(stage), "inserted": inserted, "updated": updated}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    payload["manifest"] = str(manifest)
    return payload


def rollback_upsert(target: Path, manifest: Path) -> dict[str, int]:
    """Delete only rows this upsert inserted. Updates are not reversed."""
    body = json.loads(manifest.read_text(encoding="utf-8"))
    del body
    conn = sqlite3.connect(target)
    removed = 0
    try:
        keys = conn.execute(
            "SELECT exchange, symbol, timeframe, ts FROM ccxt_upsert_manifest WHERE action='insert'"
        ).fetchall()
        for exchange, symbol, timeframe, ts in keys:
            cur = conn.execute(
                """
                DELETE FROM ccxt_ohlcv
                WHERE exchange=? AND symbol=? AND timeframe=? AND ts=?
                """,
                (exchange, symbol, timeframe, ts),
            )
            removed += cur.rowcount or 0
        conn.execute("DELETE FROM ccxt_upsert_manifest WHERE action='insert'")
        conn.commit()
    finally:
        conn.close()
    return {"removed_inserts": removed}


def probe_exchange(
    exchange_id: str,
    symbol: str,
    *,
    now_ms: int,
    factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """One public request. Records whether a bar near 30 days ago came back.

    Does not page the full month and does not write a database.
    """
    report: dict[str, Any] = {
        "exchange": exchange_id,
        "symbol": symbol,
        "since_ms": now_ms - TARGET_LOOKBACK_MS,
        "complete_30d": False,
    }
    try:
        if factory is None:
            import ccxt  # type: ignore
            klass = getattr(ccxt, exchange_id)
            client = klass({"enableRateLimit": True})
        else:
            client = factory(exchange_id)
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}:{exc}"
        report["available"] = False
        return report
    try:
        candles = client.fetch_ohlcv(symbol, timeframe="5m", since=report["since_ms"], limit=2)
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}:{exc}"
        report["available"] = False
        return report
    report["available"] = True
    report["returned"] = len(candles or [])
    if candles:
        oldest = int(candles[0][0])
        report["oldest_returned_ms"] = oldest
        report["oldest_age_days"] = (now_ms - oldest) / 86_400_000
        report["reached_30d_request"] = oldest <= report["since_ms"] + FIVE_MIN_MS
    else:
        report["reached_30d_request"] = False
        report["note"] = "empty_page_is_not_a_completed_30d_history"
    limit = None
    try:
        limit = (client.limits or {}).get("ohlcv") or client.options
    except Exception:  # noqa: BLE001
        limit = None
    report["client_limit_hint"] = str(limit)[:240] if limit is not None else None
    return report


def planned_requests(symbols: list[str]) -> list[dict[str, Any]]:
    """Public requests a full 30-day pull would make. Not executed here."""
    plans = []
    for binance_symbol in symbols:
        markets = venue_markets(binance_symbol)
        for venue in ("okx", "bybit", "coinbase"):
            item = markets[venue]
            plans.append(
                {
                    "exchange": venue,
                    "symbol": item["symbol"],
                    "quote": item["quote"],
                    "comparable_to_usdt": item.get("comparable_to_usdt", item["quote"] == "USDT"),
                    "timeframe": "5m",
                    "lookback_ms": TARGET_LOOKBACK_MS,
                }
            )
    return plans
