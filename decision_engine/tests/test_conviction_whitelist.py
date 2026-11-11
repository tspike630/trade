import os
import unittest

from decision_engine.core.conviction_whitelist import (
    REAL_DEMAND_TOP12_WHITELIST,
    WHITELIST_ENV,
    get_conviction_tier,
)
from decision_engine.core.regime_strategy_router import apply_five_state_regime_router


class ConvictionWhitelistTests(unittest.TestCase):
    def test_tiers_cover_top_real_demand_pillars_and_apps(self) -> None:
        self.assertEqual(get_conviction_tier("BTCUSDT"), "S")
        self.assertEqual(get_conviction_tier("TRX"), "S")
        self.assertEqual(get_conviction_tier("AAVEUSDT"), "A")
        self.assertEqual(get_conviction_tier("PEPEUSDT"), "SPECULATIVE")
        self.assertGreaterEqual(len(REAL_DEMAND_TOP12_WHITELIST), 12)

    def test_enforced_whitelist_blocks_speculative_dip_buy_in_state_1(self) -> None:
        old = os.environ.get(WHITELIST_ENV)
        os.environ[WHITELIST_ENV] = "1"
        try:
            candidates = [
                {"action": "buy", "binance_symbol": "AAVEUSDT", "size_pct": 0.10},
                {"action": "buy", "binance_symbol": "MEMECOINUSDT", "size_pct": 0.10},
                {"action": "sell", "binance_symbol": "MEMECOINUSDT"},
            ]
            hot = [
                {"binance_symbol": "AAVEUSDT"},
                {"binance_symbol": "MEMECOINUSDT"},
            ]
            out, _ = apply_five_state_regime_router(candidates, hot)
            kept_buys = [r["binance_symbol"] for r in out if r["action"] == "buy"]
            self.assertEqual(kept_buys, ["AAVEUSDT"])
            self.assertEqual(out[0]["conviction_tier"], "A")
            # Sells are never blocked
            self.assertEqual(len([r for r in out if r["action"] == "sell"]), 1)
        finally:
            if old is None:
                os.environ.pop(WHITELIST_ENV, None)
            else:
                os.environ[WHITELIST_ENV] = old



    def test_speculative_tokens_trade_state2_state3_with_35pct_fast_blade_cap(self) -> None:
        old = os.environ.get(WHITELIST_ENV)
        os.environ[WHITELIST_ENV] = "1"
        try:
            # State 2 Short Squeeze on PEPEUSDT (SPECULATIVE) -> ALLOWED, sized 15% -> 25% -> 35% cap
            candidates = [
                {"action": "buy", "binance_symbol": "PEPEUSDT", "size_pct": 0.10},
                {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10},
            ]
            hot = [
                {
                    "binance_symbol": "PEPEUSDT",
                    "funding_z": -2.2,
                    "use_optimal_kelly_matrix": True,
                    "unrealized_gain_pct": 2.5,
                    "oco_stop_above_breakeven": True,
                    "oco_stop_locked_stage2_profit": True,
                },
                {
                    "binance_symbol": "SOLUSDT",
                    "funding_z": -2.2,
                    "use_optimal_kelly_matrix": True,
                    "unrealized_gain_pct": 2.5,
                    "oco_stop_above_breakeven": True,
                    "oco_stop_locked_stage2_profit": True,
                },
            ]
            out, _ = apply_five_state_regime_router(candidates, hot)
            by_s = {r["binance_symbol"]: r for r in out}
            self.assertEqual(by_s["PEPEUSDT"]["conviction_tier"], "SPECULATIVE")
            self.assertAlmostEqual(by_s["PEPEUSDT"]["optimal_base_position_pct"], 0.15, places=3)
            self.assertAlmostEqual(by_s["PEPEUSDT"]["size_pct"], 0.35, places=3)
            # Hard asset SOLUSDT gets full 95% Stage-3 squeeze pyramid
            self.assertEqual(by_s["SOLUSDT"]["conviction_tier"], "S")
            self.assertAlmostEqual(by_s["SOLUSDT"]["size_pct"], 0.95, places=3)
        finally:
            if old is None:
                os.environ.pop(WHITELIST_ENV, None)
            else:
                os.environ[WHITELIST_ENV] = old


if __name__ == "__main__":
    unittest.main()
