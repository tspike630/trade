"""Smoke: synth top-arm AND strategies import + missing-field skip (no OR)."""

from __future__ import annotations

import unittest

from decision_engine.core.signal_bus import PURPOSES
from decision_engine.strategies import STRATEGY_REGISTRY, get_strategy
from decision_engine.strategies.and_vol_climax_swing import evaluate_and_vol_climax_swing
from decision_engine.strategies.and_vol_climax_selfbuilt import (
    evaluate_and_vol_climax_selfbuilt,
)
from decision_engine.strategies.and_swing_selfbuilt import evaluate_and_swing_selfbuilt


class TestSynthTopArms(unittest.TestCase):
    def test_registry_and_purposes(self):
        for sid in (
            "and_vol_climax_swing_v1",
            "and_vol_climax_selfbuilt_v1",
            "and_swing_selfbuilt_v1",
        ):
            self.assertIn(sid, STRATEGY_REGISTRY)
            get_strategy(sid)
        for purpose in (
            "race_and_vol_climax_swing",
            "race_and_vol_climax_selfbuilt",
            "race_and_swing_selfbuilt",
        ):
            self.assertIn(purpose, PURPOSES)

    def test_and_vol_climax_swing_missing(self):
        ok, why, _ = evaluate_and_vol_climax_swing({})
        self.assertFalse(ok)
        self.assertEqual(why, "volume_1h_missing")

    def test_and_vol_climax_selfbuilt_missing(self):
        ok, why, _ = evaluate_and_vol_climax_selfbuilt({})
        self.assertFalse(ok)
        self.assertEqual(why, "volume_1h_missing")

    def test_and_swing_selfbuilt_missing(self):
        ok, why, _ = evaluate_and_swing_selfbuilt({})
        self.assertFalse(ok)
        self.assertEqual(why, "liq_missing")

    def test_and_no_or_shortcut_vol_pass_swing_fail(self):
        # vol climax admits; swing fails on liq → overall reject (not OR)
        row = {
            "volume_1h": 3000.0,
            "volume_h24": 1000.0,  # accel = 3000/(1000/24)=72 >= 3
            "market_cap": 5_000_000.0,
            "txns_h1_buys": 70.0,
            "txns_h1_sells": 30.0,  # buy_ratio=0.7 >= 0.58
            # swing: liq missing
        }
        ok, why, extras = evaluate_and_vol_climax_swing(row)
        self.assertFalse(ok)
        self.assertEqual(why, "liq_missing")
        self.assertTrue(extras.get("leg_vol_climax"))

    def test_and_no_or_shortcut_swing_pass_selfbuilt_fail(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        row = {
            "liquidity_usd": 200_000.0,
            "pool_age_days": 10.0,
            "rsi_14": 45.0,  # swing band [35,55] and selfbuilt [40,65)
            "change_24h": -5.0,
            "change_5m": 1.0,
            "change_1h": 1.0,
            # selfbuilt needs volatility_24h
        }
        ok, why, extras = evaluate_and_swing_selfbuilt(row, now=now)
        self.assertFalse(ok)
        self.assertEqual(why, "vol_missing")
        self.assertTrue(extras.get("leg_swing"))


if __name__ == "__main__":
    unittest.main()
