"""Unit and integration tests for P1 Orderflow Toxicity (Taker Buy Ratio) filter."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import decision_engine  # noqa: F401
from decision_engine.core.binance_indicators import list_binance_hot_tokens
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.live_bnb_entry_coords import attach_live_entry_coordinates
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE, SignalBus
from decision_engine.strategies import get_strategy
from decision_engine.strategies.live_bnb_entry_zone import (
    LARGE_MIN_TAKER_BUY_RATIO,
    MIN_TAKER_BUY_RATIO,
    ZoneBar,
    evaluate_entry_zone,
    resolve_taker_buy_ratio,
    taker_buy_ratio_from_bar,
)
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from indicators.binance_spot import (
    SpotSymbol,
    parse_kline_row,
    persist_klines_and_factors,
    taker_buy_ratio_from_kline,
)

_LOW = 100.0
_SPAN = 10.0
_MID = _LOW + _SPAN


def _price(alpha: float) -> float:
    return _LOW + alpha * _SPAN


def _row(alpha: float = 0.20, ratio: float = 1.6, **overrides: object) -> dict:
    row = {
        "binance_symbol": "BNBUSDT",
        "chain_id": "binance",
        "token_address": "BNBUSDT",
        "kline_low": _LOW,
        "boll_mid": _MID,
        "price_usd": _price(alpha),
        "trade_size_ratio": ratio,
        "change_5m": -3.0,
        "volume_strength": 1.2,
        "boll_lower": _LOW,
        "liquidity_usd": 90_000.0,
    }
    row.update(overrides)
    return row


class TakerBuyRatioOrderflowTests(unittest.TestCase):
    def test_kline_parser_extracts_taker_buy_base_and_quote_volumes(self) -> None:
        raw_12 = [
            1_700_000_000_000,
            "100.0",
            "101.0",
            "96.0",
            "97.0",
            "50.0",
            1_700_000_299_999,
            "5000.0",
            120,
            "26.0",
            "2600.0",
            "0",
        ]
        kline = parse_kline_row(raw_12)
        self.assertIsNotNone(kline)
        assert kline is not None
        self.assertAlmostEqual(kline.taker_buy_base_volume or 0.0, 26.0)
        self.assertAlmostEqual(kline.taker_buy_quote_volume or 0.0, 2600.0)
        self.assertAlmostEqual(taker_buy_ratio_from_kline(kline) or 0.0, 0.52)

    def test_toxic_sell_dump_blocks_large_trade_market_buy(self) -> None:
        """Whale dump (trade_size_ratio=2.2) with taker_buy_ratio=0.18 (< 0.30) is blocked."""
        decision = evaluate_entry_zone(_row(alpha=0.20, ratio=2.2, taker_buy_ratio=0.18))
        self.assertFalse(decision.emit)
        self.assertEqual(decision.reject_reason, "orderflow_toxic_sell_dump")
        self.assertEqual(decision.orderflow_regime, "toxic_sell_dump")
        self.assertAlmostEqual(decision.taker_buy_ratio or 0.0, 0.18)

        strategy = TechBollFadeStrategy()
        signals = strategy.on_tick(
            {
                "hot_tokens": [_row(alpha=0.20, ratio=2.2, taker_buy_ratio=0.18)],
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            }
        )
        self.assertEqual(signals, [])
        self.assertEqual(strategy.rejected.get("orderflow_toxic_sell_dump"), 1)

    def test_weak_absorption_downgrades_large_trade_to_neutral_half_size(self) -> None:
        """Large trade (ratio=1.6) with taker_buy_ratio=0.35 (in [0.30, 0.42)) downgrades to neutral 0.5x."""
        # At alpha=0.20, neutral odds = (110-102)/(102-99) = 8/3 = 2.67 >= 2.5 -> neutral_market (0.5x)
        decision = evaluate_entry_zone(_row(alpha=0.20, ratio=1.6, taker_buy_ratio=0.35))
        self.assertTrue(decision.emit, decision.reject_reason)
        self.assertEqual(decision.tier, "neutral")
        self.assertEqual(decision.entry_reason, "neutral_market")
        self.assertEqual(decision.order_type, "MARKET")
        self.assertEqual(decision.position_size_multiplier, 0.5)
        self.assertEqual(decision.orderflow_regime, "absorption_weak_downgraded")

        # At alpha=0.28, large_trade would normally emit MARKET 1.0x, but downgraded neutral rests LIMIT at L+0.20*S!
        at_028 = evaluate_entry_zone(_row(alpha=0.28, ratio=1.6, taker_buy_ratio=0.35))
        self.assertTrue(at_028.emit, at_028.reject_reason)
        self.assertEqual(at_028.tier, "neutral")
        self.assertEqual(at_028.entry_reason, "neutral_limit")
        self.assertEqual(at_028.order_type, "LIMIT")
        self.assertAlmostEqual(at_028.limit_price or 0.0, _price(0.20))
        self.assertEqual(at_028.position_size_multiplier, 0.5)

    def test_strong_absorption_confirms_full_size_large_trade_market(self) -> None:
        """Large trade (ratio=1.6) with taker_buy_ratio >= 0.42 emits full 1.0x MARKET."""
        decision = evaluate_entry_zone(
            _row(alpha=0.28, ratio=1.6, taker_buy_ratio=LARGE_MIN_TAKER_BUY_RATIO)
        )
        self.assertTrue(decision.emit, decision.reject_reason)
        self.assertEqual(decision.tier, "large_trade")
        self.assertEqual(decision.entry_reason, "large_trade_market")
        self.assertEqual(decision.order_type, "MARKET")
        self.assertEqual(decision.position_size_multiplier, 1.0)
        self.assertEqual(decision.orderflow_regime, "absorption_strong")

        strategy = TechBollFadeStrategy(position_pct=0.2)
        signals = strategy.on_tick(
            {
                "hot_tokens": [_row(alpha=0.20, ratio=1.6, taker_buy_ratio=0.55)],
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            }
        )
        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0]["taker_buy_ratio"], 0.55)
        self.assertEqual(signals[0]["orderflow_regime"], "absorption_strong")
        self.assertEqual(signals[0]["position_size_multiplier"], 1.0)

    def test_resolve_taker_buy_ratio_from_bar_and_require_env_flag(self) -> None:
        bar = ZoneBar(
            open_ms=1_000_000,
            close_ms=1_300_000,
            low=95.0,
            close=96.0,
            quote_volume=10_000.0,
            trades=100.0,
            volume=100.0,
            taker_buy_base_volume=45.0,
            taker_buy_quote_volume=4_500.0,
        )
        tbr, src = taker_buy_ratio_from_bar(bar)
        self.assertAlmostEqual(tbr or 0.0, 0.45)
        self.assertEqual(src, "kline_taker_buy_quote")

        bad = _row(alpha=0.20, ratio=1.6, taker_buy_ratio=1.5)
        self.assertEqual(evaluate_entry_zone(bad).reject_reason, "taker_buy_ratio_invalid")

        prev = os.environ.get("LIVE_BNB_REQUIRE_TAKER_BUY_RATIO")
        os.environ["LIVE_BNB_REQUIRE_TAKER_BUY_RATIO"] = "1"
        try:
            missing = evaluate_entry_zone(_row(alpha=0.20, ratio=1.6))
            self.assertFalse(missing.emit)
            self.assertEqual(missing.reject_reason, "taker_buy_ratio_missing")
        finally:
            if prev is None:
                os.environ.pop("LIVE_BNB_REQUIRE_TAKER_BUY_RATIO", None)
            else:
                os.environ["LIVE_BNB_REQUIRE_TAKER_BUY_RATIO"] = prev

    def test_end_to_end_kline_ingest_and_entry_coords_propagate_taker_buy_ratio(self) -> None:
        start = 1_700_000_000_000
        step = 300_000
        raw_bars = []
        for idx in range(25):
            open_ms = start + idx * step
            is_last = idx == 24
            close = 97.0 if is_last else 100.0
            low = 96.1875 if is_last else 99.0
            trades = 50 if is_last else 100
            # Last bar has 55% taker buy volume (1100 / 2000)
            taker_quote = "1100.0" if is_last else "1000.0"
            raw_bars.append(
                [
                    open_ms,
                    "100.0",
                    "101.0",
                    str(low),
                    str(close),
                    "20.0",
                    open_ms + 299_999,
                    "2000.0",
                    trades,
                    "11.0",
                    taker_quote,
                    "0",
                ]
            )
        parsed = [parse_kline_row(item) for item in raw_bars]
        assert all(item is not None for item in parsed)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            conn = sqlite3.connect(db)
            stored = persist_klines_and_factors(
                conn,
                SpotSymbol("SOLUSDT", "SOL"),
                [item for item in parsed if item is not None],
            )
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertAlmostEqual(stored["taker_buy_ratio"], 0.55)
            hot = list_binance_hot_tokens(conn, limit=5, fresh_sec=3600)
            self.assertEqual(len(hot), 1)
            self.assertAlmostEqual(hot[0]["taker_buy_ratio"], 0.55)
            # Clear taker_buy_ratio on the dict to verify attach_live_entry_coordinates
            # also restores it from binance_klines.
            hot[0].pop("taker_buy_ratio", None)
            attach_live_entry_coordinates(conn, hot, now_ms=start + 30 * step)
            self.assertAlmostEqual(hot[0]["taker_buy_ratio"], 0.55)
            self.assertEqual(hot[0]["taker_buy_ratio_source"], "kline_taker_buy_quote")
            conn.close()


if __name__ == "__main__":
    unittest.main()
