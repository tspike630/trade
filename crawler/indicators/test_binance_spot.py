"""Binance spot klines → tech_boll side tables. DEX token_indicators stay put."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    INDICATORS_WEIGHT_FUNDING,
    INDICATORS_WEIGHT_SENTIMENT,
    INDICATORS_WEIGHT_TECHNICAL,
)
from decision_engine.core.binance_indicators import list_binance_hot_tokens
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from indicators.binance_spot import (
    KLINE_FETCH_CAP,
    SpotKline,
    SpotSymbol,
    canonical_spot_usdt,
    ensure_schema,
    factors_from_klines,
    ingest_spot_klines,
    kline_url,
    load_primary_universe,
    load_stored_klines,
    main,
    parse_kline_row,
    persist_klines_and_factors,
    price_series_from_klines,
    upsert_indicator,
    upsert_klines,
)
from indicators.technical import change_pct, volume_7d_avg, volume_strength
from indicators.technical_lib import compute_pandas_ta
from indicators.base import INTERVAL_SEC


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bars(n: int = 30, *, last_close: float = 10.0, quote: float = 2000.0) -> list:
    """5m bars. All closes are 100 except the last one."""
    start = 1_700_000_000_000
    step = 300_000
    rows = []
    for i in range(n):
        close = last_close if i == n - 1 else 100.0
        open_ms = start + i * step
        rows.append(
            [
                open_ms,
                "100",
                "101",
                "99",
                str(close),
                "10",
                open_ms + 299_999,
                str(quote),
                3,
            ]
        )
    parsed = [parse_kline_row(row) for row in rows]
    assert all(bar is not None for bar in parsed)
    return parsed


def _dump(conn: sqlite3.Connection, table: str) -> list | None:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if found is None:
        return None
    return conn.execute(f"SELECT * FROM {table}").fetchall()


class UniverseTests(unittest.TestCase):
    def test_canonical_spot_usdt_only(self) -> None:
        self.assertEqual(canonical_spot_usdt("btc/usdt"), ("BTCUSDT", "BTC"))
        self.assertEqual(canonical_spot_usdt("SOLUSDT"), ("SOLUSDT", "SOL"))
        self.assertIsNone(canonical_spot_usdt("ETH/USDC"))
        self.assertIsNone(canonical_spot_usdt("BTC/USDT:USDT"))
        self.assertIsNone(canonical_spot_usdt("USDT"))

    def test_sqlite_and_json_are_the_symbol_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "exports" / "binance_spot_universe.db"
            db.parent.mkdir(parents=True)
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE symbols (
                    symbol TEXT, base TEXT, quote TEXT, status TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO symbols VALUES (?, ?, ?, ?)",
                [
                    ("BTCUSDT", "BTC", "USDT", "TRADING"),
                    ("eth/usdt", "ETH", "USDT", "TRADING"),
                    ("SOLUSDT", "SOL", "USDT", "HALT"),
                    ("BTC/USDT:USDT", "BTC", "USDT", "TRADING"),
                    ("ADAUSDC", "ADA", "USDC", "TRADING"),
                ],
            )
            conn.commit()
            conn.close()
            report = root / "exports" / "reports" / "binance_spot_usdt_trading_2026-11-05.json"
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps(
                    {
                        "status": "TRADING",
                        "count": 1,
                        "symbols": [{"symbol": "XRPUSDT", "baseAsset": "XRP", "quoteAsset": "USDT"}],
                    }
                ),
                encoding="utf-8",
            )
            rows, source = load_primary_universe(
                [db, report]
            )
            self.assertEqual(source, db)
            self.assertEqual([item.symbol for item in rows], ["BTCUSDT", "ETHUSDT"])

            # JSON is the fallback when the db is absent.
            json_rows, json_source = load_primary_universe([root / "missing.db", report])
            self.assertEqual(json_source, report)
            self.assertEqual([item.symbol for item in json_rows], ["XRPUSDT"])


class FactorWriteTests(unittest.TestCase):
    def test_weights_stay_zero(self) -> None:
        self.assertEqual(INDICATORS_WEIGHT_TECHNICAL, 0.0)
        self.assertEqual(INDICATORS_WEIGHT_FUNDING, 0.0)
        self.assertEqual(INDICATORS_WEIGHT_SENTIMENT, 0.0)

    def test_kline_factors_match_paper_functions_and_leave_dex_rows(self) -> None:
        bars = _bars()
        series = price_series_from_klines(bars)
        closes = [float(row["price_usd"]) for row in series]
        factors = factors_from_klines(bars)
        assert factors is not None
        self.assertAlmostEqual(
            factors["change_5m"],
            change_pct(series, INTERVAL_SEC["change_5m"]),
            places=6,
        )
        self.assertAlmostEqual(factors["change_5m"], -90.0, places=4)
        self.assertAlmostEqual(factors["volume_strength"], volume_strength(series), places=6)
        self.assertGreaterEqual(factors["volume_strength"], 0.5)
        lib = compute_pandas_ta(closes)
        self.assertEqual(factors["boll_lower"], lib["boll_lower"])
        self.assertEqual(factors["boll_mid"], lib["boll_mid"])
        self.assertEqual(factors["boll_upper"], lib["boll_upper"])
        self.assertLess(factors["price_usd"], factors["boll_lower"])
        self.assertAlmostEqual(factors["quote_volume_24h"], 30 * 2000.0, places=4)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, change_5m REAL, boll_lower REAL
                )
                """
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES ('solana', 'MintPaper', 1.25, 3.5)"
            )
            conn.execute(
                "CREATE TABLE pair_history (chain_id TEXT, price_usd REAL)"
            )
            conn.execute("INSERT INTO pair_history VALUES ('solana', 1.25)")
            conn.commit()
            before_ti = _dump(conn, "token_indicators")
            before_ph = _dump(conn, "pair_history")
            before_cols = conn.execute("PRAGMA table_info(token_indicators)").fetchall()
            stored = persist_klines_and_factors(
                conn,
                SpotSymbol("ZZQBOLLUSDT", "ZZQBOLL"),
                bars,
            )
            self.assertIsNotNone(stored)
            self.assertEqual(_dump(conn, "token_indicators"), before_ti)
            self.assertEqual(_dump(conn, "pair_history"), before_ph)
            self.assertEqual(
                conn.execute("PRAGMA table_info(token_indicators)").fetchall(),
                before_cols,
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM token_indicators WHERE token_address = 'ZZQBOLLUSDT'"
                ).fetchone()
            )
            hot = list_binance_hot_tokens(conn, limit=5, fresh_sec=3600)
            self.assertEqual([row["token_address"] for row in hot], ["ZZQBOLLUSDT"])
            self.assertEqual(hot[0]["chain_id"], "binance")
            self.assertNotEqual(hot[0]["token_address"], "MintPaper")
            signals = TechBollFadeStrategy().on_tick({"hot_tokens": hot})
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["token_address"], "ZZQBOLLUSDT")
            self.assertEqual(signals[0]["strategy_id"], "tech_boll_fade_v1")
            self.assertIn("tech_fade&boll_lower", signals[0]["and_mode"])
            conn.close()

    def test_ingest_iterates_universe_symbols_only(self) -> None:
        bars = _bars(n=25, last_close=10.0, quote=2000.0)
        raw = [
            [
                bar.open_time_ms,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.close_time_ms,
                bar.quote_volume,
                bar.trades,
            ]
            for bar in bars
        ]
        seen: list[str] = []

        def fetcher(url: str):
            seen.append(url)
            if "BTCUSDT" in url:
                return raw
            if "ETHUSDT" in url:
                raise RuntimeError("down")
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE token_indicators (chain_id TEXT, token_address TEXT, change_5m REAL)"
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES ('solana', 'MintPaper', 4.0)"
            )
            conn.commit()
            before = _dump(conn, "token_indicators")
            stats = ingest_spot_klines(
                conn,
                [
                    SpotSymbol("BTCUSDT", "BTC"),
                    SpotSymbol("ETHUSDT", "ETH"),
                ],
                fetcher=fetcher,
                pause_sec=0,
            )
            self.assertEqual(stats["indicators"], 1)
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(len(seen), 2)
            self.assertTrue(all("/api/v3/klines?" in url for url in seen))
            self.assertTrue(all("exchangeInfo" not in url for url in seen))
            self.assertEqual(_dump(conn, "token_indicators"), before)
            symbols = {
                row[0]
                for row in conn.execute("SELECT symbol FROM binance_indicators").fetchall()
            }
            self.assertEqual(symbols, {"BTCUSDT"})
            conn.close()

    def test_top_list_is_cex_side_path(self) -> None:
        now = _iso(datetime.now(timezone.utc))
        stale = _iso(datetime.now(timezone.utc) - timedelta(hours=5))
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, change_5m REAL, tier TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES ('solana', 'MintPaper', -9, 'hot')"
            )
            ensure_schema(conn)
            for symbol, change, quote, updated in (
                ("AAAUSDT", -1.0, 10_000.0, now),
                ("BBBUSDT", -8.0, 80_000.0, now),
                ("CCCUSDT", -20.0, 90_000.0, stale),
            ):
                upsert_indicator(
                    conn,
                    {
                        "symbol": symbol,
                        "base_asset": symbol[:-4],
                        "quote_asset": "USDT",
                        "status": "TRADING",
                        "interval": "5m",
                        "price_usd": 1.0,
                        "change_5m": change,
                        "volume_strength": 1.0,
                        "boll_lower": 2.0,
                        "quote_volume_24h": quote,
                        "tier": "hot",
                        "tech_updated_at": updated,
                        "updated_at": updated,
                    },
                )
            conn.commit()
            by_volume = list_binance_hot_tokens(
                conn, limit=1, sort="quote_volume_24h", fresh_sec=3600
            )
            self.assertEqual([row["token_address"] for row in by_volume], ["BBBUSDT"])
            by_fade = list_binance_hot_tokens(
                conn, limit=5, sort="change_5m", fresh_sec=3600
            )
            self.assertEqual(
                [row["token_address"] for row in by_fade],
                ["BBBUSDT", "AAAUSDT"],
            )
            self.assertNotIn("MintPaper", [row["token_address"] for row in by_fade])
            self.assertNotIn("CCCUSDT", [row["token_address"] for row in by_fade])
            conn.close()

    def test_dry_run_reads_universe_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "universe.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE symbols (symbol TEXT, status TEXT)"
            )
            conn.execute("INSERT INTO symbols VALUES ('BNBUSDT', 'TRADING')")
            conn.commit()
            conn.close()
            old = os.environ.get("BINANCE_SPOT_UNIVERSE_DB")
            os.environ["BINANCE_SPOT_UNIVERSE_DB"] = str(db)
            os.environ["LOG_HANDLER"] = "stdout"
            try:
                code = main(["--dry-run"])
            finally:
                if old is None:
                    os.environ.pop("BINANCE_SPOT_UNIVERSE_DB", None)
                else:
                    os.environ["BINANCE_SPOT_UNIVERSE_DB"] = old
            self.assertEqual(code, 0)


