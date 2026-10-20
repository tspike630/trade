"""JEV buys: reject stale jev data and score-band 'bullish' with negated/neutral stance."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from decision_engine.strategies.jev_strategy import JevStrengthStrategy, jev_age_min, stance_is_bullish

NOW = datetime.now(timezone.utc)


def _row(age_h=1.0, stance="BULLISH", **kw):
    r = {"chain_id": "solana", "token_address": "tok", "primary_pair_address": "p",
         "jev_strength": 0.5, "jev_signal": "bullish", "jev_score": 80.0,
         "jev_updated_at": (NOW - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "jev_stance": stance}
    r.update(kw)
    return r


def _run(rows, **kw):
    s = JevStrengthStrategy(min_jev_strength=0.1, **kw)
    return s, s.on_tick({"hot_tokens": rows})


class StanceTest(unittest.TestCase):
    def test_parse(self):
        self.assertTrue(stance_is_bullish("BULLISH"))
        self.assertTrue(stance_is_bullish("lean bullish"))
        for s in ("NOT BULLISH", "not  bullish", "NEUTRAL", "INSUFFICIENT EVIDENCE", None, ""):
            self.assertFalse(stance_is_bullish(s), s)

    def test_negated_and_neutral_rejected(self):
        s, out = _run([_row(stance="NOT BULLISH"), _row(stance="NEUTRAL"), _row(stance=None)])
        self.assertEqual(out, [])
        self.assertEqual(s.rejected["jev_stance_not_bullish"], 3)

    def test_bullish_fresh_passes_with_extras(self):
        _, out = _run([_row()])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["jev_stance"], "BULLISH")
        self.assertAlmostEqual(out[0]["jev_age_min"], 60.0, delta=2)

    def test_stance_check_can_be_disabled(self):
        _, out = _run([_row(stance="NOT BULLISH")], require_bullish_stance=False)
        self.assertEqual(len(out), 1)


class FreshnessTest(unittest.TestCase):
    def test_stale_rejected(self):
        s, out = _run([_row(age_h=48)])
        self.assertEqual(out, [])
        self.assertEqual(s.rejected["jev_stale"], 1)

    def test_missing_ts_with_column_rejected(self):
        _, out = _run([_row(jev_updated_at=None)])
        self.assertEqual(out, [])

    def test_threshold_and_disable(self):
        self.assertEqual(len(_run([_row(age_h=5)], max_age_min=360)[1]), 1)
        self.assertEqual(len(_run([_row(age_h=7)], max_age_min=360)[1]), 0)
        self.assertEqual(len(_run([_row(age_h=500)], max_age_min=0)[1]), 1)

    def test_indicator_age_column_preferred(self):
        self.assertEqual(jev_age_min({"jev_age_min": 12, "jev_updated_at": "2000-01-01T00:00:00Z"}), 12.0)
        self.assertEqual(len(_run([_row(age_h=500, jev_age_min=5)])[1]), 1)


if __name__ == "__main__":
    unittest.main()
