"""JEV stale/stance gates also cover factor_jev_timeexit_v1 / trail_v1 / rsi_and_v1."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import decision_engine.strategies as de
from decision_engine.strategies import JEV_GATED_STRATEGIES, STRATEGY_REGISTRY

NOW = datetime.now(timezone.utc)
EXTENDED = ("factor_jev_timeexit_v1", "factor_jev_trail_v1", "factor_jev_rsi_and_v1")


def _row(age_h=1.0, stance="BULLISH", **kw):
    r = {"chain_id": "solana", "token_address": "tok", "primary_pair_address": "p",
         "jev_strength": 0.5, "jev_signal": "bullish", "jev_score": 80.0,
         "jev_updated_at": (NOW - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "jev_stance": stance, "rsi_14": 25.0, "change_5m": 0.02}
    r.update(kw)
    return r


def _run(sid, rows, **kw):
    s = STRATEGY_REGISTRY[sid](min_jev_strength=0.1, **kw)
    return s, s.on_tick({"hot_tokens": rows})


def _jev_factor(s):
    return next(f for f in s._buy_factors if f.factor_name == "jev_strength")


class ExtendedGateTests(unittest.TestCase):
    def test_fresh_bullish_passes_with_extras(self):
        for sid in EXTENDED:
            _, out = _run(sid, [_row()])
            self.assertEqual(len(out), 1, sid)
            self.assertEqual(out[0]["jev_stance_raw"], "BULLISH", sid)
            self.assertAlmostEqual(out[0]["jev_age_min"], 60.0, delta=2, msg=sid)

    def test_stale_rejected(self):
        for sid in EXTENDED:
            s, out = _run(sid, [_row(age_h=48), _row(jev_updated_at=None)])
            self.assertEqual(out, [], sid)
            self.assertEqual(_jev_factor(s).rejected["jev_stale"], 2, sid)

    def test_stance_not_bullish_rejected(self):
        for sid in EXTENDED:
            s, out = _run(sid, [_row(stance="NOT BULLISH"), _row(stance="NEUTRAL"), _row(stance=None)])
            self.assertEqual(out, [], sid)
            self.assertEqual(_jev_factor(s).rejected["jev_stance_not_bullish"], 3, sid)

    def test_gate_threshold_override(self):
        for sid in EXTENDED:
            _, out = _run(sid, [_row(age_h=48, stance="NEUTRAL")],
                          max_age_min=0, require_bullish_stance=False)
            self.assertEqual(len(out), 1, sid)

    def test_rsi_and_still_requires_rsi(self):
        _, out = _run("factor_jev_rsi_and_v1", [_row(rsi_14=60.0)])
        self.assertEqual(out, [])


class CanonicalListTests(unittest.TestCase):
    def test_exact_list(self):
        self.assertEqual(JEV_GATED_STRATEGIES, frozenset({
            "jev_strength_v1", "factor_jev_strength_v1",
            "combined_jev_tech_v1", "factor_combined_jev_tech_v1",
            "factor_jev_timeexit_v1", "factor_jev_trail_v1", "factor_jev_rsi_and_v1"}))

    def test_all_registered_and_no_jev_combo_left_out(self):
        for sid in JEV_GATED_STRATEGIES:
            self.assertIn(sid, STRATEGY_REGISTRY)
        outside = {sid for sid, (buy, *_r) in de._FACTOR_COMBOS.items()
                   if de.JEV_GATED_BUY_FACTORS & set(buy)} - JEV_GATED_STRATEGIES
        self.assertEqual(outside, set())


if __name__ == "__main__":
    unittest.main()
