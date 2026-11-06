"""Binance-native universe and kline factors for live_bnb_boll_fade."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import decision_engine  # noqa: F401 — crawler/ on path for logging_setup
from decision_engine import config as de_config
from decision_engine.core.bnb_native import (
    _boll_lower,
    _change_5m,
    _trailing_hour_volume,
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
    PURPOSE_LIVE_CANDIDATE,
    SignalBus,
)
from decision_engine.run import main as run_main
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


def _zone_hit_bars() -> list[tuple[int, float, float, float, float, float, float, int]]:
    """``_hit_closes`` plus a last-bar wick and trade counts.

    Close series is unchanged, so change_5m / boll_lower stay on the old
    diagnostic. Low is set so alpha against SMA(20) is 0.20. Last-bar
    trades are half the baseline, so trade_size_ratio is 2.
    """
    closes = _hit_closes()
    mid = sum(closes[-20:]) / 20.0
    price = closes[-1]
    low = (price - 0.20 * mid) / 0.80
    bars = _bars(closes)
    out = []
    for index, (ts, op, hi, _lo, close, vol, quote) in enumerate(bars):
        bar_low = low if index == len(bars) - 1 else _lo
        trades = 50 if index == len(bars) - 1 else 100
        out.append((ts, op, hi, bar_low, close, vol, quote, trades))
    return out


def _admit_quote(conn: sqlite3.Connection, symbol: str, quote: float = 3_000_000.0) -> None:
    """Fresh Binance ticker quoteVolume so a new buy can clear the 3M floor."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ccxt_ticks (
            exchange TEXT, symbol TEXT, ts INTEGER,
            quote_volume_24h REAL, volume_24h REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO ccxt_ticks (exchange, symbol, ts, quote_volume_24h, volume_24h) "
        "VALUES ('binance', ?, ?, ?, 1)",
        (symbol, int(time.time() * 1000), quote),
    )


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

    def test_trailing_hour_prefers_quote_volume(self) -> None:
        bars = _bars([100.0] * 12, volume=2.0, quote=9.0)
        self.assertAlmostEqual(_trailing_hour_volume(bars, 11), 12 * 9.0)
        zero_quote = [(*row[:5], 4.0, 0.0) for row in bars]
        self.assertAlmostEqual(_trailing_hour_volume(zero_quote, 11), 0.0)

    def test_trailing_hour_falls_back_to_base_when_quote_is_missing(self) -> None:
        bars = _bars([100.0] * 12, volume=3.0, quote=50.0)
        missing = [(*row[:6], None) for row in bars]
        self.assertAlmostEqual(_trailing_hour_volume(missing, 11), 12 * 3.0)
        mixed = list(missing)
        mixed[-1] = (*bars[-1][:5], 3.0, 50.0)
        self.assertAlmostEqual(_trailing_hour_volume(mixed, 11), 11 * 3.0 + 50.0)

    def test_kline_volume_strength_follows_quote_not_base(self) -> None:
        closes = [100.0] * 24
        quotes = [10.0] * 23 + [400.0]
        quote_bars = []
        fallback_bars = []
        for index, row in enumerate(_bars(closes, volume=1.0, quote=10.0)):
            quote_bars.append((*row[:5], 1.0, quotes[index]))
            fallback_bars.append((*row[:5], quotes[index], None))
        now = quote_bars[-1][0] + 5 * 60 * 1000
        from_quote = factors_from_klines(quote_bars, now_ms=now)
        from_base = factors_from_klines(fallback_bars, now_ms=now)
        flat_base = factors_from_klines(_bars(closes, volume=1.0, quote=1.0), now_ms=now)
        self.assertIsNotNone(from_quote)
        self.assertIsNotNone(from_base)
        self.assertIsNotNone(flat_base)
        assert from_quote is not None and from_base is not None and flat_base is not None
        self.assertAlmostEqual(from_quote["volume_strength"], from_base["volume_strength"])
        self.assertNotAlmostEqual(
            from_quote["volume_strength"], flat_base["volume_strength"]
        )

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
                    boll_mid REAL,
                    kline_low REAL,
                    trade_size_ratio REAL,
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
                    boll_mid, kline_low, trade_size_ratio,
                    quote_volume_24h, updated_at
                ) VALUES (?, ?, 'USDT', ?, '5m', ?, ?, ?, ?, ?, ?, ?, ?, '2026-11-05T06:00:00Z')
                """,
                [
                    # alpha=(0.9-0.825)/(1.2-0.825)=0.20 and ratio 2 → entry zone
                    ("ZZQBOLLUSDT", "ZZQBOLL", "TRADING", 0.9, -3.0, 0.9, 1.0, 1.2, 0.825, 2.0, 90_000),
                    ("ZZQFLATUSDT", "ZZQFLAT", "TRADING", 0.9, -0.1, 0.9, 1.0, None, None, None, 90_000),
                    ("ZZQTHINUSDT", "ZZQTHIN", "TRADING", 0.9, -3.0, 0.9, 1.0, None, None, None, 1_000),
                    ("ZZQHALTUSDT", "ZZQHALT", "BREAK", 0.9, -3.0, 0.9, 1.0, None, None, None, 90_000),
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
            _admit_quote(conn, "ZZQBOLL/USDT")
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
            self.assertEqual(extras["and_mode"], "entry_zone")
            self.assertEqual(extras["entry_reason"], "large_trade_market")
            self.assertEqual(extras["order_type"], "MARKET")
            self.assertAlmostEqual(extras["take_profit_price"], 1.2)
            self.assertIn("stop_price", extras)
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
                    volume REAL, quote_volume REAL, trades INTEGER
                )
                """
            )
            conn.executemany(
                "INSERT INTO binance_klines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("ZZQBOLLUSDT", "5m", ts, op, hi, lo, close, vol, quote, trades)
                    for ts, op, hi, lo, close, vol, quote, trades in _zone_hit_bars()
                ],
            )
            _admit_quote(conn, "ZZQBOLL/USDT")
            conn.commit()
            conn.close()
            rows, payload = self._tick(db)
            self.assertEqual(payload["bnb_factor_source"], "binance_klines")
            self.assertEqual(len(rows), 1)
            extras = json.loads(rows[0]["raw_json"])["extras"]
            self.assertEqual(extras["binance_symbol"], "ZZQBOLLUSDT")
            self.assertEqual(extras["factor_source"], "binance_klines")
            self.assertEqual(extras["kline_interval"], "5m")
            self.assertEqual(extras["and_mode"], "entry_zone")
            self.assertEqual(extras["entry_reason"], "large_trade_market")
            self.assertEqual(extras["order_type"], "MARKET")
            self.assertAlmostEqual(extras["take_profit_price"], extras["boll_mid"])
            self.assertLess(extras["stop_price"], extras["price_usd"])
            self.assertLessEqual(extras["change_5m"], -2.0)
            self.assertGreaterEqual(extras["volume_strength"], 0.5)
            self.assertLess(extras["price_usd"], extras["boll_lower"])
            self.assertEqual(extras["legacy_gate"], "diagnostic_only")

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
                    volume REAL, quote_volume REAL, close_time_ms INTEGER,
                    trades INTEGER
                )
                """
            )
            for symbol in ("ZZQBOLLUSDT", "ZZQNOPEUSDT"):
                conn.executemany(
                    """
                    INSERT INTO binance_klines VALUES (?, '5m', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (symbol, ts, op, hi, lo, close, vol, quote, ts + 5 * 60 * 1000 - 1, trades)
                        for ts, op, hi, lo, close, vol, quote, trades in _zone_hit_bars()
                    ],
                )
            _admit_quote(conn, "ZZQBOLL/USDT")
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


