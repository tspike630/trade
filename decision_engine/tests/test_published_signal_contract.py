"""Router result must still be on the row SignalBus writes.

Ordinary State 3 keeps LIMIT_MAKER on the decision and used to drop it
unless a news shock was applied. volume_z and obi_top5 were evaluated from
the hot row and not copied. Execution reads those keys from the published
extras. Thresholds, missing-book allow, and size math are unchanged.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import decision_engine  # noqa: F401
from decision_engine import config as de_config
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.news_shock_gate import load_news_snapshot
from decision_engine.core.regime_strategy_router import (
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    apply_five_state_regime_router,
)
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE, SignalBus
from decision_engine.strategies import get_strategy

_HOUR_MS = 3_600_000
_CLOSE_SPAN = _HOUR_MS - 1


def _extras(db: Path) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT action, size_pct, raw_json FROM decision_log ORDER BY id"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    raw = json.loads(row["raw_json"])
    extras = dict(raw.get("extras") or {})
    extras["_action"] = row["action"]
    extras["_size_pct"] = row["size_pct"]
    return extras


def _seed_state3(db: Path, book: Path, now_ms: int) -> None:
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
            quote_volume_24h REAL,
            xs_breadth_1h REAL,
            xs_rank_pct_1h REAL
        )
        """
    )
    low, span = 100.0, 10.0
    price = low + 0.08 * span
    conn.execute(
        """
        INSERT INTO binance_indicators VALUES (
            'FADEUSDT', 'FADE', 'USDT', 'TRADING', ?, -4.0, 0.9, ?, ?, ?, 1.3, ?, 0.75, 0.85
        )
        """,
        (price, price + 1.0, low + span, low, float(de_config.DECISION_MIN_LIQ_USD) + 10_000.0),
    )
    conn.execute(
        "CREATE TABLE symbols (symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT)"
    )
    conn.execute("INSERT INTO symbols VALUES ('FADEUSDT', 'TRADING', 'FADE', 'USDT')")
    conn.execute(
        """
        CREATE TABLE ccxt_ticks (
            exchange TEXT, symbol TEXT, ts INTEGER,
            quote_volume_24h REAL, volume_24h REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO ccxt_ticks VALUES ('binance', 'FADE/USDT', ?, 3000000, 1)",
        (now_ms,),
    )
    conn.execute(
        """
        CREATE TABLE binance_klines (
            symbol TEXT, interval TEXT, open_time_ms INTEGER,
            close_time_ms INTEGER, quote_volume REAL
        )
        """
    )
    latest_open = (now_ms // _HOUR_MS) * _HOUR_MS - _HOUR_MS
    if latest_open + _CLOSE_SPAN > now_ms:
        latest_open -= _HOUR_MS
    for index in range(21):
        opened = latest_open - (20 - index) * _HOUR_MS
        quote = 100.0 if index < 20 else 130.0
        if index == 3:
            quote = 160.0
        conn.execute(
            "INSERT INTO binance_klines VALUES ('FADEUSDT', '1h', ?, ?, ?)",
            (opened, opened + _CLOSE_SPAN, quote),
        )
    conn.commit()
    conn.close()

    deriv = sqlite3.connect(book)
    deriv.execute(
        """
        CREATE TABLE binance_spot_microstructure (
            symbol TEXT, obi_top5 REAL, obi_top5_source_ts_ms INTEGER,
            obi_top5_error TEXT, orderbook_imbalance_20 REAL
        )
        """
    )
    deriv.execute(
        "INSERT INTO binance_spot_microstructure VALUES ('FADEUSDT', -0.10, ?, NULL, -0.55)",
        (now_ms,),
    )
    deriv.commit()
    deriv.close()


class PublishedSignalTests(unittest.TestCase):
    def test_engine_tick_publishes_state3_maker_and_source_fields(self) -> None:
        now_ms = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "decision.db"
            book = root / "public_derivatives.db"
            _seed_state3(db, book, now_ms)
            saved = {
                key: os.environ.get(key)
                for key in ("PUBLIC_DERIVATIVES_DB_PATH", "NEWS_EVENTS_DB_PATH", "LIVE_BNB_FACTOR_SOURCE")
            }
            os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = str(book)
            os.environ["NEWS_EVENTS_DB_PATH"] = str(root / "missing-news.db")
            os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
            adapter = IndicatorDataAdapter(db)
            bus = SignalBus(db, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
            try:
                payload = DecisionEngine(
                    adapter,
                    bus,
                    [get_strategy("tech_boll_fade_v1")],
                    risk=RiskManager(cooldown_sec=0),
                ).tick()
            finally:
                bus.close()
                adapter.close()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self.assertEqual(payload["emitted"], 1)
            extras = _extras(db)
            self.assertEqual(extras["_action"], "buy")
            self.assertAlmostEqual(extras["_size_pct"], 0.0575)
            self.assertEqual(extras["market_regime"], REGIME_3_CROSS_ASSET_BULL_PULLBACK)
            self.assertEqual(extras["recommended_order_type"], "LIMIT_MAKER")
            self.assertEqual(extras["obi_top5"], -0.10)
            self.assertEqual(extras["obi_top5_source_ts_ms"], now_ms)
            self.assertNotIn("orderbook_imbalance_20", extras)
            self.assertIsInstance(extras["volume_z"], float)
            self.assertLess(extras["volume_z"], 3.8)
            self.assertFalse(extras.get("news_shock_applied"))

    def test_news_defensive_and_sell_passthrough_keep_published_fields(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        published = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            news = root / "news.db"
            conn = sqlite3.connect(news)
            conn.execute(
                """
                CREATE TABLE news_events (
                    id INTEGER PRIMARY KEY,
                    source TEXT, source_tier TEXT, title TEXT, url TEXT,
                    published_at TEXT, fetched_at TEXT, summary TEXT,
                    raw_json TEXT, onchain_status TEXT, notes TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO news_events VALUES (
                    1, 'fed_speeches', 'whitelist', 'inflation remains elevated',
                    'https://example.test/1', ?, ?, '', '{}', 'pending', ''
                )
                """,
                (published, published),
            )
            conn.commit()
            conn.close()
            snapshot = load_news_snapshot(news, now=now, timeout_sec=1.0)
            hot = [{
                "binance_symbol": "ETHUSDT",
                "xs_breadth_1h": 0.20,
                "obi_top5": -0.05,
                "obi_top5_source_ts_ms": 111,
                "volume_z": 1.25,
                "orderbook_imbalance_20": -0.90,
            }]
            routed, _counts = apply_five_state_regime_router(
                [
                    {"action": "buy", "binance_symbol": "ETHUSDT", "token_address": "ETHUSDT", "size_pct": 0.10},
                    {"action": "sell", "binance_symbol": "ETHUSDT", "token_address": "ETHUSDT", "size_pct": 0.10},
                ],
                hot,
                news_snapshot=snapshot,
            )
            self.assertEqual([row["action"] for row in routed], ["buy", "sell"])
            db = root / "signals.db"
            bus = SignalBus(db, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
            try:
                bus.publish_many(routed)
            finally:
                bus.close()
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(
                "SELECT action, size_pct, raw_json FROM decision_log ORDER BY id"
            )]
            conn.close()
            self.assertEqual([row["action"] for row in rows], ["buy", "sell"])
            buy = json.loads(rows[0]["raw_json"])["extras"]
            sell = json.loads(rows[1]["raw_json"])["extras"]
            self.assertEqual(buy["market_regime"], REGIME_4_CROWDED_LONG_DEFENSIVE)
            self.assertEqual(buy["recommended_order_type"], "LIMIT_MAKER")
            self.assertAlmostEqual(rows[0]["size_pct"], 0.055)
            self.assertEqual(buy["obi_top5"], -0.05)
            self.assertEqual(buy["obi_top5_source_ts_ms"], 111)
            self.assertEqual(buy["volume_z"], 1.25)
            self.assertNotIn("orderbook_imbalance_20", buy)
            self.assertEqual(rows[1]["size_pct"], 0.10)
            self.assertNotIn("recommended_order_type", sell)


if __name__ == "__main__":
    unittest.main()
