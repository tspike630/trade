"""Smoke: Wave2 write-green strategies import + missing-field skip."""

from __future__ import annotations

import unittest

from decision_engine.core.signal_bus import PURPOSES
from decision_engine.strategies import STRATEGY_REGISTRY, get_strategy
from decision_engine.strategies.vol_climax_mcap_band import (
    evaluate_vol_climax_mcap_band,
)
from decision_engine.strategies.funding_z_fade import evaluate_funding_z_fade
from decision_engine.strategies.lifecycle_momentum import evaluate_lifecycle_momentum
from decision_engine.strategies.cs_mom_1w import CsMom1wStrategy


class TestW2Writegreen(unittest.TestCase):
    def test_registry_and_purposes(self):
        for sid in (
            "vol_climax_mcap_band_v1",
            "cs_mom_1w_v1",
            "lifecycle_momentum_v1",
            "funding_z_fade_v1",
        ):
            self.assertIn(sid, STRATEGY_REGISTRY)
            get_strategy(sid)
        for purpose in (
            "race_vol_climax_mcap_band",
            "race_cs_mom_1w",
            "race_lifecycle_momentum",
            "race_funding_z_fade",
        ):
            self.assertIn(purpose, PURPOSES)

    def test_vol_climax_missing(self):
        ok, why, _ = evaluate_vol_climax_mcap_band({})
        self.assertFalse(ok)
        self.assertEqual(why, "volume_1h_missing")
        ok, why, _ = evaluate_vol_climax_mcap_band(
            {"volume_1h": 1000, "volume_h24": 1000}
        )
        # accel = 1000/(1000/24)=24 >=3; mcap missing
        self.assertFalse(ok)
        self.assertEqual(why, "mcap_missing")

    def test_funding_missing(self):
        ok, why, extras = evaluate_funding_z_fade({"jev_funding_bias": -3.0})
        self.assertFalse(ok)
        self.assertEqual(why, "funding_missing")
        self.assertTrue(extras.get("jev_funding_bias_ignored"))

    def test_lifecycle_missing_7d(self):
        ok, why, _ = evaluate_lifecycle_momentum(
            {"change_24h": 20.0, "volume_1h": 100.0}
        )
        self.assertFalse(ok)
        self.assertEqual(why, "change_7d_missing")

    def test_cs_mom_1w_empty_universe(self):
        s = CsMom1wStrategy()
        signals = s.on_tick({"hot_tokens": []})
        self.assertEqual(signals, [])
        self.assertIn("universe_too_small", s.rejected)


if __name__ == "__main__":
    unittest.main()