_STEP = 300_000
_START = 1_700_000_000_000
_WEEK_BARS = 7 * 24 * 12 + 1
_EIGHT_DAY_BARS = 8 * 24 * 12 + 1


def _grid(
    n: int,
    *,
    start: int = _START,
    quote: float = 100.0,
    trades: int | None = 10,
    taker_quote: float | None = 40.0,
    taker_base: float | None = 4.0,
    close: float = 100.0,
    closes: list[float] | None = None,
    high: float | None = None,
    low: float | None = None,
) -> list[SpotKline]:
    bars: list[SpotKline] = []
    for index in range(n):
        px = closes[index] if closes is not None else close
        open_ms = start + index * _STEP
        bars.append(
            SpotKline(
                open_time_ms=open_ms,
                open=px,
                high=px + 1.0 if high is None else high,
                low=px - 1.0 if low is None else low,
                close=px,
                volume=2.0,
                quote_volume=quote,
                close_time_ms=open_ms + _STEP - 1,
                trades=trades,
                taker_buy_volume=taker_base,
                taker_buy_quote_volume=taker_quote,
            )
        )
    return bars


def _ohlc(
    raw_open: int,
    raw_high: str,
    raw_low: str,
    raw_close: str,
    raw_quote: str,
    *,
    trades: int | None = 3,
    taker_base: str | None = None,
    taker_quote: str | None = None,
) -> list:
    row = [
        raw_open,
        "100",
        raw_high,
        raw_low,
        raw_close,
        "10",
        raw_open + _STEP - 1,
        raw_quote,
    ]
    if trades is not None or taker_base is not None or taker_quote is not None:
        row.append(trades)
    if taker_base is not None or taker_quote is not None:
        row.append(taker_base)
    if taker_quote is not None:
        row.append(taker_quote)
    return row


