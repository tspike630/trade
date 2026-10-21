"""decision_log extras carry jev_age_min (gate value or null) and jev_stance_raw. Logging only."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import decision_engine  # noqa: F401
from decision_engine.core.signal_bus import SignalBus
from decision_engine.strategies.jev_strategy import JevStrengthStrategy

NOW_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _buy(**kw):
    s = {"strategy_id": "jev_strength_v1", "action": "buy", "chain_id": "solana",
         "token_address": "Tok111", "pair_address": "Pair111", "size_pct": 0.05,
         "reason": "t", "risk_passed": True}
    s.update(kw)
    return s


class LogJevAgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "out.db"
        self.bus = SignalBus(self.db, dry_run=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _extras(self):
        c = sqlite3.connect(str(self.db))
        rows = [json.loads(r[0]).get("extras") or {} for r in c.execute("SELECT raw_json FROM decision_log ORDER BY id")]
        c.close()
        return rows

    def test_strategy_signal_logs_gate_age_and_stance(self):
        row = {"chain_id": "solana", "token_address": "Tok111", "primary_pair_address": "Pair111",
               "jev_strength": 0.9, "jev_signal": "bullish", "jev_score": 90.0,
               "jev_stance": "BULLISH", "jev_age_min": 7.25, "jev_updated_at": NOW_ISO}
        sigs = JevStrengthStrategy(min_jev_strength=0.3, max_age_min=1440,
                                   require_bullish_stance=True).on_tick({"hot_tokens": [row]})
        self.assertEqual(len(sigs), 1)
        for s in sigs:
            s["risk_passed"] = True
            self.bus.publish(s)
        ex = self._extras()[0]
        self.assertEqual(ex["jev_age_min"], 7.2)  # gate's rounded value
        self.assertEqual(ex["jev_stance_raw"], "BULLISH")

    def test_missing_or_nonfinite_age_logged_as_null(self):
        self.bus.publish(_buy())
        self.bus.publish(_buy(token_address="Tok222", jev_age_min=float("nan"), jev_stance_raw="BULLISH"))
        a, b = self._extras()
        self.assertIn("jev_age_min", a)
        self.assertIsNone(a["jev_age_min"])
        self.assertNotIn("jev_stance_raw", a)
        self.assertIsNone(b["jev_age_min"])
        self.assertEqual(b["jev_stance_raw"], "BULLISH")


if __name__ == "__main__":
    unittest.main()
