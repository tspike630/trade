"""Binance-native universe and kline factors for live_bnb_boll_fade."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import decision_engine  # noqa: F401 — crawler/ on path for logging_setup
from decision_engine.core.bnb_native import (
    _boll_lower,
    _change_5m,
    _volume_strength,
    factors_from_klines,
    load_native_candidate_rows,
    symbols_from_connection,
    symbols_from_json_payload,
)
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    SignalBus,
)
from decision_engine.strategies import get_strategy

_ENV_KEYS = (
    "LIVE_BNB_IGNORE_REPO_UNIVERSE",
    "LIVE_BNB_FACTOR_SOURCE",
    "LIVE_BNB_DEX_HOT_MAP",
    "LIVE_BNB_KLINE_FETCH",
    "BINANCE_SPOT_UNIVERSE_DB",
    "BINANCE_SPOT_UNIVERSE_JSON",
)


def _hit_closes() -> list[float]:
    return [100.0] * 19 + [97.0]


def _bars(
    closes: list[float],
    *,
    volume: float = 1.0,
    quote: float = 10_000.0,
    start_ms: int = 1_600_000_000_000,
) -> list[tuple[int, float, float, float, float, float, float]]:
    step = 5 * 60 * 1000
    out = []
    for index, close in enumerate(closes):
        ts = start_ms + index * step
        out.append((ts, close, close, close, close, volume, quote))
    return out


class FactorMathTests(unittest.TestCase):
    def test_change_and_boll_match_the_existing_parameters(self) -> None:
        closes = _hit_closes()
        step = 300.0
        series = [(1_600_000_000.0 + index * step, px) for index, px in enumerate(closes)]
        # Bar close is one bar after open; the helper above is open-time.
        # change_5m compares the last two close timestamps 300s apart.
        self.assertAlmostEqual(_change_5m(series), -3.0)
        window = closes[-20:]
        mid = sum(window) / 20.0
        var = sum((px - mid) ** 2 for px in window) / 20.0
        expected = mid - 2.0 * math.sqrt(var)
        self.assertAlmostEqual(_boll_lower(closes) or 0.0, expected)
        self.assertLess(closes[-1], expected)

    def test_volume_strength_is_current_over_mean(self) -> None:
        points = [(float(index), 12.0) for index in range(10)]
        self.assertAlmostEqual(_volume_strength(points) or 0.0, 1.0)
        points[-1] = (9.0, 6.0)
        self.assertAlmostEqual(_volume_strength(points) or 0.0, 6.0 / 11.4)

    def test_forming_bar_is_dropped(self) -> None:
        closed = _bars([100.0] * 20)
        last_open = closed[-1][0]
        forming = (last_open + 5 * 60 * 1000, 50.0, 50.0, 50.0, 50.0, 1.0, 10_000.0)
        now = last_open + 5 * 60 * 1000
        factors = factors_from_klines([*closed, forming], now_ms=now)
        self.assertIsNotNone(factors)
        assert factors is not None
        self.assertAlmostEqual(factors["price_usd"], 100.0)
        self.assertAlmostEqual(factors["change_5m"], 0.0)


class SymbolSnapshotTests(unittest.TestCase):
    def test_json_keeps_trading_usdt_only(self) -> None:
        payload = {
            "count": 3,
            "symbols": [
                "BTCUSDT",
                {"symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT"},
                {"symbol": "BNBUSDT", "status": "BREAK"},
                {"symbol": "SOLUSDC", "status": "TRADING", "quoteAsset": "USDC"},
                {"symbol": "BTCUSDT_PERP", "status": "TRADING"},
                "BTC/USDT:USDT",
            ],
        }
        self.assertEqual(symbols_from_json_payload(payload), {"BTCUSDT", "ETHUSDT"})

    def test_symbols_table_filters_status(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE symbols (
                symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT, is_spot INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO symbols VALUES (?, ?, ?, ?, ?)",
            [
                ("ZZQBOLLUSDT", "TRADING", "ZZQBOLL", "USDT", 1),
                ("ZZQHALTUSDT", "BREAK", "ZZQHALT", "USDT", 1),
                ("ZZQUSDCUSDT", "TRADING", "ZZQUSDC", "USDC", 1),
                ("ZZQNOPEUSDT", "TRADING", "ZZQNOPE", "USDT", 0),
            ],
        )
        present, found = symbols_from_connection(conn)
        conn.close()
        self.assertTrue(present)
        self.assertEqual(found, {"ZZQBOLLUSDT"})


class NativeEmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {key: os.environ.get(key) for key in _ENV_KEYS}
        os.environ["LIVE_BNB_IGNORE_REPO_UNIVERSE"] = "1"
        for key in _ENV_KEYS[1:]:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _tick(self, db: Path) -> tuple[list[sqlite3.Row], dict]:
        adapter = IndicatorDataAdapter(db)
        bus = SignalBus(db, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
        try:
            engine = DecisionEngine(
                adapter,
                bus,
                [get_strategy("tech_boll_fade_v1")],
                risk=RiskManager(cooldown_sec=0),
                hot_limit=20,
            )
            payload = engine.tick()
        finally:
            bus.close()
            adapter.close()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            rows = list(conn.execute("SELECT * FROM decision_log ORDER BY id"))
        finally:
            conn.close()
        return rows, payload

    def test_indicator_table_emits_binance_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE symbols (
                    symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO symbols VALUES (?, ?, ?, ?)",
                [
                    ("ZZQBOLLUSDT", "TRADING", "ZZQBOLL", "USDT"),
                    ("ZZQFLATUSDT", "TRADING", "ZZQFLAT", "USDT"),
                    ("ZZQTHINUSDT", "TRADING", "ZZQTHIN", "USDT"),
                    ("ZZQHALTUSDT", "BREAK", "ZZQHALT", "USDT"),
                ],
            )
            conn.execute(
                """
                CREATE TABLE binance_indicators (
                    symbol TEXT PRIMARY KEY,
                    base_asset TEXT,
                    quote_asset TEXT,
                    status TEXT,
                    interval TEXT,
                    price_usd REAL,
                    change_5m REAL,
                    volume_strength REAL,
                    boll_lower REAL,
                    quote_volume_24h REAL,
                    updated_at TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO binance_indicators (
                    symbol, base_asset, quote_asset, status, interval,
                    price_usd, change_5m, volume_strength, boll_lower,
                    quote_volume_24h, updated_at
                ) VALUES (?, ?, 'USDT', ?, '5m', ?, ?, ?, ?, ?, '2026-11-05T06:00:00Z')
                """,
                [
                    ("ZZQBOLLUSDT", "ZZQBOLL", "TRADING", 0.9, -3.0, 0.9, 1.0, 90_000),
                    ("ZZQFLATUSDT", "ZZQFLAT", "TRADING", 0.9, -0.1, 0.9, 1.0, 90_000),
                    ("ZZQTHINUSDT", "ZZQTHIN", "TRADING", 0.9, -3.0, 0.9, 1.0, 1_000),
                    ("ZZQHALTUSDT", "ZZQHALT", "BREAK", 0.9, -3.0, 0.9, 1.0, 90_000),
                ],
            )
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, change_5m REAL,
                    volume_strength REAL, boll_lower REAL, jev_strength REAL,
                    tier TEXT, updated_at TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("solana", "MintShouldNotEmit", -3.0, 0.9, 1.0, 0.9, "hot", "2026-11-05T00:00:00Z"),
            )
            conn.commit()
            conn.close()
            rows, payload = self._tick(db)
            self.assertEqual(payload["bnb_factor_source"], "binance_indicators")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["purpose"], PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(row["strategy_id"], "tech_boll_fade_v1")
            self.assertEqual(row["chain_id"], "binance")
            self.assertEqual(row["token_address"], "ZZQBOLLUSDT")
            self.assertEqual(row["mode"], "dry_run")
            raw = json.loads(row["raw_json"])
            extras = raw["extras"]
            self.assertEqual(extras["binance_symbol"], "ZZQBOLLUSDT")
            self.assertEqual(extras["ccxt_symbol"], "ZZQBOLL/USDT")
            self.assertEqual(extras["market"], "spot")
            self.assertEqual(extras["quote_asset"], "USDT")
            self.assertEqual(extras["universe"], "binance_spot_usdt")
            self.assertEqual(extras["factor_source"], "binance_indicators")
            self.assertEqual(extras["and_mode"], "tech_fade&boll_lower")
            self.assertNotIn("local_score", extras)
            self.assertNotIn("source_token_address", extras)
            self.assertNotIn("MintShouldNotEmit", row["token_address"])

    def test_klines_feed_emits_when_indicator_table_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE symbols (symbol TEXT, status TEXT)")
            conn.execute("INSERT INTO symbols VALUES ('ZZQBOLLUSDT', 'TRADING')")
            conn.execute(
                """
                CREATE TABLE binance_klines (
                    symbol TEXT, timeframe TEXT, ts INTEGER,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, quote_volume REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO binance_klines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("ZZQBOLLUSDT", "5m", ts, op, hi, lo, close, vol, quote)
                    for ts, op, hi, lo, close, vol, quote in _bars(_hit_closes())
                ],
            )
            conn.commit()
            conn.close()
            rows, payload = self._tick(db)
            self.assertEqual(payload["bnb_factor_source"], "binance_klines")
            self.assertEqual(len(rows), 1)
            extras = json.loads(rows[0]["raw_json"])["extras"]
            self.assertEqual(extras["binance_symbol"], "ZZQBOLLUSDT")
            self.assertEqual(extras["factor_source"], "binance_klines")
            self.assertEqual(extras["kline_interval"], "5m")
            self.assertLessEqual(extras["change_5m"], -2.0)
            self.assertGreaterEqual(extras["volume_strength"], 0.5)
            self.assertLess(extras["price_usd"], extras["boll_lower"])

    def test_indicator_values_win_over_klines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE symbols (symbol TEXT, status TEXT)")
            conn.execute("INSERT INTO symbols VALUES ('ZZQBOLLUSDT', 'TRADING')")
            conn.execute(
                """
                CREATE TABLE binance_indicators (
                    symbol TEXT, change_5m REAL, volume_strength REAL,
                    boll_lower REAL, price_usd REAL, liquidity_usd REAL
                )
                """
            )
            conn.execute(
                "INSERT INTO binance_indicators VALUES (?, ?, ?, ?, ?, ?)",
                ("ZZQBOLLUSDT", -0.1, 0.9, 1.0, 0.9, 90_000),
            )
            conn.execute(
                """
                CREATE TABLE ccxt_ohlcv (
                    exchange TEXT, symbol TEXT, timeframe TEXT, ts INTEGER,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, quote_volume REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO ccxt_ohlcv VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("binance", "ZZQBOLL/USDT", "5m", ts, op, hi, lo, close, vol, quote)
                    for ts, op, hi, lo, close, vol, quote in _bars(_hit_closes())
                ],
            )
            conn.commit()
            conn.close()
            rows, payload = self._tick(db)
            self.assertEqual(payload["bnb_factor_source"], "binance_indicators")
            self.assertEqual(rows, [])

    def test_empty_snapshot_does_not_fall_back_to_seeded_listings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE symbols (symbol TEXT, status TEXT)")
            conn.execute(
                "CREATE TABLE cex_listing_events (exchange TEXT, symbol TEXT, base TEXT, quote TEXT)"
            )
            conn.execute(
                "INSERT INTO cex_listing_events VALUES ('binance', 'ZZQBOLL/USDT', 'ZZQBOLL', 'USDT')"
            )
            conn.commit()
            conn.close()
            rows, payload = self._tick(db)
            self.assertEqual(payload["bnb_factor_source"], "binance_klines")
            self.assertEqual(rows, [])

    def test_json_snapshot_is_the_universe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "d.db"
            listing = root / "universe.json"
            listing.write_text(
                json.dumps({"symbols": [{"symbol": "ZZQBOLLUSDT", "status": "TRADING"}]}),
                encoding="utf-8",
            )
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE binance_klines (
                    symbol TEXT, interval TEXT, open_time_ms INTEGER,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, quote_volume REAL, close_time_ms INTEGER
                )
                """
            )
            for symbol in ("ZZQBOLLUSDT", "ZZQNOPEUSDT"):
                conn.executemany(
                    """
                    INSERT INTO binance_klines VALUES (?, '5m', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (symbol, ts, op, hi, lo, close, vol, quote, ts + 5 * 60 * 1000 - 1)
                        for ts, op, hi, lo, close, vol, quote in _bars(_hit_closes())
                    ],
                )
            conn.commit()
            conn.close()
            os.environ["BINANCE_SPOT_UNIVERSE_JSON"] = str(listing)
            rows, payload = self._tick(db)
            self.assertEqual(payload["bnb_factor_source"], "binance_klines")
            self.assertEqual(len(rows), 1)
            extras = json.loads(rows[0]["raw_json"])["extras"]
            self.assertEqual(extras["binance_symbol"], "ZZQBOLLUSDT")

    def test_missing_feed_fails_closed_and_does_not_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE symbols (symbol TEXT, status TEXT)")
            conn.execute("INSERT INTO symbols VALUES ('ZZQBOLLUSDT', 'TRADING')")
            conn.commit()
            called: list[str] = []

            def _boom(symbol: str) -> list:
                called.append(symbol)
                raise AssertionError("fetch should stay off")

            rows, stats = load_native_candidate_rows(
                conn,
                db,
                fetch_klines=None,
            )
            import decision_engine.core.bnb_native as native

            original = native._public_fetch
            native._public_fetch = _boom  # type: ignore[assignment]
            try:
                rows, stats = load_native_candidate_rows(conn, db)
            finally:
                native._public_fetch = original
            conn.close()
            self.assertEqual(rows, [])
            self.assertEqual(stats["listed"], 1)
            self.assertEqual(stats["candidates"], 0)
            self.assertEqual(called, [])

    def test_injected_fetch_stamps_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE symbols (symbol TEXT, status TEXT)")
            conn.execute("INSERT INTO symbols VALUES ('ZZQBOLLUSDT', 'TRADING')")
            conn.commit()
            rows, _stats = load_native_candidate_rows(
                conn,
                db,
                now_ms=1_700_000_000_000,
                fetch_klines=lambda _symbol: _bars(_hit_closes()),
            )
            conn.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["binance_symbol"], "ZZQBOLLUSDT")
            self.assertEqual(rows[0]["factor_source"], "binance_kline_fetch")
            self.assertGreaterEqual(rows[0]["liquidity_usd"], 50_000)

    def test_binance_source_does_not_fill_from_dex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, change_5m REAL,
                    volume_strength REAL, boll_lower REAL, jev_strength REAL,
                    tier TEXT, updated_at TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("solana", "MintA", -3.0, 0.9, 1.0, 0.9, "hot", "2026-11-05T00:00:00Z"),
            )
            conn.execute(
                "CREATE TABLE cex_listing_events (exchange TEXT, symbol TEXT, base TEXT, quote TEXT)"
            )
            conn.execute(
                "INSERT INTO cex_listing_events VALUES ('binance', 'ZZQBOLL/USDT', 'ZZQBOLL', 'USDT')"
            )
            conn.commit()
            conn.close()
            os.environ["LIVE_BNB_FACTOR_SOURCE"] = "binance"
            rows, payload = self._tick(db)
            self.assertEqual(payload["bnb_factor_source"], "binance_indicators")
            self.assertEqual(rows, [])

    def test_dex_map_ignores_indicator_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE binance_indicators (
                    symbol TEXT, status TEXT, price_usd REAL, change_5m REAL,
                    volume_strength REAL, boll_lower REAL, quote_volume_24h REAL,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO binance_indicators VALUES (
                    'ONLYCEXUSDT', 'TRADING', 0.8, -4.0, 0.9, 1.0, 90000, '2026-11-05T06:00:00Z'
                )
                """
            )
            conn.commit()
            conn.close()
            os.environ["LIVE_BNB_FACTOR_SOURCE"] = "dex_map"
            rows, payload = self._tick(db)
            self.assertEqual(payload["bnb_factor_source"], "dex_map")
            self.assertNotIn("ONLYCEXUSDT", {row["token_address"] for row in rows})


if __name__ == "__main__":
    unittest.main()
