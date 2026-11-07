"""Binance spot klines → tech_boll side tables. DEX token_indicators stay put."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import unittest
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
    CHANGE_5M_Z_PRIOR_RETURNS,
    TRADES_SPIKE_PRIOR_BARS,
    SpotSymbol,
    canonical_spot_usdt,
    change_5m_z_from_klines,
    crowding_shadow_from_klines,
    ensure_schema,
    factors_from_klines,
    ingest_spot_klines,
    load_primary_universe,
    main,
    parse_kline_row,
    persist_klines_and_factors,
    price_series_from_klines,
    upsert_indicator,
)
from indicators.technical import change_pct, volume_strength
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
        # Flat 30×2000 USDT fixture. Locks the quote-volume ratio itself.
        self.assertAlmostEqual(factors["volume_strength"], 1.2244897959183674, places=6)
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


def _rest_bar(
    i: int,
    *,
    quote: float = 2000.0,
    trades: int | None | str = 10,
    taker_base: str = "1",
    taker_quote: str | None = "500",
    close: float = 100.0,
) -> list:
    """One /api/v3/klines array. Index 10 is taker buy quote, not index 9."""
    open_ms = 1_700_000_000_000 + i * 300_000
    row: list = [
        open_ms,
        "100",
        "101",
        "99",
        str(close),
        "10",
        open_ms + 299_999,
        str(quote),
        "" if trades is None else trades,
        taker_base,
    ]
    if taker_quote is not None:
        row.append(taker_quote)
    return row


class CrowdingShadowTests(unittest.TestCase):
    def test_parse_reads_taker_buy_quote_volume_at_index_10(self) -> None:
        raw = _rest_bar(0, taker_base="999", taker_quote="40.5", trades=4)
        bar = parse_kline_row(raw)
        assert bar is not None
        self.assertEqual(bar.trades, 4)
        self.assertAlmostEqual(bar.quote_volume, 2000.0)
        self.assertAlmostEqual(bar.taker_buy_quote_volume or 0.0, 40.5)
        self.assertNotEqual(bar.taker_buy_quote_volume, 999.0)

        short = raw[:9]
        short_bar = parse_kline_row(short)
        assert short_bar is not None
        self.assertIsNone(short_bar.taker_buy_quote_volume)
        self.assertEqual(short_bar.trades, 4)

        bad = _rest_bar(1, taker_quote="nope", trades=4)
        bad_bar = parse_kline_row(bad)
        assert bad_bar is not None
        self.assertIsNone(bad_bar.taker_buy_quote_volume)
        self.assertEqual(bad_bar.trades, 4)

    def test_avg_size_ratio_and_spike_use_the_latest_5m_bar(self) -> None:
        self.assertEqual(TRADES_SPIKE_PRIOR_BARS, 12)
        rows = [
            _rest_bar(i, quote=2000.0, trades=10, taker_quote="100")
            for i in range(12)
        ]
        rows.append(
            _rest_bar(
                12,
                quote=4000.0,
                trades=40,
                taker_base="888",
                taker_quote="1000",
                close=10.0,
            )
        )
        bars = [parse_kline_row(row) for row in rows]
        assert all(bar is not None for bar in bars)
        shadow = crowding_shadow_from_klines(bars)
        self.assertAlmostEqual(shadow["avg_trade_size"] or 0.0, 4000.0 / 40.0)
        self.assertAlmostEqual(shadow["taker_buy_quote_ratio"] or 0.0, 1000.0 / 4000.0)
        self.assertAlmostEqual(shadow["trades_spike_ratio"] or 0.0, 40.0 / 10.0)
        factors = factors_from_klines(bars)
        assert factors is not None
        self.assertEqual(factors["avg_trade_size"], shadow["avg_trade_size"])
        self.assertEqual(factors["trades_spike_ratio"], shadow["trades_spike_ratio"])
        self.assertEqual(factors["taker_buy_quote_ratio"], shadow["taker_buy_quote_ratio"])
        series = price_series_from_klines(bars)
        self.assertAlmostEqual(
            factors["volume_strength"],
            volume_strength(series),
            places=6,
        )

        strategy = TechBollFadeStrategy()
        self.assertEqual(strategy.min_change_5m, 2.0)
        self.assertEqual(strategy.min_volume_strength, 0.5)
        rejected = strategy.on_tick(
            {
                "hot_tokens": [
                    {
                        "chain_id": "binance",
                        "token_address": "ZZQBOLLUSDT",
                        "change_5m": -1.0,
                        "volume_strength": 9.0,
                        "boll_lower": 100.0,
                        "price_usd": 1.0,
                        "avg_trade_size": shadow["avg_trade_size"],
                        "trades_spike_ratio": shadow["trades_spike_ratio"],
                        "taker_buy_quote_ratio": shadow["taker_buy_quote_ratio"],
                    }
                ]
            }
        )
        self.assertEqual(rejected, [])
        self.assertEqual(strategy.rejected.get("tech_leg_fail"), 1)

    def test_empty_trades_and_zero_quote_stay_null(self) -> None:
        self.assertEqual(
            crowding_shadow_from_klines([]),
            {
                "avg_trade_size": None,
                "trades_spike_ratio": None,
                "taker_buy_quote_ratio": None,
            },
        )
        missing = [
            parse_kline_row(_rest_bar(i, trades=10, taker_quote="100"))
            for i in range(12)
        ]
        missing.append(parse_kline_row(_rest_bar(12, trades=None, taker_quote="100")))
        assert all(bar is not None for bar in missing)
        shadow = crowding_shadow_from_klines(missing)
        self.assertIsNone(shadow["avg_trade_size"])
        self.assertIsNone(shadow["trades_spike_ratio"])
        self.assertAlmostEqual(shadow["taker_buy_quote_ratio"] or 0.0, 100.0 / 2000.0)
        factors = factors_from_klines(missing)
        assert factors is not None
        self.assertIsNone(factors["avg_trade_size"])
        self.assertIsNone(factors["trades_spike_ratio"])

        zero_trades = [
            parse_kline_row(_rest_bar(i, trades=10, taker_quote="100"))
            for i in range(12)
        ]
        zero_trades.append(parse_kline_row(_rest_bar(12, trades=0, taker_quote="100")))
        assert all(bar is not None for bar in zero_trades)
        zero_shadow = crowding_shadow_from_klines(zero_trades)
        self.assertIsNone(zero_shadow["avg_trade_size"])
        self.assertEqual(zero_shadow["trades_spike_ratio"], 0.0)

        zero_quote = [parse_kline_row(_rest_bar(0, quote=0.0, trades=4, taker_quote="1"))]
        assert zero_quote[0] is not None
        quiet = crowding_shadow_from_klines(zero_quote)
        self.assertEqual(quiet["avg_trade_size"], 0.0)
        self.assertIsNone(quiet["taker_buy_quote_ratio"])
        self.assertIsNone(quiet["trades_spike_ratio"])

        hole = [
            parse_kline_row(_rest_bar(i, trades=None if i == 3 else 10))
            for i in range(13)
        ]
        assert all(bar is not None for bar in hole)
        self.assertIsNone(crowding_shadow_from_klines(hole)["trades_spike_ratio"])

    def test_existing_tables_gain_columns_without_a_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, change_5m REAL
                )
                """
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES ('solana', 'MintPaper', 1.25)"
            )
            conn.execute(
                """
                CREATE TABLE crowding_futures_sentinel (id INTEGER, note TEXT)
                """
            )
            conn.execute(
                "INSERT INTO crowding_futures_sentinel VALUES (1, 'ls_ratio untouched')"
            )
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
                INSERT INTO binance_klines (
                    symbol, interval, open_time_ms, open, high, low, close,
                    volume, quote_volume, close_time_ms, trades
                ) VALUES ('OLDUSDT', '5m', 1, 1, 1, 1, 1, 1, 9, 2, 7)
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
            conn.execute(
                """
                INSERT INTO binance_indicators (
                    symbol, interval, volume_strength, updated_at
                ) VALUES ('OLDUSDT', '5m', 1.25, '2026-11-05T00:00:00Z')
                """
            )
            conn.commit()
            before_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            before_token = _dump(conn, "token_indicators")
            ensure_schema(conn)
            ensure_schema(conn)
            after_tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertEqual(after_tables, before_tables)
            self.assertEqual(_dump(conn, "token_indicators"), before_token)
            self.assertEqual(
                conn.execute("SELECT note FROM crowding_futures_sentinel").fetchone()[0],
                "ls_ratio untouched",
            )
            kline_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(binance_klines)")
            }
            ind_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(binance_indicators)")
            }
            self.assertIn("taker_buy_quote_volume", kline_cols)
            self.assertIn("avg_trade_size", ind_cols)
            self.assertIn("trades_spike_ratio", ind_cols)
            self.assertIn("taker_buy_quote_ratio", ind_cols)
            self.assertIn("change_5m_z", ind_cols)
            old_kline = conn.execute(
                """
                SELECT quote_volume, trades, taker_buy_quote_volume
                FROM binance_klines WHERE symbol = 'OLDUSDT'
                """
            ).fetchone()
            self.assertEqual(tuple(old_kline), (9.0, 7, None))
            old_vs = conn.execute(
                """
                SELECT volume_strength, avg_trade_size, trades_spike_ratio,
                       taker_buy_quote_ratio, change_5m_z
                FROM binance_indicators WHERE symbol = 'OLDUSDT'
                """
            ).fetchone()
            self.assertEqual(tuple(old_vs), (1.25, None, None, None, None))

            bars = []
            for i in range(12):
                parsed = parse_kline_row(_rest_bar(i, trades=10, taker_quote="100"))
                assert parsed is not None
                bars.append(parsed)
            last = parse_kline_row(
                _rest_bar(12, quote=4000.0, trades=40, taker_quote="1000", close=10.0)
            )
            assert last is not None
            bars.append(last)
            stored = persist_klines_and_factors(
                conn,
                SpotSymbol("ZZQBOLLUSDT", "ZZQBOLL"),
                bars,
            )
            assert stored is not None
            self.assertAlmostEqual(stored["avg_trade_size"], 100.0)
            self.assertAlmostEqual(stored["trades_spike_ratio"], 4.0)
            self.assertAlmostEqual(stored["taker_buy_quote_ratio"], 0.25)
            self.assertIsNone(stored["change_5m_z"])
            self.assertAlmostEqual(
                stored["volume_strength"],
                volume_strength(price_series_from_klines(bars)),
                places=6,
            )
            saved_taker = conn.execute(
                """
                SELECT taker_buy_quote_volume FROM binance_klines
                WHERE symbol = 'ZZQBOLLUSDT'
                ORDER BY open_time_ms DESC LIMIT 1
                """
            ).fetchone()
            self.assertAlmostEqual(saved_taker[0], 1000.0)
            saved = conn.execute(
                """
                SELECT avg_trade_size, trades_spike_ratio, taker_buy_quote_ratio,
                       change_5m_z
                FROM binance_indicators WHERE symbol = 'ZZQBOLLUSDT'
                """
            ).fetchone()
            self.assertEqual(tuple(saved), (100.0, 4.0, 0.25, None))
            hot = list_binance_hot_tokens(conn, limit=5, fresh_sec=3600)
            shaped = [row for row in hot if row["token_address"] == "ZZQBOLLUSDT"]
            self.assertEqual(len(shaped), 1)
            for key in (
                "avg_trade_size",
                "trades_spike_ratio",
                "taker_buy_quote_ratio",
                "change_5m_z",
            ):
                self.assertNotIn(key, shaped[0])
            self.assertEqual(_dump(conn, "token_indicators"), before_token)
            conn.close()


