"""Smoke: portfolio_risk_v1 + portfolio_tight_sl_v1 (+ and_blend side)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from decision_engine.core.signal_bus import PURPOSES
from decision_engine.strategies import STRATEGY_REGISTRY, get_strategy
from decision_engine.strategies.portfolio_and_blend import (
    PortfolioAndBlendStrategy,
    SIZE_SLEEVE,
)
from decision_engine.strategies.portfolio_risk_sleeves import (
    PortfolioRiskSleevesStrategy,
    SIZE_A,
    SIZE_B,
    SIZE_C,
    SIZE_MULT_ASSUMED,
)
from decision_engine.strategies.portfolio_tight_sl import (
    PortfolioTightSlStrategy,
    TIGHT_SL_PCT,
    TIGHT_TRAIL_PCT,
)


class TestPortfolioSleeves(unittest.TestCase):
    def test_registry_and_purposes(self):
        for sid in (
            "portfolio_risk_v1",
            "portfolio_tight_sl_v1",
            "portfolio_and_blend_v1",
        ):
            self.assertIn(sid, STRATEGY_REGISTRY)
            get_strategy(sid)
        for purpose in (
            "race_portfolio_risk",
            "race_portfolio_tight_sl",
            "race_portfolio_risk_sleeves",  # legacy kept
            "race_portfolio_and_blend",
        ):
            self.assertIn(purpose, PURPOSES)

        self.assertEqual(get_strategy("portfolio_risk_v1").strategy_id, "portfolio_risk_v1")
        self.assertEqual(
            get_strategy("portfolio_risk_sleeves_v1").strategy_id, "portfolio_risk_v1"
        )
        self.assertEqual(
            get_strategy("port_top3_equal_v1").strategy_id, "portfolio_risk_v1"
        )
        self.assertEqual(get_strategy("sleeve_top_v1").strategy_id, "portfolio_risk_v1")
        self.assertEqual(
            get_strategy("race_portfolio_risk").strategy_id, "portfolio_risk_v1"
        )
        self.assertEqual(
            get_strategy("sleeve_tight_sl_v1").strategy_id, "portfolio_tight_sl_v1"
        )
        self.assertEqual(
            get_strategy("race_portfolio_tight_sl").strategy_id, "portfolio_tight_sl_v1"
        )
        self.assertEqual(
            get_strategy("race_portfolio_and_blend").strategy_id,
            "portfolio_and_blend_v1",
        )

    def test_size_mult_7_scale(self):
        self.assertEqual(SIZE_MULT_ASSUMED, 7.0)
        self.assertAlmostEqual(SIZE_A, 0.35 / 7.0)
        self.assertAlmostEqual(SIZE_B, 0.25 / 7.0)
        self.assertAlmostEqual(SIZE_C, 0.10 / 7.0)
        self.assertAlmostEqual(SIZE_A, 0.05)
        self.assertAlmostEqual(SIZE_B, 0.0357142857, places=9)
        self.assertAlmostEqual(SIZE_C, 0.0142857143, places=9)

    def test_missing_fields_skip(self):
        s = PortfolioRiskSleevesStrategy()
        signals = s.on_tick({"hot_tokens": [{}]})
        self.assertEqual(signals, [])
        self.assertTrue(s.rejected)

        s2 = PortfolioAndBlendStrategy()
        signals2 = s2.on_tick({"hot_tokens": [{}]})
        self.assertEqual(signals2, [])
        self.assertTrue(s2.rejected)

        s3 = PortfolioTightSlStrategy()
        signals3 = s3.on_tick({"hot_tokens": [{}]})
        self.assertEqual(signals3, [])
        self.assertTrue(s3.rejected)

    def test_and_blend_no_or_fake(self):
        """Vol climax alone must NOT emit from and_blend (true AND required)."""
        row = {
            "token_address": "TokAND1",
            "chain_id": "solana",
            "volume_1h": 3000.0,
            "volume_h24": 1000.0,
            "market_cap": 5_000_000.0,
            "txns_h1_buys": 70.0,
            "txns_h1_sells": 30.0,
        }
        s = PortfolioAndBlendStrategy()
        signals = s.on_tick({"hot_tokens": [row]})
        self.assertEqual(signals, [])

    def test_token_mutex_and_sleeve_sizes(self):
        swing_tok = {
            "token_address": "TokSwing",
            "chain_id": "solana",
            "liquidity_usd": 250_000.0,
            "pool_age_days": 14.0,
            "rsi_14": 45.0,
            "change_24h": -8.0,
            "change_5m": 2.0,
            "change_1h": 1.5,
            "volatility_24h": 0.8,
        }
        climax_tok = {
            "token_address": "TokClimax",
            "chain_id": "solana",
            "volume_1h": 5000.0,
            "volume_h24": 2000.0,
            "market_cap": 8_000_000.0,
            "txns_h1_buys": 80.0,
            "txns_h1_sells": 20.0,
        }
        selfbuilt_tok = {
            "token_address": "TokSelf",
            "chain_id": "solana",
            "liquidity_usd": 200_000.0,
            "pool_age_days": 10.0,
            "rsi_14": 50.0,
            "change_1h": 3.0,
            "volatility_24h": 1.0,
        }
        s = PortfolioRiskSleevesStrategy()
        signals = s.on_tick(
            {"hot_tokens": [climax_tok, swing_tok, selfbuilt_tok]}
        )
        sizes = {sig["token_address"]: sig["size_pct"] for sig in signals}
        sleeves = {sig["token_address"]: sig["portfolio_sleeve"] for sig in signals}
        self.assertLessEqual(len(signals), 3)
        self.assertEqual(len(sizes), len(signals))
        if "TokClimax" in sizes:
            self.assertAlmostEqual(sizes["TokClimax"], SIZE_A)
            self.assertEqual(sleeves["TokClimax"], "A_vol_climax")
        for sig in signals:
            self.assertNotIn("sl_pct", sig)  # parent uses peer defaults

        dual = dict(swing_tok)
        dual["token_address"] = "TokDual"
        s2 = PortfolioRiskSleevesStrategy()
        sig2 = s2.on_tick({"hot_tokens": [dual]})
        toks = [x["token_address"] for x in sig2]
        self.assertEqual(toks.count("TokDual"), 1)
        self.assertEqual(sig2[0]["portfolio_sleeve"], "B_swing")
        self.assertAlmostEqual(sig2[0]["size_pct"], SIZE_B)

    def test_tight_sl_exit_meta_and_same_sizes(self):
        swing_tok = {
            "token_address": "TokSwingT",
            "chain_id": "solana",
            "liquidity_usd": 250_000.0,
            "pool_age_days": 14.0,
            "rsi_14": 45.0,
            "change_24h": -8.0,
            "change_5m": 2.0,
            "change_1h": 1.5,
            "volatility_24h": 0.8,
        }
        climax_tok = {
            "token_address": "TokClimaxT",
            "chain_id": "solana",
            "volume_1h": 5000.0,
            "volume_h24": 2000.0,
            "market_cap": 8_000_000.0,
            "txns_h1_buys": 80.0,
            "txns_h1_sells": 20.0,
        }
        selfbuilt_tok = {
            "token_address": "TokSelfT",
            "chain_id": "solana",
            "liquidity_usd": 200_000.0,
            "pool_age_days": 10.0,
            "rsi_14": 50.0,
            "change_1h": 3.0,
            "volatility_24h": 1.0,
        }
        s = PortfolioTightSlStrategy()
        signals = s.on_tick(
            {"hot_tokens": [climax_tok, swing_tok, selfbuilt_tok]}
        )
        self.assertGreaterEqual(len(signals), 1)
        for sig in signals:
            self.assertEqual(sig["strategy_id"], "portfolio_tight_sl_v1")
            self.assertAlmostEqual(sig["sl_pct"], TIGHT_SL_PCT)
            self.assertAlmostEqual(sig["trail_pct"], TIGHT_TRAIL_PCT)
            self.assertEqual(sig["exit_profile"], "tight_sl")
            self.assertIn(sig["size_pct"], (SIZE_A, SIZE_B, SIZE_C))
            # sizes must match MULT=7 scale
            if sig["portfolio_sleeve"] == "A_vol_climax":
                self.assertAlmostEqual(sig["size_pct"], SIZE_A)
            elif sig["portfolio_sleeve"] == "B_swing":
                self.assertAlmostEqual(sig["size_pct"], SIZE_B)
            elif sig["portfolio_sleeve"] == "C_selfbuilt":
                self.assertAlmostEqual(sig["size_pct"], SIZE_C)

    def test_and_blend_sleeve_size(self):
        row = {
            "token_address": "TokBlend",
            "chain_id": "solana",
            "liquidity_usd": 250_000.0,
            "pool_age_days": 14.0,
            "rsi_14": 45.0,
            "change_24h": -5.0,
            "change_5m": 1.0,
            "change_1h": 2.0,
            "volatility_24h": 0.9,
        }
        s = PortfolioAndBlendStrategy()
        signals = s.on_tick({"hot_tokens": [row]})
        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0]["size_pct"], SIZE_SLEEVE)
        self.assertEqual(signals[0]["portfolio_sleeve"], "and_swing_selfbuilt")


if __name__ == "__main__":
    unittest.main()
