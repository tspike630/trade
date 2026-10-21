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


if __name__ == "__main__":
    unittest.main()
