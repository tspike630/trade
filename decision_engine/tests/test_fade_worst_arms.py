"""Smoke: wipe→fade arms import, missing-field skip, synthetic hit."""

from __future__ import annotations

import unittest

from decision_engine.core.signal_bus import PURPOSES
from decision_engine.strategies import STRATEGY_REGISTRY, get_strategy
from decision_engine.strategies.tech_fade import TechFadeStrategy
from decision_engine.strategies.cs_fade_1w import CsFade1wStrategy
from decision_engine.strategies.rs_fade_10d import RsFade10dStrategy
from decision_engine.strategies.vol_spike_fade import (
    VolSpikeFadeStrategy,
    evaluate_vol_spike_fade,
)
from decision_engine.strategies.rsi_bounce_fade import RsiBounceFadeStrategy
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from decision_engine.strategies.rsi_boll_fade import RsiBollFadeStrategy


FADE_IDS = (
    "tech_fade_v1",
    "cs_fade_1w_v1",
    "rs_fade_10d_v1",
    "vol_spike_fade_v1",
    "rsi_bounce_fade_v1",
    "tech_boll_fade_v1",
    "rsi_boll_fade_v1",
)
FADE_PURPOSES = (
    "race_tech_fade",
    "race_cs_fade_1w",
    "race_rs_fade_10d",
    "race_vol_spike_fade",
    "race_rsi_bounce_fade",
    "race_tech_boll_fade",
    "race_rsi_boll_fade",
)


class TestFadeWorstArms(unittest.TestCase):
    def test_registry_and_purposes(self):
        for sid in FADE_IDS:
            self.assertIn(sid, STRATEGY_REGISTRY)
            get_strategy(sid)
        for purpose in FADE_PURPOSES:
            self.assertIn(purpose, PURPOSES)
        # synth AND peers still registered
        for sid in (
            "and_vol_climax_swing_v1",
            "and_vol_climax_selfbuilt_v1",
            "and_swing_selfbuilt_v1",
        ):
            self.assertIn(sid, STRATEGY_REGISTRY)

    def test_tech_fade_missing_and_hit(self):
        s = TechFadeStrategy()
        self.assertEqual(s.on_tick({"hot_tokens": [{}]}), [])
        self.assertTrue(s.rejected)
        hit = s.on_tick(
            {
                "hot_tokens": [
                    {
                        "chain_id": "solana",
                        "token_address": "t1",
                        "change_5m": -2.5,
                        "volume_strength": 0.8,
                        "liquidity_usd": 100_000,
                    }
                ]
            }
        )
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["action"], "buy")

    def test_cs_fade_empty_and_bottom_hit(self):
        s = CsFade1wStrategy()
        self.assertEqual(s.on_tick({"hot_tokens": []}), [])
        self.assertIn("universe_too_small", s.rejected)
        rows = []
        for i in range(12):
            rows.append(
                {
                    "chain_id": "solana",
                    "token_address": f"t{i}",
                    # losers get higher liq so they clear med_liq floor
                    "liquidity_usd": 200_000 if i < 3 else 80_000 + i,
                    "volume_strength": 1.0,
                    "change_24h": -50.0 if i < 3 else (10.0 + i),
                }
            )
        sigs = s.on_tick({"hot_tokens": rows})
        self.assertGreaterEqual(len(sigs), 1)
        self.assertTrue(all(x["action"] == "buy" for x in sigs))

    def test_rs_fade_empty_and_bottom_hit(self):
        s = RsFade10dStrategy()
        self.assertEqual(s.on_tick({"hot_tokens": []}), [])
        rows = []
        for i in range(12):
            rows.append(
                {
                    "chain_id": "solana",
                    "token_address": f"r{i}",
                    "liquidity_usd": 200_000 if i < 3 else 80_000 + i,
                    "volume_strength": 1.0,
                    "change_1h": -20.0 if i < 3 else (5.0 + i),
                }
            )
        sigs = s.on_tick({"hot_tokens": rows})
        self.assertGreaterEqual(len(sigs), 1)

    def test_vol_spike_fade_missing_and_hit(self):
        ok, why, _ = evaluate_vol_spike_fade({})
        self.assertFalse(ok)
        self.assertEqual(why, "liq_missing")
        s = VolSpikeFadeStrategy()
        sigs = s.on_tick(
            {
                "hot_tokens": [
                    {
                        "chain_id": "solana",
                        "token_address": "vs1",
                        "liquidity_usd": 80_000,
                        "pool_age_days": 3.0,
                        "volume_1h": 3000,
                        "volume_h24": 2400,  # baseline 100; spike 30x
                        "change_1h": -5.0,
                    }
                ]
            }
        )
        self.assertEqual(len(sigs), 1)

    def test_rsi_bounce_fade_missing_and_hit(self):
        s = RsiBounceFadeStrategy()
        self.assertEqual(s.on_tick({"hot_tokens": [{}]}), [])
        sigs = s.on_tick(
            {
                "hot_tokens": [
                    {
                        "chain_id": "solana",
                        "token_address": "rb1",
                        "rsi_14": 75.0,
                        "change_5m": -1.2,
                        "liquidity_usd": 90_000,
                    }
                ]
            }
        )
        self.assertEqual(len(sigs), 1)

    def test_tech_boll_fade_and_hit(self):
        s = TechBollFadeStrategy()
        self.assertEqual(s.on_tick({"hot_tokens": [{"change_5m": -3, "volume_strength": 1}]}), [])
        sigs = s.on_tick(
            {
                "hot_tokens": [
                    {
                        "chain_id": "solana",
                        "token_address": "tb1",
                        "change_5m": -3.0,
                        "volume_strength": 0.9,
                        "price_usd": 0.9,
                        "boll_lower": 1.0,
                        "liquidity_usd": 90_000,
                    }
                ]
            }
        )
        self.assertEqual(len(sigs), 1)

    def test_rsi_boll_fade_and_hit(self):
        s = RsiBollFadeStrategy()
        sigs = s.on_tick(
            {
                "hot_tokens": [
                    {
                        "chain_id": "solana",
                        "token_address": "rbf1",
                        "rsi_14": 72.0,
                        "change_5m": -0.5,
                        "price_usd": 0.8,
                        "boll_lower": 1.0,
                        "liquidity_usd": 90_000,
                    }
                ]
            }
        )
        self.assertEqual(len(sigs), 1)


if __name__ == "__main__":
    unittest.main()
