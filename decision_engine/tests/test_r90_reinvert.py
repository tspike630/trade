"""Smoke: −90% reinvert arms cs_mom_1w_r90 / rsi_bounce_r90."""
from __future__ import annotations

import unittest

from decision_engine.core.signal_bus import PURPOSES
from decision_engine.strategies import STRATEGY_REGISTRY, get_strategy
from decision_engine.strategies.cs_mom_1w_r90 import CsMom1wR90Strategy
from decision_engine.strategies.rsi_bounce_r90 import RsiBounceR90Strategy

R90_IDS = ("cs_mom_1w_r90_v1", "rsi_bounce_r90_v1")
R90_PURPOSES = ("race_cs_mom_1w_r90", "race_rsi_bounce_r90")


class R90ReinvertTests(unittest.TestCase):
    def test_registry_and_purposes(self):
        for sid in R90_IDS:
            self.assertIn(sid, STRATEGY_REGISTRY)
            self.assertIsNotNone(get_strategy(sid))
        for purpose in R90_PURPOSES:
            self.assertIn(purpose, PURPOSES)

    def test_cs_mom_1w_r90_top20_and_gates(self):
        s = CsMom1wR90Strategy(position_pct=0.05)
        hot = []
        # 10 weak / 2 strong positive with vs+liq
        for i in range(10):
            hot.append({
                "chain_id": "solana", "token_address": f"w{i}", "primary_pair_address": f"pw{i}",
                "liquidity_usd": 80_000, "volume_strength": 1.0, "change_24h": -0.05 + i * 0.001,
            })
        hot.append({
            "chain_id": "solana", "token_address": "top1", "primary_pair_address": "pt1",
            "liquidity_usd": 200_000, "volume_strength": 3.0, "change_24h": 0.80,
        })
        hot.append({
            "chain_id": "solana", "token_address": "top2", "primary_pair_address": "pt2",
            "liquidity_usd": 180_000, "volume_strength": 2.5, "change_24h": 0.70,
        })
        sigs = s.on_tick({"hot_tokens": hot})
        toks = {x.get("token_address") for x in sigs}
        self.assertIn("top1", toks)
        self.assertTrue(all(x["action"] == "buy" for x in sigs))

    def test_rsi_bounce_r90_oversold_bounce(self):
        s = RsiBounceR90Strategy()
        # miss: overbought
        miss = {"chain_id": "solana", "token_address": "a", "rsi_14": 75, "change_5m": 0.02}
        # miss: oversold but not bounce
        flat = {"chain_id": "solana", "token_address": "b", "rsi_14": 25, "change_5m": -0.01}
        # hit
        hit = {"chain_id": "solana", "token_address": "c", "rsi_14": 28, "change_5m": 0.03,
               "primary_pair_address": "pc"}
        sigs = s.on_tick({"hot_tokens": [miss, flat, hit]})
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0]["token_address"], "c")
        self.assertGreater(s.rejected.get("rsi_not_oversold", 0), 0)
        self.assertGreater(s.rejected.get("change_5m_not_bounce", 0), 0)

    def test_rsi_bounce_r90_missing_skip(self):
        s = RsiBounceR90Strategy()
        sigs = s.on_tick({"hot_tokens": [
            {"chain_id": "solana", "token_address": "x", "change_5m": 0.1},
            {"chain_id": "solana", "token_address": "y", "rsi_14": 20},
        ]})
        self.assertEqual(sigs, [])
        self.assertGreater(s.rejected.get("rsi_14_missing", 0), 0)
        self.assertGreater(s.rejected.get("change_5m_missing", 0), 0)


if __name__ == "__main__":
    unittest.main()