class ReadonlyFieldTests(unittest.TestCase):
    def test_short_rows_parse_and_eleven_fields_keep_taker_columns(self) -> None:
        open_ms = _START
        nine = _ohlc(open_ms, "101", "99", "100", "80", trades=4)
        self.assertEqual(len(nine), 9)
        short = parse_kline_row(nine)
        assert short is not None
        self.assertIsNone(short.taker_buy_volume)
        self.assertIsNone(short.taker_buy_quote_volume)
        self.assertEqual(short.trades, 4)
        eight = nine[:8]
        self.assertIsNone(parse_kline_row(eight).trades)
        self.assertIsNone(parse_kline_row(eight).taker_buy_quote_volume)

        eleven = _ohlc(
            open_ms,
            "101",
            "99",
            "100",
            "80",
            trades=4,
            taker_base="1.5",
            taker_quote="30",
        )
        self.assertEqual(len(eleven), 11)
        full = parse_kline_row(eleven)
        assert full is not None
        self.assertEqual(full.taker_buy_volume, 1.5)
        self.assertEqual(full.taker_buy_quote_volume, 30.0)
        with_ignore = [*eleven, "0"]
        self.assertEqual(parse_kline_row(with_ignore), full)
        broken = [*eleven[:9], "nope", ""]
        bad = parse_kline_row(broken)
        assert bad is not None
        self.assertEqual(bad.close, 100.0)
        self.assertIsNone(bad.taker_buy_volume)
        self.assertIsNone(bad.taker_buy_quote_volume)
        # Positional construction from before these fields still works.
        legacy = SpotKline(1, 2, 3, 1, 2, 1, 4, 10, trades=2)
        self.assertIsNone(legacy.taker_buy_quote_volume)

    def test_migration_is_repeatable_and_keeps_rows_and_foreign_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "old.db")
            conn.execute(
                """
                CREATE TABLE binance_klines (
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    quote_volume REAL,
                    close_time_ms INTEGER NOT NULL,
                    trades INTEGER,
                    PRIMARY KEY (symbol, interval, open_time_ms)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE binance_indicators (
                    symbol TEXT PRIMARY KEY,
                    base_asset TEXT,
                    quote_asset TEXT,
                    status TEXT,
                    interval TEXT NOT NULL,
                    price_usd REAL,
                    change_5m REAL,
                    change_15m REAL,
                    change_1h REAL,
                    change_4h REAL,
                    change_24h REAL,
                    volatility_24h REAL,
                    volume_strength REAL,
                    rsi_14 REAL,
                    macd REAL,
                    macd_signal REAL,
                    macd_hist REAL,
                    boll_upper REAL,
                    boll_mid REAL,
                    boll_lower REAL,
                    quote_volume_24h REAL,
                    sample_count INTEGER,
                    kline_close_time_ms INTEGER,
                    tier TEXT,
                    tech_updated_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("ALTER TABLE binance_klines ADD COLUMN kline_note TEXT")
            conn.execute("ALTER TABLE binance_indicators ADD COLUMN crowd_shadow REAL")
            conn.execute(
                """
                INSERT INTO binance_klines (
                    symbol, interval, open_time_ms, open, high, low, close,
                    volume, quote_volume, close_time_ms, trades, kline_note
                ) VALUES ('BTCUSDT', '5m', ?, 10, 11, 9, 10, 1, 50, ?, 7, 'keep')
                """,
                (_START, _START + _STEP - 1),
            )
            conn.execute(
                """
                INSERT INTO binance_indicators (
                    symbol, base_asset, quote_asset, status, interval,
                    price_usd, change_5m, volume_strength, boll_lower,
                    quote_volume_24h, tier, tech_updated_at, updated_at, crowd_shadow
                ) VALUES (
                    'BTCUSDT', 'BTC', 'USDT', 'TRADING', '5m',
                    10, -3, 1.2, 9, 50, 'hot', 't', 't', 1.25
                )
                """
            )
            conn.execute(
                "CREATE TABLE token_indicators (token_address TEXT, change_5m REAL)"
            )
            conn.execute("INSERT INTO token_indicators VALUES ('MintPaper', 1.0)")
            conn.commit()
            ensure_schema(conn)
            ensure_schema(conn)
            kline_cols = [row[1] for row in conn.execute("PRAGMA table_info(binance_klines)")]
            ind_cols = [row[1] for row in conn.execute("PRAGMA table_info(binance_indicators)")]
            self.assertEqual(kline_cols.count("taker_buy_quote_volume"), 1)
            self.assertEqual(ind_cols.count("trade_size_ratio"), 1)
            self.assertEqual(ind_cols.count("lower_wick_ratio"), 1)
            self.assertIn("kline_note", kline_cols)
            self.assertIn("crowd_shadow", ind_cols)
            kept = conn.execute(
                "SELECT close, trades, kline_note, taker_buy_volume, taker_buy_quote_volume "
                "FROM binance_klines"
            ).fetchone()
            self.assertEqual(kept, (10.0, 7, "keep", None, None))
            kept_ind = conn.execute(
                "SELECT change_5m, volume_strength, crowd_shadow, change_7d, buy_ratio_5m "
                "FROM binance_indicators"
            ).fetchone()
            self.assertEqual(kept_ind, (-3.0, 1.2, 1.25, None, None))
            self.assertEqual(
                conn.execute("SELECT * FROM token_indicators").fetchall(),
                [("MintPaper", 1.0)],
            )
            upsert_klines(
                conn,
                "BTCUSDT",
                "5m",
                [
                    SpotKline(
                        _START, 10, 11, 9, 10.5, 1, 50, _START + _STEP - 1,
                        trades=7, taker_buy_volume=0.2, taker_buy_quote_volume=12,
                    )
                ],
            )
            upsert_indicator(
                conn,
                {
                    "symbol": "BTCUSDT",
                    "base_asset": "BTC",
                    "quote_asset": "USDT",
                    "status": "TRADING",
                    "interval": "5m",
                    "price_usd": 10.5,
                    "change_5m": -3.0,
                    "volume_strength": 1.2,
                    "quote_volume_24h": 50,
                    "tier": "hot",
                    "tech_updated_at": "t",
                    "updated_at": "t2",
                    "lower_wick_ratio": 0.25,
                },
            )
            conn.commit()
            after = conn.execute(
                "SELECT close, kline_note, taker_buy_quote_volume FROM binance_klines"
            ).fetchone()
            self.assertEqual(after, (10.5, "keep", 12.0))
            after_ind = conn.execute(
                "SELECT updated_at, crowd_shadow, lower_wick_ratio, change_5m "
                "FROM binance_indicators"
            ).fetchone()
            self.assertEqual(after_ind, ("t2", 1.25, 0.25, -3.0))
            # A reader that only knows the pre-change columns still gets the row.
            old_reader = conn.execute(
                "SELECT symbol, price_usd, change_5m, volume_strength FROM binance_indicators"
            ).fetchone()
            self.assertEqual(old_reader, ("BTCUSDT", 10.5, -3.0, 1.2))
            conn.close()

    def test_persist_roundtrip_and_latest_closed_bar_only(self) -> None:
        closed = _grid(20, quote=80.0, trades=4, taker_quote=20.0, taker_base=1.0)
        last = closed[-1]
        forming = replace(
            last,
            open_time_ms=last.open_time_ms + _STEP,
            close_time_ms=last.close_time_ms + _STEP,
            close=1.0,
            open=1.0,
            high=2.0,
            low=0.5,
            quote_volume=9_000.0,
            taker_buy_quote_volume=8_000.0,
        )
        now = last.close_time_ms
        factors = factors_from_klines([*closed, forming], now_ms=now)
        assert factors is not None
        self.assertAlmostEqual(factors["price_usd"], last.close)
        self.assertEqual(factors["kline_close_time_ms"], last.close_time_ms)
        self.assertEqual(factors["taker_buy_quote_volume_5m"], 20.0)
        self.assertAlmostEqual(factors["buy_ratio_5m"], 20.0 / 80.0)
        self.assertAlmostEqual(factors["quote_volume_24h"], 20 * 80.0)
        self.assertNotEqual(factors["price_usd"], 1.0)

        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "ind.db")
            stored = persist_klines_and_factors(
                conn,
                SpotSymbol("ADAUSDT", "ADA"),
                [*closed, forming],
                updated_at="2026-11-07T00:00:00Z",
                now_ms=now,
            )
            assert stored is not None
            loaded = load_stored_klines(conn, "ADAUSDT", "5m")
            self.assertEqual(len(loaded), 21)
            self.assertEqual(loaded[-1].taker_buy_quote_volume, 8_000.0)
            row = conn.execute(
                """
                SELECT taker_buy_quote_volume_5m, buy_ratio_5m, price_usd
                FROM binance_indicators WHERE symbol = 'ADAUSDT'
                """
            ).fetchone()
            self.assertEqual(row[0], 20.0)
            self.assertAlmostEqual(row[1], 0.25)
            self.assertAlmostEqual(row[2], last.close)
            conn.close()

    def test_missing_taker_zero_volume_flat_and_true_wick_not_substituted(self) -> None:
        missing = _grid(1, taker_quote=None, taker_base=None, trades=None)
        factors = factors_from_klines(missing, now_ms=missing[-1].close_time_ms)
        assert factors is not None
        self.assertIsNone(factors["taker_buy_quote_volume_5m"])
        self.assertIsNone(factors["buy_ratio_5m"])
        self.assertIsNone(factors["avg_trade_usd_5m"])
        self.assertIsNone(factors["trade_size_ratio"])

        zero_quote = _grid(1, quote=0.0, trades=5, taker_quote=0.0, taker_base=0.0)
        zero = factors_from_klines(zero_quote, now_ms=zero_quote[-1].close_time_ms)
        assert zero is not None
        self.assertIsNone(zero["buy_ratio_5m"])
        self.assertEqual(zero["avg_trade_usd_5m"], 0.0)
        self.assertEqual(zero["taker_buy_quote_volume_5m"], 0.0)

        flat = _grid(1, close=5.0, high=5.0, low=5.0, quote=10.0, trades=2, taker_quote=4.0)
        flat_factors = factors_from_klines(flat, now_ms=flat[-1].close_time_ms)
        assert flat_factors is not None
        self.assertEqual(flat_factors["lower_wick_ratio"], 0.0)

        # Close sits high in the range. The lower wick from the open is shorter.
        located = SpotKline(
            _START, 95.0, 110.0, 90.0, 108.0, 1.0, 10.0, _START + _STEP - 1, trades=2,
            taker_buy_volume=1.0, taker_buy_quote_volume=4.0,
        )
        located_factors = factors_from_klines([located], now_ms=located.close_time_ms)
        assert located_factors is not None
        self.assertAlmostEqual(located_factors["lower_wick_ratio"], (108.0 - 90.0) / 20.0)
        true_wick = (min(95.0, 108.0) - 90.0) / 20.0
        self.assertNotAlmostEqual(located_factors["lower_wick_ratio"], true_wick)

        inverted = replace(located, high=90.0, low=110.0)
        inverted_factors = factors_from_klines([inverted], now_ms=inverted.close_time_ms)
        assert inverted_factors is not None
        self.assertIsNone(inverted_factors["lower_wick_ratio"])

    def test_buy_ratio_1h_needs_twelve_continuous_bars_and_weights_by_quote(self) -> None:
        bars = _grid(13, quote=10.0, taker_quote=1.0, taker_base=1.0)
        bars[0] = replace(bars[0], taker_buy_quote_volume=999.0, quote_volume=10.0)
        bars[-1] = replace(bars[-1], taker_buy_quote_volume=9.0, quote_volume=30.0)
        now = bars[-1].close_time_ms
        factors = factors_from_klines(bars, now_ms=now)
        assert factors is not None
        # Oldest bar is 60m before the latest open, so it is outside the 12.
        taker_sum = 11 * 1.0 + 9.0
        quote_sum = 11 * 10.0 + 30.0
        self.assertAlmostEqual(factors["buy_ratio_1h"], taker_sum / quote_sum)
        self.assertNotAlmostEqual(
            factors["buy_ratio_1h"],
            (taker_sum + 999.0) / (quote_sum + 10.0),
        )
        self.assertAlmostEqual(factors["buy_ratio_5m"], 9.0 / 30.0)

        short = factors_from_klines(bars[-11:], now_ms=bars[-1].close_time_ms)
        assert short is not None
        self.assertIsNone(short["buy_ratio_1h"])

        gapped = list(bars[-12:])
        gapped[4] = replace(gapped[4], open_time_ms=gapped[4].open_time_ms + _STEP // 2)
        self.assertIsNone(
            factors_from_klines(gapped, now_ms=gapped[-1].close_time_ms)["buy_ratio_1h"]
        )

        missing_base = list(bars[-12:])
        missing_base[3] = replace(missing_base[3], taker_buy_volume=None)
        missing_hour = factors_from_klines(
            missing_base, now_ms=missing_base[-1].close_time_ms
        )
        assert missing_hour is not None
        self.assertIsNone(missing_hour["buy_ratio_1h"])
        self.assertAlmostEqual(missing_hour["buy_ratio_5m"], 9.0 / 30.0)
        missing_quote_taker = list(bars[-12:])
        missing_quote_taker[3] = replace(missing_quote_taker[3], taker_buy_quote_volume=None)
        self.assertIsNone(
            factors_from_klines(
                missing_quote_taker, now_ms=missing_quote_taker[-1].close_time_ms
            )["buy_ratio_1h"]
        )
        zeros = _grid(12, quote=0.0, taker_quote=0.0, taker_base=0.0)
        self.assertIsNone(
            factors_from_klines(zeros, now_ms=zeros[-1].close_time_ms)["buy_ratio_1h"]
        )

    def test_trade_size_ratio_is_sum_over_sum_on_preceding_288(self) -> None:
        bars = _grid(290, quote=100.0, trades=10)
        for index, bar in enumerate(bars):
            if index == 0:
                bars[index] = replace(bar, quote_volume=1_000_000_000.0, trades=1)
            elif index < len(bars) - 1 and index % 2 == 0:
                bars[index] = replace(bar, quote_volume=100.0, trades=100)
        baseline = bars[1:-1]
        self.assertEqual(len(baseline), 288)
        self.assertEqual(
            bars[-1].open_time_ms - baseline[0].open_time_ms,
            288 * _STEP,
        )
        quote_sum = sum(bar.quote_volume for bar in baseline)
        trade_sum = sum(bar.trades or 0 for bar in baseline)
        current_avg = bars[-1].quote_volume / (bars[-1].trades or 1)
        expected = current_avg / (quote_sum / trade_sum)
        per_bar = [bar.quote_volume / (bar.trades or 1) for bar in baseline]
        mean_ratio = current_avg / (sum(per_bar) / len(per_bar))
        self.assertNotAlmostEqual(expected, mean_ratio)
        factors = factors_from_klines(bars, now_ms=bars[-1].close_time_ms)
        assert factors is not None
        self.assertAlmostEqual(factors["trade_size_ratio"], expected)
        self.assertNotAlmostEqual(factors["trade_size_ratio"], mean_ratio)
        wrong = [bars[0], *baseline]
        wrong_quote = sum(bar.quote_volume for bar in wrong)
        wrong_trades = sum(bar.trades or 0 for bar in wrong)
        self.assertNotAlmostEqual(
            factors["trade_size_ratio"],
            current_avg / (wrong_quote / wrong_trades),
        )

        self.assertIsNone(
            factors_from_klines(bars[-288:], now_ms=bars[-1].close_time_ms)["trade_size_ratio"]
        )
        holed = list(bars[-289:])
        del holed[10]
        self.assertIsNone(
            factors_from_klines(holed, now_ms=holed[-1].close_time_ms)["trade_size_ratio"]
        )
        missing_trades = list(bars[-289:])
        missing_trades[4] = replace(missing_trades[4], trades=None)
        self.assertIsNone(
            factors_from_klines(missing_trades, now_ms=missing_trades[-1].close_time_ms)[
                "trade_size_ratio"
            ]
        )
        zero_trades = list(bars[-289:])
        zero_trades[4] = replace(zero_trades[4], trades=0)
        self.assertIsNone(
            factors_from_klines(zero_trades, now_ms=zero_trades[-1].close_time_ms)[
                "trade_size_ratio"
            ]
        )

    def test_seven_day_requires_continuous_coverage_and_audits_the_helper(self) -> None:
        week = _grid(_WEEK_BARS, quote=100.0)
        week[0] = replace(week[0], open=80.0, high=81.0, low=79.0, close=80.0)
        now = week[-1].close_time_ms
        factors = factors_from_klines(week, now_ms=now)
        assert factors is not None
        series = price_series_from_klines(week)
        self.assertAlmostEqual(factors["change_7d"], 25.0)
        self.assertAlmostEqual(factors["change_7d"], change_pct(series, INTERVAL_SEC["change_7d"]))
        self.assertIsNone(factors["volume_7d_avg"])
        self.assertIsNone(volume_7d_avg(series))
        self.assertIsNone(series[0]["volume_h24"])
        self.assertIsNotNone(series[288]["volume_h24"])

        # One bar short of 7d is inside change_pct's 6h skew. Do not store that.
        short = week[1:]
        short_series = price_series_from_klines(short)
        self.assertIsNotNone(change_pct(short_series, INTERVAL_SEC["change_7d"]))
        short_factors = factors_from_klines(short, now_ms=short[-1].close_time_ms)
        assert short_factors is not None
        self.assertIsNone(short_factors["change_7d"])
        self.assertIsNone(short_factors["volume_7d_avg"])

        # A hole inside the last 7d. The anchor price is still there, so the
        # helper would answer; the stored columns stay NULL.
        spanned = _grid(_EIGHT_DAY_BARS, quote=100.0)
        spanned[288] = replace(spanned[288], open=80.0, high=81.0, low=79.0, close=80.0)
        gapped = [bar for index, bar in enumerate(spanned) if index != 500]
        gapped_series = price_series_from_klines(gapped)
        self.assertIsNotNone(change_pct(gapped_series, INTERVAL_SEC["change_7d"]))
        gapped_factors = factors_from_klines(gapped, now_ms=gapped[-1].close_time_ms)
        assert gapped_factors is not None
        self.assertIsNone(gapped_factors["change_7d"])
        self.assertIsNone(gapped_factors["volume_7d_avg"])

        full = _grid(_EIGHT_DAY_BARS, quote=100.0)
        full[288] = replace(full[288], open=50.0, high=51.0, low=49.0, close=50.0)
        full_factors = factors_from_klines(full, now_ms=full[-1].close_time_ms)
        full_series = price_series_from_klines(full)
        assert full_factors is not None
        self.assertAlmostEqual(full_factors["change_7d"], 100.0)
        self.assertIsNotNone(full_factors["volume_7d_avg"])
        self.assertAlmostEqual(full_factors["volume_7d_avg"], volume_7d_avg(full_series))
        self.assertAlmostEqual(full_factors["volume_7d_avg"], 289 * 100.0)
        self.assertNotAlmostEqual(full_factors["volume_7d_avg"], (289 * 100.0) / 7.0)

    def test_stored_history_outruns_one_fetch_without_a_backfill(self) -> None:
        self.assertEqual(KLINE_FETCH_CAP, 1000)
        self.assertNotIn("startTime", kline_url("BTCUSDT", limit=KLINE_FETCH_CAP))
        week = _grid(_WEEK_BARS, quote=100.0)
        week[0] = replace(week[0], open=80.0, high=81.0, low=79.0, close=80.0)
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "ind.db")
            first = persist_klines_and_factors(
                conn, SpotSymbol("SOLUSDT", "SOL"), week, updated_at="t1"
            )
            assert first is not None
            self.assertAlmostEqual(first["change_7d"], 25.0)
            tail = list(week[-30:])
            tail[-1] = replace(tail[-1], open=120.0, high=121.0, low=119.0, close=120.0)
            second = persist_klines_and_factors(
                conn, SpotSymbol("SOLUSDT", "SOL"), tail, updated_at="t2"
            )
            assert second is not None
            self.assertAlmostEqual(second["change_7d"], 50.0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM binance_klines WHERE symbol = 'SOLUSDT'"
                ).fetchone()[0],
                _WEEK_BARS,
            )
            conn.close()

        calls: list[str] = []

        def fetcher(url: str):
            calls.append(url)
            raw = []
            for bar in week[:3]:
                raw.append(
                    [
                        bar.open_time_ms,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.close_time_ms,
                        bar.quote_volume,
                        bar.trades,
                        bar.taker_buy_volume,
                        bar.taker_buy_quote_volume,
                    ]
                )
            return raw

        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "ind.db")
            ingest_spot_klines(
                conn,
                [SpotSymbol("SOLUSDT", "SOL")],
                fetcher=fetcher,
                kline_limit=5000,
                pause_sec=0,
            )
            self.assertEqual(len(calls), 1)
            self.assertIn("limit=1000", calls[0])
            self.assertNotIn("startTime", calls[0])
            conn.close()


if __name__ == "__main__":
    unittest.main()
