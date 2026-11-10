"""Unit tests for the 5-State Market Regime Strategy Router (`regime_strategy_router.py`)."""

from __future__ import annotations

import unittest

from decision_engine.core.regime_strategy_router import (
    compute_optimal_ratchet_target_pct,
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



    def test_optimal_kelly_three_stage_ratchet_pyramid(self) -> None:
        # State 1: 18.5% fixed Kelly base, no pyramiding
        s1 = compute_optimal_ratchet_target_pct(1, unrealized_gain_pct=2.5, oco_stop_above_breakeven=True, oco_stop_locked_stage2_profit=True)
        self.assertEqual(s1["pyramid_stage"], 1)
        self.assertAlmostEqual(s1["target_cumulative_pct"], 0.185, places=3)

        # State 2: 35% -> 70% -> 95% as OCO ratchet locks break-even and stage-2 profit
        s2_stage1 = compute_optimal_ratchet_target_pct(2, unrealized_gain_pct=0.5, oco_stop_above_breakeven=False)
        self.assertEqual(s2_stage1["pyramid_stage"], 1)
        self.assertAlmostEqual(s2_stage1["target_cumulative_pct"], 0.350, places=3)

        s2_stage2 = compute_optimal_ratchet_target_pct(2, unrealized_gain_pct=1.35, oco_stop_above_breakeven=True)
        self.assertEqual(s2_stage2["pyramid_stage"], 2)
        self.assertAlmostEqual(s2_stage2["target_cumulative_pct"], 0.700, places=3)

        s2_stage3 = compute_optimal_ratchet_target_pct(2, unrealized_gain_pct=2.40, oco_stop_above_breakeven=True, oco_stop_locked_stage2_profit=True)
        self.assertEqual(s2_stage3["pyramid_stage"], 3)
        self.assertAlmostEqual(s2_stage3["target_cumulative_pct"], 0.950, places=3)

        # State 3: 30% -> 60% -> 85%
        s3_stage3 = compute_optimal_ratchet_target_pct(3, unrealized_gain_pct=2.50, oco_stop_above_breakeven=True, oco_stop_locked_stage2_profit=True)
        self.assertEqual(s3_stage3["pyramid_stage"], 3)
        self.assertAlmostEqual(s3_stage3["target_cumulative_pct"], 0.850, places=3)



    def test_state_3_anti_mug_kill_book_and_volume_checks(self) -> None:
        # Thin L2 bid book + weak taker buy -> rejected (看封板质量防炸板闷杀)
        dec_thin = evaluate_five_state_regime(
            {
                "symbol": "SOLUSDT",
                "nvda_1h_ret_pct": 1.60,
                "xs_breadth_1h": 0.75,
                "xs_rank_pct_1h": 0.85,
                "obi_top5": -0.32,
                "taker_buy_ratio": 0.41,
            }
        )
        self.assertFalse(dec_thin.allow_buy)
        self.assertEqual(dec_thin.reject_reason, "regime3_thin_bid_book_trap")

        # Climax volume blow-off + whale distribution -> rejected (看换手量能防巨量出货闷杀)
        dec_climax = evaluate_five_state_regime(
            {
                "symbol": "SOLUSDT",
                "nvda_1h_ret_pct": 1.60,
                "xs_breadth_1h": 0.75,
                "xs_rank_pct_1h": 0.85,
                "volume_z": 4.2,
                "whale_retail_divergence": 0.55,
            }
        )
        self.assertFalse(dec_climax.allow_buy)
        self.assertEqual(dec_climax.reject_reason, "regime3_climax_volume_distribution_trap")

        # Manual thresholds do not also require a weak taker or a low whale ratio.
        dec_obi_eq = evaluate_five_state_regime(
            {
                "symbol": "SOLUSDT",
                "nvda_1h_ret_pct": 1.60,
                "xs_breadth_1h": 0.75,
                "xs_rank_pct_1h": 0.85,
                "obi_top5": -0.25,
                "taker_buy_ratio": 0.80,
            }
        )
        self.assertFalse(dec_obi_eq.allow_buy)
        self.assertEqual(dec_obi_eq.reject_reason, "regime3_thin_bid_book_trap")
        dec_obi_above = evaluate_five_state_regime(
            {
                "symbol": "SOLUSDT",
                "nvda_1h_ret_pct": 1.60,
                "xs_breadth_1h": 0.75,
                "xs_rank_pct_1h": 0.85,
                "obi_top5": -0.24,
                "taker_buy_ratio": 0.20,
            }
        )
        self.assertTrue(dec_obi_above.allow_buy)

        dec_vol_eq = evaluate_five_state_regime(
            {
                "symbol": "SOLUSDT",
                "nvda_1h_ret_pct": 1.60,
                "xs_breadth_1h": 0.75,
                "xs_rank_pct_1h": 0.85,
                "volume_z": 3.8,
                "whale_retail_divergence": 1.50,
            }
        )
        self.assertFalse(dec_vol_eq.allow_buy)
        self.assertEqual(dec_vol_eq.reject_reason, "regime3_climax_volume_distribution_trap")
        dec_vol_below = evaluate_five_state_regime(
            {
                "symbol": "SOLUSDT",
                "nvda_1h_ret_pct": 1.60,
                "xs_breadth_1h": 0.75,
                "xs_rank_pct_1h": 0.85,
                "volume_z": 3.79,
                "whale_retail_divergence": 0.40,
            }
        )
        self.assertTrue(dec_vol_below.allow_buy)


if __name__ == "__main__":
    unittest.main()
