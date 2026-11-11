"""Unit tests for Upgraded Tier-1 Kelly Sizing & State 2/3 OCO Take-Profit Expansion."""

from __future__ import annotations

import os
import unittest

from decision_engine.core.conviction_whitelist import WHITELIST_ENV
from decision_engine.core.regime_strategy_router import (
    EXPAND_OCO_TP_ENV,
    OPTIMAL_KELLY_ENV,
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    apply_five_state_regime_router,
)
from decision_engine.core.signal_bus import normalize_signal


class Tier1KellyAndOcoTpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_kelly = os.environ.get(OPTIMAL_KELLY_ENV)
        self._saved_tp = os.environ.get(EXPAND_OCO_TP_ENV)
        self._saved_wl = os.environ.get(WHITELIST_ENV)
        os.environ.pop(OPTIMAL_KELLY_ENV, None)
        os.environ.pop(EXPAND_OCO_TP_ENV, None)
        os.environ.pop(WHITELIST_ENV, None)

    def tearDown(self) -> None:
        for key, val in (
            (OPTIMAL_KELLY_ENV, self._saved_kelly),
            (EXPAND_OCO_TP_ENV, self._saved_tp),
            (WHITELIST_ENV, self._saved_wl),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_env_switch_activates_one_shot_multi_symbol_kelly_sizing(self) -> None:
        """USE_OPTIMAL_KELLY_MATRIX=1 sizes 2 concurrent whitelist leaders to 45%+45%=90% in State 2."""
        os.environ[OPTIMAL_KELLY_ENV] = "1"
        os.environ[WHITELIST_ENV] = "1"
        candidates = [
            {
                "action": "buy",
                "binance_symbol": "SOLUSDT",
                "size_pct": 0.05,
                "price_usd": 200.0,
                "stop_price": 197.5,
                "take_profit_price": 203.0,
            },
            {
                "action": "buy",
                "binance_symbol": "ETHUSDT",
                "size_pct": 0.05,
                "price_usd": 3000.0,
                "stop_price": 2960.0,
                "take_profit_price": 3045.0,
            },
            {
                "action": "buy",
                "binance_symbol": "PEPEUSDT",
                "size_pct": 0.05,
                "price_usd": 0.0100,
                "stop_price": 0.0098,
                "take_profit_price": 0.0102,
            },
        ]
        hot = [
            {"binance_symbol": "SOLUSDT", "funding_z": -2.1, "low": 198.0, "span": 5.0},
            {"binance_symbol": "ETHUSDT", "funding_z": -2.0, "low": 2970.0, "span": 75.0},
            {"binance_symbol": "PEPEUSDT", "funding_z": -2.2},
        ]
        out, _ = apply_five_state_regime_router(candidates, hot)
        by_sym = {r["binance_symbol"]: r for r in out}

        self.assertEqual(by_sym["SOLUSDT"]["market_regime"], REGIME_2_SHORT_SQUEEZE_BREAKOUT)
        self.assertAlmostEqual(by_sym["SOLUSDT"]["size_pct"], 0.45, places=6)
        self.assertAlmostEqual(by_sym["ETHUSDT"]["size_pct"], 0.45, places=6)
        # Top-2 concurrent leaders combine to 90% spot exposure with zero same-symbol re-buys
        self.assertAlmostEqual(by_sym["SOLUSDT"]["size_pct"] + by_sym["ETHUSDT"]["size_pct"], 0.90, places=6)
        # Speculative token stays capped at 15% on one-shot entry
        self.assertAlmostEqual(by_sym["PEPEUSDT"]["size_pct"], 0.15, places=6)

        # State 2 OCO take_profit_price expands past 5m boll_mid (203.0 -> 203.0 + 1.2 * 5.0 = 209.0)
        self.assertEqual(by_sym["SOLUSDT"]["take_profit_price_before_regime"], 203.0)
        self.assertTrue(by_sym["SOLUSDT"]["regime_tp_expanded"])
        self.assertAlmostEqual(by_sym["SOLUSDT"]["take_profit_price"], 209.0, places=6)

        # SignalBus preserves expanded take_profit_price in _extras for binance_spot OCO execution
        norm = normalize_signal(by_sym["SOLUSDT"], purpose="live_bnb_boll_fade")
        self.assertAlmostEqual(norm["size_pct"], 0.45, places=6)
        self.assertAlmostEqual(norm["_extras"]["take_profit_price"], 209.0, places=6)

    def test_state1_keeps_boll_mid_tp_and_20pct_base_while_state3_expands_to_upper_band(self) -> None:
        os.environ[OPTIMAL_KELLY_ENV] = "1"
        # State 1 calm mean-reversion: 20% size, boll_mid TP untouched
        s1_out, _ = apply_five_state_regime_router(
            [{
                "action": "buy",
                "binance_symbol": "BNBUSDT",
                "size_pct": 0.05,
                "price_usd": 600.0,
                "stop_price": 594.0,
                "take_profit_price": 606.0,
            }],
            [{"binance_symbol": "BNBUSDT", "span": 8.0}],
        )
        self.assertEqual(s1_out[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertAlmostEqual(s1_out[0]["size_pct"], 0.20, places=6)
        self.assertEqual(s1_out[0]["take_profit_price"], 606.0)
        self.assertNotIn("regime_tp_expanded", s1_out[0])

        # State 3 AI/Bull trend leader pullback: 35% size (2 leaders = 70%), TP expands to boll_upper
        s3_out, _ = apply_five_state_regime_router(
            [{
                "action": "buy",
                "binance_symbol": "AAVEUSDT",
                "size_pct": 0.05,
                "price_usd": 150.0,
                "stop_price": 147.5,
                "take_profit_price": "152.0",
            }],
            [{
                "binance_symbol": "AAVEUSDT",
                "nvda_1h_ret_pct": 1.65,
                "xs_rank_pct_1h": 0.85,
                "obi_top5": 0.22,
                "volume_z": 1.4,
                "boll_upper": 156.5,
            }],
        )
        self.assertEqual(s3_out[0]["market_regime"], REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        self.assertAlmostEqual(s3_out[0]["size_pct"], 0.35, places=6)
        self.assertTrue(s3_out[0]["regime_tp_expanded"])
        self.assertEqual(s3_out[0]["take_profit_price_before_regime"], "152.0")
        # String format preserved cleanly for Decimal parser in binance_spot
        self.assertEqual(s3_out[0]["take_profit_price"], "156.5")

        # State 4 defensive mode: 5% base size, TP not expanded upward
        s4_out, _ = apply_five_state_regime_router(
            [{
                "action": "buy",
                "binance_symbol": "BTCUSDT",
                "size_pct": 0.10,
                "price_usd": 90000.0,
                "stop_price": 89000.0,
                "take_profit_price": 90800.0,
            }],
            [{"binance_symbol": "BTCUSDT", "funding_z": 2.1}],
        )
        self.assertEqual(s4_out[0]["market_regime"], REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertAlmostEqual(s4_out[0]["size_pct"], 0.05, places=6)
        self.assertEqual(s4_out[0]["take_profit_price"], 90800.0)

    def test_default_off_leaves_legacy_sizing_and_tp_untouched(self) -> None:
        """When USE_OPTIMAL_KELLY_MATRIX is unset, existing contract behavior is 100% preserved."""
        out, _ = apply_five_state_regime_router(
            [{
                "action": "buy",
                "binance_symbol": "SOLUSDT",
                "size_pct": 0.05,
                "price_usd": 200.0,
                "stop_price": 197.5,
                "take_profit_price": 203.0,
            }],
            [{"binance_symbol": "SOLUSDT", "funding_z": -2.1, "span": 5.0}],
        )
        self.assertAlmostEqual(out[0]["size_pct"], 0.06, places=6)  # 0.05 * 1.20
        self.assertEqual(out[0]["take_profit_price"], 203.0)
        self.assertNotIn("regime_tp_expanded", out[0])


if __name__ == "__main__":
    unittest.main()
