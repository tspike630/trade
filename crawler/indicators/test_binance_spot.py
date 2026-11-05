"""Binance spot klines → tech_boll side tables. DEX token_indicators stay put."""

from __future__ import annotations

import json
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
    SpotSymbol,
    canonical_spot_usdt,
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


if __name__ == "__main__":
    unittest.main()
