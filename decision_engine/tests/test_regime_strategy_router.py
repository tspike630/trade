"""Unit tests for the 5-State Market Regime Strategy Router (`regime_strategy_router.py`)."""

from __future__ import annotations

import unittest

from decision_engine.core.regime_strategy_router import (
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    REGIME_5_CRISIS_LIQUIDATION_HALT,
    apply_five_state_regime_router,
    evaluate_five_state_regime,
)


class TestFiveStateRegimeRouter(unittest.TestCase):
    def test_state_1_positive_gamma_pinned_standard_mean_reversion(self) -> None:
        dec = evaluate_five_state_regime(
            {
                "symbol": "SOLUSDT",
                "gamma_regime": "positive_gamma_pinned",
                "funding_z": -0.35,
                "xs_breadth_1h": 0.48,
            }
        )
        self.assertEqual(dec.regime, REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertTrue(dec.allow_buy)
        self.assertAlmostEqual(dec.size_multiplier, 1.00, places=2)
        self.assertAlmostEqual(dec.tp_multiplier, 1.00, places=2)
        self.assertFalse(dec.enable_trailing_ratchet)

    def test_state_2_short_squeeze_breakout_boosts_size_and_enables_ratchet(self) -> None:
        dec = evaluate_five_state_regime(
            {
                "symbol": "ETHUSDT",
                "funding_z": -2.45,
                "whale_retail_divergence": 1.65,
                "cot_regime": "cot_institutional_squeeze_favorable",
            }
        )
        self.assertEqual(dec.regime, REGIME_2_SHORT_SQUEEZE_BREAKOUT)
        self.assertTrue(dec.allow_buy)
        self.assertAlmostEqual(dec.size_multiplier, 1.20, places=2)
        self.assertAlmostEqual(dec.tp_multiplier, 1.60, places=2)
        self.assertTrue(dec.enable_trailing_ratchet)

    def test_state_3_cross_asset_bull_pullback_keeps_leaders_and_skips_laggards(self) -> None:
        # Leader (top 15%, xs_rank_pct_1h = 0.88) during NVDA +1.6% breakout -> accepted with 1.15x
        dec_leader = evaluate_five_state_regime(
            {
                "symbol": "FETUSDT",
                "nvda_1h_ret_pct": 1.60,
                "xs_breadth_1h": 0.75,
                "xs_rank_pct_1h": 0.88,
                "funding_z": 0.50,
            }
        )
        self.assertEqual(dec_leader.regime, REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        self.assertTrue(dec_leader.allow_buy)
        self.assertAlmostEqual(dec_leader.size_multiplier, 1.15, places=2)
        self.assertTrue(dec_leader.enable_trailing_ratchet)

        # Laggard (bottom 20%, xs_rank_pct_1h = 0.20) in bull trend -> skipped
        dec_laggard = evaluate_five_state_regime(
            {
                "symbol": "WEAKUSDT",
                "nvda_1h_ret_pct": 1.60,
                "xs_breadth_1h": 0.75,
                "xs_rank_pct_1h": 0.20,
                "funding_z": 0.50,
            }
        )
        self.assertEqual(dec_laggard.regime, REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        self.assertFalse(dec_laggard.allow_buy)
        self.assertEqual(dec_laggard.reject_reason, "regime3_bull_trend_laggard_skip")

    def test_state_4_crowded_long_defensive_cuts_size_and_requires_beta_hedge(self) -> None:
        dec = evaluate_five_state_regime(
            {
                "symbol": "BTCUSDT",
                "cot_regime": "cot_leveraged_long_crowded",
                "funding_z": 1.95,
            }
        )
        self.assertEqual(dec.regime, REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertTrue(dec.allow_buy)
        self.assertAlmostEqual(dec.size_multiplier, 0.55, places=2)
        self.assertAlmostEqual(dec.tp_multiplier, 0.80, places=2)
        self.assertTrue(dec.require_perp_beta_hedge)

    def test_state_5_crisis_liquidation_halt_blocks_buys_but_keeps_sells(self) -> None:
        candidates = [
            {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.05},
            {"action": "sell", "binance_symbol": "SOLUSDT"},
        ]
        hot = [
            {
                "binance_symbol": "SOLUSDT",
                "liq_cascade_regime": "long_liquidation_waterfall",
            }
        ]
        out, counts = apply_five_state_regime_router(candidates, hot)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "sell")
        self.assertEqual(counts[REGIME_5_CRISIS_LIQUIDATION_HALT], 1)


if __name__ == "__main__":
    unittest.main()
