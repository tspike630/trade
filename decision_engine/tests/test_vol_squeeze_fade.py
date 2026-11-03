"""Smoke: vol_squeeze_fade hit lower+squeeze+vol; reject upper breakout; missing skip."""

from __future__ import annotations

import unittest

from decision_engine.core.signal_bus import PURPOSES
from decision_engine.strategies import STRATEGY_REGISTRY, get_strategy
from decision_engine.strategies.vol_squeeze_fade import (
    VolSqueezeFadeStrategy,
    evaluate_vol_squeeze_fade,
)


def _base(**overrides):
    row = {
        "chain_id": "solana",
        "token_address": "vsf1",
        "liquidity_usd": 80_000,
        "price_usd": 0.90,
        "boll_upper": 1.03,
        "boll_mid": 1.00,
        "boll_lower": 0.97,
        "volume_strength": 2.0,
        "volume_1h": 2000,
        "volume_h24": 24000,
    }
    row.update(overrides)
    return row


class TestVolSqueezeFade(unittest.TestCase):
    def test_registry_and_purpose(self):
        self.assertIn("vol_squeeze_fade_v1", STRATEGY_REGISTRY)
        get_strategy("vol_squeeze_fade_v1")
        get_strategy("vol_squeeze_fade")
        get_strategy("race_vol_squeeze_fade")
        self.assertIn("race_vol_squeeze_fade", PURPOSES)

    def test_missing_fields_skip(self):
        ok, why, _ = evaluate_vol_squeeze_fade({})
        self.assertFalse(ok)
        self.assertEqual(why, "liq_missing")
        ok2, why2, _ = evaluate_vol_squeeze_fade(
            {"liquidity_usd": 80_000, "price_usd": 0.5}
        )
        self.assertFalse(ok2)
        self.assertEqual(why2, "boll_missing")
        s = VolSqueezeFadeStrategy()
        self.assertEqual(s.on_tick({"hot_tokens": [{}]}), [])
        self.assertTrue(s.rejected)

    def test_hit_lower_squeeze_vol(self):
        # BW = (1.03-0.97)/1.00 = 0.06 <= 0.08; price 0.90 < lower 0.97; vs=2.0
        ok, why, extras = evaluate_vol_squeeze_fade(_base())
        self.assertTrue(ok, why)
        self.assertIsNone(why)
        self.assertEqual(extras.get("vol_confirm"), "volume_strength")
        self.assertLessEqual(extras["bandwidth"], 0.08)
        s = VolSqueezeFadeStrategy()
        sigs = s.on_tick({"hot_tokens": [_base()]})
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["action"], "buy")

    def test_reject_upper_breakout_source_direction(self):
        # Same squeeze+vol but price above upper → must NOT hit fade
        row = _base(price_usd=1.10)  # > boll_upper 1.03
        ok, why, _ = evaluate_vol_squeeze_fade(row)
        self.assertFalse(ok)
        self.assertEqual(why, "not_below_lower")
        s = VolSqueezeFadeStrategy()
        sigs = s.on_tick({"hot_tokens": [row]})
        self.assertEqual(sigs, [])
        self.assertIn("not_below_lower", s.rejected)

    def test_not_compressed_skip(self):
        # Wide bands: BW = (1.20-0.80)/1.00 = 0.40 > 0.08
        row = _base(boll_upper=1.20, boll_lower=0.80, price_usd=0.70)
        ok, why, _ = evaluate_vol_squeeze_fade(row)
        self.assertFalse(ok)
        self.assertEqual(why, "not_compressed")


if __name__ == "__main__":
    unittest.main()
