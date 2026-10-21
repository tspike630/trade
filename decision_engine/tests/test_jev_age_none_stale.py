"""jev_age_min present-but-None must be treated as stale (no jev_updated_at fallback)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from decision_engine.strategies.jev_strategy import JevStrengthStrategy, jev_age_min, jev_gate

NOW_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(**kw):
    r = {"chain_id": "solana", "token_address": "tok", "primary_pair_address": "p",
         "jev_strength": 0.9, "jev_signal": "bullish", "jev_score": 90.0,
         "jev_stance": "BULLISH", "jev_updated_at": NOW_ISO}
    r.update(kw)
    return r


class JevAgeNoneStaleTests(unittest.TestCase):
    def test_age_none_with_fresh_updated_at_is_stale(self):
        row = _row(jev_age_min=None)
        self.assertIsNone(jev_age_min(row))
        why, extras = jev_gate(row, 1440, True)
        self.assertEqual(why, "jev_stale")
        self.assertIsNone(extras["jev_age_min"])
        s = JevStrengthStrategy(min_jev_strength=0.3, max_age_min=1440, require_bullish_stance=True)
        self.assertEqual(s.on_tick({"hot_tokens": [row]}), [])
        self.assertEqual(s.rejected["jev_stale"], 1)

    def test_legacy_row_without_column_uses_updated_at(self):
        row = _row()
        self.assertNotIn("jev_age_min", row)
        age = jev_age_min(row)
        self.assertIsNotNone(age)
        self.assertLess(age, 2)
        self.assertIsNone(jev_gate(row, 1440, True)[0])

    def test_valid_fresh_bullish_passes(self):
        row = _row(jev_age_min=5.0)
        self.assertEqual(jev_age_min(row), 5.0)
        s = JevStrengthStrategy(min_jev_strength=0.3, max_age_min=1440, require_bullish_stance=True)
        out = s.on_tick({"hot_tokens": [row]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["jev_age_min"], 5.0)

    def test_nan_inf_negative_are_stale(self):
        for bad in (float("nan"), float("inf"), float("-inf"), "nan", "NaN", "inf", -1.0):
            row = _row(jev_age_min=bad)
            self.assertIsNone(jev_age_min(row), repr(bad))
            self.assertEqual(jev_gate(row, 1440, True)[0], "jev_stale", repr(bad))


if __name__ == "__main__":
    unittest.main()
