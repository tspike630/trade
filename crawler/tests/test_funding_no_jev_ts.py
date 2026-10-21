"""Regression: jev row without fetched_at/analyzed_at must not look fresh."""

from __future__ import annotations

import os
import sys
import unittest

from indicators.funding import compute_funding_fields

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _load_jev_gate():
    """Import the repo-root decision_engine (crawler/ has an older copy without jev_gate)."""
    saved_path = list(sys.path)
    saved_mods = {k: v for k, v in sys.modules.items() if k == "decision_engine" or k.startswith("decision_engine.")}
    for k in saved_mods:
        del sys.modules[k]
    sys.path.insert(0, _REPO)
    try:
        from decision_engine.strategies.jev_strategy import jev_gate as g
        return g
    finally:
        sys.path[:] = saved_path
        for k in [k for k in sys.modules if k == "decision_engine" or k.startswith("decision_engine.")]:
            del sys.modules[k]
        sys.modules.update(saved_mods)


jev_gate = _load_jev_gate()

SIG = {"stance": "BULLISH", "jev_score": 90, "coverage_pct": 100,
       "fetched_at": None, "analyzed_at": None}


class FundingNoJevTimestamp(unittest.TestCase):
    def test_no_timestamp_gives_none(self):
        f = compute_funding_fields(dict(SIG))
        self.assertIsNone(f["jev_updated_at"])
        self.assertIsNone(f["jev_age_min"])
        self.assertEqual(f["jev_signal"], "bullish")

    def test_decision_gate_rejects(self):
        f = compute_funding_fields(dict(SIG))
        row = {**f, "jev_stance": SIG["stance"]}
        why, _ = jev_gate(row, max_age_min=60.0, require_bullish_stance=True)
        self.assertEqual(why, "jev_stale")


class FutureTsTests(unittest.TestCase):
    def test_future_ts(self):
        from datetime import datetime, timedelta, timezone
        from indicators.funding import jev_age_minutes
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        iso = lambda d: (now + d).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(jev_age_minutes(iso(timedelta(minutes=10)), now))
        self.assertEqual(jev_age_minutes(iso(timedelta(minutes=1)), now), 0.0)
        self.assertEqual(jev_age_minutes(iso(timedelta(minutes=-5)), now), 5.0)


if __name__ == "__main__":
    unittest.main()
