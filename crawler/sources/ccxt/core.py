"""CCXT fetch + idempotent write into ccxt.db (ticks / ohlcv / listings)."""

from __future__ import annotations

import sqlite3
import time

import ccxt

from logging_setup import get_logger

log = get_logger("sources.ccxt.core")


def _timeframe_ms(timeframe: str) -> int | None:
    tf = (timeframe or "").strip().lower()
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    if len(tf) < 2 or tf[-1] not in units:
        return None
    try:
        count = int(tf[:-1])
    except ValueError:
        return None
    if count <= 0:
        return None
    return count * units[tf[-1]]


def candle_is_closed(ts_ms: int, timeframe: str, now_ms: int) -> bool:
    """A bar is closed once its open time plus the timeframe is at or before now."""
    span = _timeframe_ms(timeframe)
    if span is None:
        return True
    return int(ts_ms) + span <= int(now_ms)


def write_ohlcv(
    conn: sqlite3.Connection,
    exchange: str,
    symbol: str,
    timeframe: str,
    candles: list,
    now_ms: int,
) -> int:
    """Write closed bars only. Unclosed bars are dropped so INSERT cannot freeze them.

    Commits this symbol before the caller fetches the next one.
    """
    written = 0
    for candle in candles:
        ts = int(candle[0])
        if not candle_is_closed(ts, timeframe, now_ms):
            continue
        cur = conn.execute(
            """
            INSERT INTO ccxt_ohlcv
            (exchange, symbol, timeframe, ts, open, high, low, close, volume, quote_volume)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(exchange, symbol, timeframe, ts) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                quote_volume = excluded.quote_volume
            """,
            (
                exchange,
                symbol,
                timeframe,
                ts,
                candle[1],
                candle[2],
                candle[3],
                candle[4],
                candle[5],
                None,
            ),
        )
        if cur.rowcount and cur.rowcount > 0:
            written += 1
    conn.commit()
    return written


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ccxt_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,            -- epoch ms, 快照时间
    last_price REAL,
    bid REAL,
    ask REAL,
    high_24h REAL,
    low_24h REAL,
    volume_24h REAL,                -- base 成交量
    quote_volume_24h REAL,
    change_24h_pct REAL,
    UNIQUE (exchange, symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_ccxt_ticks_ex_sym ON ccxt_ticks(exchange, symbol, ts);
CREATE TABLE IF NOT EXISTS ccxt_ohlcv (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,            -- epoch ms, K线开盘时间
    open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL,
    UNIQUE (exchange, symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_ccxt_ohlcv_ex_sym_tf ON ccxt_ohlcv(exchange, symbol, timeframe, ts);
CREATE TABLE IF NOT EXISTS ccxt_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    first_seen_ts INTEGER NOT NULL,
    first_price REAL,
    note TEXT,
    UNIQUE (exchange, symbol)
);
"""


class CcxtFetcher:
    """Read-only public market data; no API keys required."""

    def __init__(self, exchanges, db_path, top_limit=50,
                 min_quote_volume=100000.0, ohlcv_limit=60):
        self.db_path = db_path
        self.top_limit = top_limit
        self.min_quote_volume = min_quote_volume
        self.ohlcv_limit = ohlcv_limit
        self.exchanges = []
        for name in exchanges:
            name = (name or "").strip().lower()
            if not name:
                continue
            klass = getattr(ccxt, name, None)
            if klass is None:
                log.warning("ccxt exchange %r not supported; skip", name)
                continue
            try:
                self.exchanges.append(klass({"enableRateLimit": True}))
            except Exception as exc:  # noqa: BLE001
                log.warning("init exchange %s failed: %s", name, exc)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _mark_listings(self, conn, exchange, symbols, now) -> int:
        seen = {
            r[0]
            for r in conn.execute(
                "SELECT symbol FROM ccxt_listings WHERE exchange=?", (exchange,)
            )
        }
        new = [s for s in symbols if s not in seen]
        added = 0
        for sym in new:
            conn.execute(
                "INSERT OR IGNORE INTO ccxt_listings"
                "(exchange, symbol, first_seen_ts, first_price, note)"
                " VALUES (?,?,?,?,?)",
                (exchange, sym, now, None, "first seen by ccxt side-channel"),
            )
            added += 1
        conn.commit()
        return added

    def fetch_and_store(self, timeframe: str) -> dict:
        """One pass over all exchanges. Returns per-exchange summary dict."""
        summary: dict = {}
        conn = self._conn()
        try:
            for ex in self.exchanges:
                exname = ex.id
                try:
                    tickers = ex.fetch_tickers()
                except Exception as exc:  # noqa: BLE001
                    log.warning("fetch_tickers %s failed: %s", exname, exc)
                    continue

                rows = []
                for sym, t in tickers.items():
                    qv = t.get("quoteVolume") or 0.0
                    if qv < self.min_quote_volume:
                        continue
                    rows.append(
                        (
                            exname,
                            sym,
                            t.get("last"),
                            t.get("bid"),
                            t.get("ask"),
                            t.get("high"),
                            t.get("low"),
                            t.get("baseVolume"),
                            qv,
                            t.get("percentage"),
                        )
                    )
                rows.sort(key=lambda r: r[8] or 0.0, reverse=True)
                rows = rows[: self.top_limit]

                now = int(time.time() * 1000)
                stored_ticks = 0
                for r in rows:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO ccxt_ticks"
                        "(exchange, symbol, ts, last_price, bid, ask,"
                        " high_24h, low_24h, volume_24h, quote_volume_24h,"
                        " change_24h_pct)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (r[0], r[1], now) + tuple(r[2:]),
                    )
                    stored_ticks += cur.rowcount
                conn.commit()

                new_listings = self._mark_listings(
                    conn, exname, [r[1] for r in rows], now
                )

                ohlcv_rows = 0
                for sym in [r[1] for r in rows]:
                    try:
                        candles = ex.fetch_ohlcv(
                            sym, timeframe=timeframe, limit=self.ohlcv_limit
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    # Commit per symbol so the write txn does not span the next HTTP call.
                    ohlcv_rows += write_ohlcv(
                        conn, exname, sym, timeframe, candles, int(time.time() * 1000)
                    )

                summary[exname] = {
                    "symbols": len(rows),
                    "ticks": stored_ticks,
                    "ohlcv": ohlcv_rows,
                    "new_listings": new_listings,
                }
        finally:
            conn.close()
        return summary