class ScanWindowTests(unittest.TestCase):
    """live_bnb_boll_fade scan window, not an entry gate."""

    def _seed_ranked_dump(self, db: Path, *, decoys: int = 60) -> None:
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE binance_indicators (
                symbol TEXT PRIMARY KEY,
                base_asset TEXT,
                quote_asset TEXT,
                status TEXT,
                price_usd REAL,
                change_5m REAL,
                volume_strength REAL,
                boll_lower REAL,
                boll_mid REAL,
                kline_low REAL,
                trade_size_ratio REAL,
                quote_volume_24h REAL
            )
            """
        )
        liq = float(de_config.DECISION_MIN_LIQ_USD) + 10_000.0
        rows = [
            (
                f"D{index:03d}USDT",
                f"D{index:03d}",
                "USDT",
                "TRADING",
                5.0,
                -10.0,
                0.9,
                1.0,
                None,
                None,
                None,
                liq,
            )
            for index in range(decoys)
        ]
        # KEEP is the least negative change, so a short scan drops it.
        # alpha=(1-0.75)/(2-0.75)=0.20, ratio 1.5 → the entry zone emits.
        rows.append(
            ("KEEPUSDT", "KEEP", "USDT", "TRADING", 1.0, -3.0, 0.9, 2.0, 2.0, 0.75, 1.5, liq)
        )
        conn.executemany(
            """
            INSERT INTO binance_indicators (
                symbol, base_asset, quote_asset, status,
                price_usd, change_5m, volume_strength, boll_lower,
                boll_mid, kline_low, trade_size_ratio, quote_volume_24h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute(
            "CREATE TABLE symbols (symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT)"
        )
        conn.executemany(
            "INSERT INTO symbols VALUES (?, 'TRADING', ?, 'USDT')",
            [(row[0], row[1]) for row in rows],
        )
        for row in rows:
            _admit_quote(conn, f"{row[1]}/USDT")
        conn.commit()
        conn.close()

    def _tick(self, db: Path, *, hot_limit: int | None) -> tuple[list[sqlite3.Row], dict, int]:
        adapter = IndicatorDataAdapter(db)
        bus = SignalBus(db, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
        try:
            kwargs = {}
            if hot_limit is not None:
                kwargs["hot_limit"] = hot_limit
            engine = DecisionEngine(
                adapter,
                bus,
                [get_strategy("tech_boll_fade_v1")],
                risk=RiskManager(cooldown_sec=0),
                **kwargs,
            )
            payload = engine.tick()
            limit = engine.hot_limit
        finally:
            bus.close()
            adapter.close()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            rows = list(conn.execute("SELECT token_address FROM decision_log"))
        finally:
            conn.close()
        return rows, payload, limit

    def test_default_scan_keeps_a_break_ranked_past_fifty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._seed_ranked_dump(db)
            rows, payload, limit = self._tick(db, hot_limit=None)
            self.assertGreaterEqual(limit, 600)
            self.assertEqual(payload["bnb_factor_source"], "binance_indicators")
            self.assertGreaterEqual(payload["hot_loaded"], 61)
            self.assertEqual([row["token_address"] for row in rows], ["KEEPUSDT"])

    def test_explicit_limit_still_truncates_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._seed_ranked_dump(db)
            rows, payload, limit = self._tick(db, hot_limit=20)
            self.assertEqual(limit, 20)
            self.assertEqual(payload["hot_loaded"], 20)
            self.assertEqual(rows, [])

    def test_other_purpose_keeps_the_global_hot_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            sqlite3.connect(db).close()
            adapter = IndicatorDataAdapter(db)
            bus = SignalBus(db, purpose=PURPOSE_LIVE_CANDIDATE)
            try:
                engine = DecisionEngine(
                    adapter,
                    bus,
                    [get_strategy("tech_boll_fade_v1")],
                    risk=RiskManager(cooldown_sec=0),
                )
                self.assertEqual(engine.hot_limit, de_config.HOT_TOKEN_LIMIT)
                self.assertLess(engine.hot_limit, 600)
            finally:
                bus.close()
                adapter.close()

    def _capture_cli_limit(self, argv: list[str]) -> int:
        captured: dict[str, int] = {}

        def spy(engine, adapter, bus, strategies, **kwargs):
            del engine, adapter, strategies
            captured["hot_limit"] = int(kwargs["hot_limit"])
            raise RuntimeError("stop-before-tick")

        original = DecisionEngine.__init__
        DecisionEngine.__init__ = spy  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                run_main(argv)
        finally:
            DecisionEngine.__init__ = original  # type: ignore[method-assign]
        return captured["hot_limit"]

    def test_cli_live_bnb_defaults_to_the_wide_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            sqlite3.connect(db).close()
            limit = self._capture_cli_limit(
                [
                    "--strategy",
                    "tech_boll_fade_v1",
                    "--purpose",
                    PURPOSE_LIVE_BNB_BOLL_FADE,
                    "--once",
                    "--db",
                    str(db),
                ]
            )
            self.assertGreaterEqual(limit, 600)

    def test_cli_other_purpose_and_explicit_limit_stay_put(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            sqlite3.connect(db).close()
            other = self._capture_cli_limit(
                [
                    "--strategy",
                    "tech_boll_fade_v1",
                    "--purpose",
                    PURPOSE_LIVE_CANDIDATE,
                    "--once",
                    "--db",
                    str(db),
                ]
            )
            explicit = self._capture_cli_limit(
                [
                    "--strategy",
                    "tech_boll_fade_v1",
                    "--purpose",
                    PURPOSE_LIVE_BNB_BOLL_FADE,
                    "--once",
                    "--limit",
                    "20",
                    "--db",
                    str(db),
                ]
            )
            self.assertEqual(other, de_config.HOT_TOKEN_LIMIT)
            self.assertEqual(explicit, 20)


if __name__ == "__main__":
    unittest.main()
