"""Self-built momentum candidate A: admit / R3 reject / missing-field reject."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from decision_engine.strategies import get_strategy, list_strategies
from decision_engine.strategies.selfbuilt_momentum_a import (
    RULE_VERSION,
    STRATEGY_ID,
    SelfbuiltMomentumAStrategy,
    evaluate_candidate_a,
)

NOW = datetime(2026, 10, 25, 2, 0, 0, tzinfo=timezone.utc)
DAY_MS = 86_400_000
CREATED_OLD = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _row(**overrides: object) -> dict:
    base = {
        "chain_id": "solana",
        "token_address": "SelfBuiltToken111",
        "primary_pair_address": "SelfBuiltPair111",
        "liquidity_usd": 80_000.0,
        "pair_created_at": CREATED_OLD,
        "volatility_24h": 1.0,
        "change_1h": 0.5,
        "rsi_14": 50.0,
    }
    base.update(overrides)
    return base


class EvaluateCandidateATests(unittest.TestCase):
    def test_admits_default_arm(self) -> None:
        ok, why, extras = evaluate_candidate_a(_row(), now=NOW)
        self.assertTrue(ok)
        self.assertIsNone(why)
        self.assertEqual(extras["entry_r3"], 0)
        self.assertEqual(extras["rule_version"], RULE_VERSION)

    def test_rsi_band_edges(self) -> None:
        ok40, _, _ = evaluate_candidate_a(_row(rsi_14=40.0), now=NOW)
        self.assertTrue(ok40)
        ok64, _, _ = evaluate_candidate_a(_row(rsi_14=64.999), now=NOW)
        self.assertTrue(ok64)
        ok65, why65, _ = evaluate_candidate_a(_row(rsi_14=65.0), now=NOW)
        self.assertFalse(ok65)
        self.assertEqual(why65, "rsi_out_of_band")
        ok39, why39, _ = evaluate_candidate_a(_row(rsi_14=39.999), now=NOW)
        self.assertFalse(ok39)
        self.assertEqual(why39, "rsi_out_of_band")

    def test_change_1h_must_be_strictly_positive(self) -> None:
        ok0, why0, _ = evaluate_candidate_a(_row(change_1h=0.0), now=NOW)
        self.assertFalse(ok0)
        self.assertEqual(why0, "change_1h_non_positive")
        ok_neg, why_neg, _ = evaluate_candidate_a(_row(change_1h=-0.01), now=NOW)
        self.assertFalse(ok_neg)
        self.assertEqual(why_neg, "change_1h_non_positive")

    def test_r3_rejects(self) -> None:
        # vol>2.5 and change_1h<0
        ok, why, extras = evaluate_candidate_a(
            _row(volatility_24h=2.6, change_1h=-0.1, rsi_14=50.0),
            now=NOW,
        )
        self.assertFalse(ok)
        self.assertEqual(why, "r3")
        self.assertEqual(extras["entry_r3"], 1)
        # vol>2.5 and rsi extreme high (also outside M1 band, but R3 checked first
        # after momentum completeness — rsi=75 is complete, R3 fires)
        ok2, why2, _ = evaluate_candidate_a(
            _row(volatility_24h=3.0, change_1h=0.5, rsi_14=75.0),
            now=NOW,
        )
        self.assertFalse(ok2)
        self.assertEqual(why2, "r3")
        # vol>2.5 and rsi < 30
        ok3, why3, _ = evaluate_candidate_a(
            _row(volatility_24h=3.0, change_1h=0.5, rsi_14=25.0),
            now=NOW,
        )
        self.assertFalse(ok3)
        self.assertEqual(why3, "r3")

    def test_vol_missing_rejects(self) -> None:
        ok, why, _ = evaluate_candidate_a(_row(volatility_24h=None), now=NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "vol_missing")

    def test_liq_missing_or_below(self) -> None:
        ok_m, why_m, _ = evaluate_candidate_a(_row(liquidity_usd=None), now=NOW)
        self.assertFalse(ok_m)
        self.assertEqual(why_m, "liq_missing")
        ok_b, why_b, _ = evaluate_candidate_a(_row(liquidity_usd=49_999.0), now=NOW)
        self.assertFalse(ok_b)
        self.assertEqual(why_b, "liq_below")

    def test_pool_age_missing_or_below(self) -> None:
        ok_m, why_m, _ = evaluate_candidate_a(
            {"liquidity_usd": 80_000.0, "volatility_24h": 1.0, "change_1h": 0.5, "rsi_14": 50.0},
            now=NOW,
        )
        self.assertFalse(ok_m)
        self.assertEqual(why_m, "pool_age_missing")
        young_ms = int((NOW.timestamp() * 1000) - 0.5 * DAY_MS)
        ok_b, why_b, _ = evaluate_candidate_a(
            _row(pair_created_at=young_ms),
            now=NOW,
        )
        self.assertFalse(ok_b)
        self.assertEqual(why_b, "pool_age_below")

    def test_momentum_incomplete(self) -> None:
        ok, why, _ = evaluate_candidate_a(_row(change_1h=None), now=NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "momentum_incomplete")
        ok2, why2, _ = evaluate_candidate_a(_row(rsi_14=None), now=NOW)
        self.assertFalse(ok2)
        self.assertEqual(why2, "momentum_incomplete")


class StrategyOnTickTests(unittest.TestCase):
    def test_registry(self) -> None:
        self.assertIn(STRATEGY_ID, list_strategies())
        s = get_strategy(STRATEGY_ID)
        self.assertIsInstance(s, SelfbuiltMomentumAStrategy)
        self.assertEqual(s.strategy_id, STRATEGY_ID)
        alias = get_strategy("selfbuilt_a")
        self.assertEqual(alias.strategy_id, STRATEGY_ID)

    def test_on_tick_emits_buy_with_rule_version(self) -> None:
        strat = SelfbuiltMomentumAStrategy()
        signals = strat.on_tick({"hot_tokens": [_row(), _row(token_address="Bad", liquidity_usd=1000.0)]})
        self.assertEqual(len(signals), 1)
        sig = signals[0]
        self.assertEqual(sig["strategy_id"], STRATEGY_ID)
        self.assertEqual(sig["action"], "buy")
        self.assertEqual(sig["rule_version"], RULE_VERSION)
        self.assertEqual(sig["entry_r3"], 0)
        self.assertEqual(sig["selfbuilt_sidebook"], "momentum_a")
        self.assertEqual(strat.rejected.get("liq_below"), 1)

    def test_on_tick_r3_counted(self) -> None:
        strat = SelfbuiltMomentumAStrategy()
        signals = strat.on_tick(
            {
                "hot_tokens": [
                    _row(volatility_24h=3.0, change_1h=-0.2, rsi_14=50.0),
                ]
            }
        )
        self.assertEqual(signals, [])
        self.assertEqual(strat.rejected.get("r3"), 1)


if __name__ == "__main__":
    unittest.main()