def _bars_from_closes(closes: list[float]) -> list:
    bars = []
    for i, close in enumerate(closes):
        bar = parse_kline_row(_rest_bar(i, close=close))
        assert bar is not None
        bars.append(bar)
    return bars


class Change5mZTests(unittest.TestCase):
    def test_z_is_latest_pct_over_prior_20_and_not_volatility_24h(self) -> None:
        self.assertEqual(CHANGE_5M_Z_PRIOR_RETURNS, 20)
        closes = [100.0]
        for i in range(20):
            closes.append(closes[-1] * (1.5 if i % 2 == 0 else 0.5))
        closes.append(closes[-1] * 2.0)
        bars = _bars_from_closes(closes)
        # Prior 20 returns are ±50 percentage points. Current step is +100.
        expected = 2.0 * math.sqrt(19.0 / 20.0)
        z = change_5m_z_from_klines(bars)
        self.assertAlmostEqual(z or 0.0, expected, places=9)
        factors = factors_from_klines(bars)
        assert factors is not None
        self.assertAlmostEqual(factors["change_5m"], 100.0, places=6)
        self.assertAlmostEqual(factors["change_5m_z"], expected, places=9)
        self.assertAlmostEqual(factors["change_5m_z"], factors["change_5m"] / (50.0 * math.sqrt(20.0 / 19.0)), places=9)
        vol = factors["volatility_24h"]
        assert vol is not None and vol > 0
        self.assertNotAlmostEqual(factors["change_5m_z"], factors["change_5m"] / vol, places=4)

        strategy = TechBollFadeStrategy()
        self.assertEqual(strategy.min_change_5m, 2.0)
        self.assertEqual(strategy.min_volume_strength, 0.5)
        rejected = strategy.on_tick(
            {
                "hot_tokens": [
                    {
                        "chain_id": "binance",
                        "token_address": "ZZQBOLLUSDT",
                        "change_5m": -1.0,
                        "volume_strength": 3.0,
                        "boll_lower": 10.0,
                        "price_usd": 1.0,
                        "change_5m_z": -12.0,
                    }
                ]
            }
        )
        self.assertEqual(rejected, [])
        self.assertEqual(strategy.rejected.get("tech_leg_fail"), 1)
        hit = strategy.on_tick(
            {
                "hot_tokens": [
                    {
                        "chain_id": "binance",
                        "token_address": "ZZQBOLLUSDT",
                        "change_5m": -3.0,
                        "volume_strength": 0.8,
                        "boll_lower": 2.0,
                        "price_usd": 1.0,
                        "change_5m_z": -12.0,
                    }
                ]
            }
        )
        self.assertEqual(len(hit), 1)
        self.assertNotIn("change_5m_z", hit[0])
        self.assertIn("c5=-3.000<=-2.0", hit[0]["reason"])

    def test_constant_short_and_zero_sigma_stay_null(self) -> None:
        flat = _bars_from_closes([100.0] * 30)
        self.assertIsNone(change_5m_z_from_klines(flat))
        flat_factors = factors_from_klines(flat)
        assert flat_factors is not None
        self.assertEqual(flat_factors["change_5m"], 0.0)
        self.assertIsNone(flat_factors["change_5m_z"])

        short = _bars_from_closes([100.0 + i for i in range(10)])
        self.assertLess(len(short), CHANGE_5M_Z_PRIOR_RETURNS + 2)
        self.assertIsNone(change_5m_z_from_klines(short))
        short_factors = factors_from_klines(short)
        assert short_factors is not None
        self.assertIsNotNone(short_factors["change_5m"])
        self.assertIsNone(short_factors["change_5m_z"])

        doubled = [100.0]
        for _ in range(25):
            doubled.append(doubled[-1] * 2.0)
        zero_sigma = _bars_from_closes(doubled)
        zero_factors = factors_from_klines(zero_sigma)
        assert zero_factors is not None
        self.assertAlmostEqual(zero_factors["change_5m"], 100.0, places=6)
        self.assertEqual(zero_factors["volatility_24h"], 0.0)
        self.assertIsNone(zero_factors["change_5m_z"])
        self.assertIsNone(change_5m_z_from_klines([]))


if __name__ == "__main__":
    unittest.main()
