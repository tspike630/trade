"""Per-token buy re-emit cooldown + signal_id / is_first / dup_of."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from decision_engine.core.signal_bus import SignalBus
from decision_engine.core.token_cooldown import TokenCooldown

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _buy(token="TokA", ts=None, **kw):
    s = {"strategy_id": "jev_strength_v1", "action": "buy", "chain_id": "solana",
         "token_address": token, "size_pct": 0.05, "risk_passed": True}
    if ts is not None:
        s["ts"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    s.update(kw)
    return s


class TokenCooldownTest(unittest.TestCase):
    def test_suppresses_within_window_and_reemits_after(self):
        tc = TokenCooldown(60)
        a = tc.filter([_buy(ts=T0)])
        self.assertEqual(len(a), 1)
        self.assertTrue(a[0]["is_first"])
        self.assertIsNone(a[0]["dup_of"])
        self.assertEqual(a[0]["signal_id"], "solana:toka:2026-10-20T12:00:00Z")
        self.assertEqual(tc.filter([_buy(ts=T0 + timedelta(minutes=59))]), [])
        c = tc.filter([_buy(ts=T0 + timedelta(minutes=61))])
        self.assertEqual(len(c), 1)
        self.assertTrue(c[0]["is_first"])
        self.assertEqual(c[0]["signal_id"], "solana:toka:2026-10-20T13:01:00Z")

    def test_other_tokens_and_non_buys_unaffected(self):
        tc = TokenCooldown(60)
        tc.filter([_buy(ts=T0)])
        out = tc.filter([_buy("TokB", ts=T0), _buy(ts=T0, action="sell"), _buy(ts=T0)])
        self.assertEqual([s["token_address"] for s in out], ["TokB", "TokA"])
        self.assertEqual(out[1]["action"], "sell")
        self.assertNotIn("signal_id", out[1])

    def test_same_tick_duplicates(self):
        tc = TokenCooldown(60)
        self.assertEqual(len(tc.filter([_buy(ts=T0), _buy(ts=T0)])), 1)

    def test_zero_disables_but_labels(self):
        tc = TokenCooldown(0)
        out = tc.filter([_buy(ts=T0), _buy(ts=T0 + timedelta(minutes=1))])
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0]["is_first"])
        self.assertFalse(out[1]["is_first"])
        self.assertEqual(out[1]["dup_of"], out[0]["signal_id"])
        self.assertEqual(out[1]["signal_id"], out[0]["signal_id"])

    def test_state_survives_restart_via_decision_log(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "out.db")
            bus = SignalBus(db, dry_run=True, purpose="shadow_010")
            tc = TokenCooldown(60)
            bus.publish_many(tc.filter([_buy(ts=T0)]))
            bus.close()
            import sqlite3
            raw = json.loads(sqlite3.connect(db).execute("select raw_json from decision_log").fetchone()[0])
            self.assertEqual(raw["extras"]["signal_id"], "solana:toka:2026-10-20T12:00:00Z")
            self.assertTrue(raw["extras"]["is_first"])
            tc2 = TokenCooldown(60)
            self.assertEqual(tc2.load_from_db(db), 1)
            self.assertEqual(tc2.filter([_buy(ts=T0 + timedelta(minutes=30))]), [])
            later = tc2.filter([_buy(ts=T0 + timedelta(minutes=90))])
            self.assertEqual(len(later), 1)

    def test_load_legacy_rows_without_signal_id(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "out.db")
            bus = SignalBus(db, dry_run=True, purpose="shadow_010")
            bus.publish(_buy(ts=T0))
            bus.close()
            tc = TokenCooldown(0)
            tc.load_from_db(db)
            out = tc.filter([_buy(ts=T0 + timedelta(minutes=5))])
            self.assertEqual(out[0]["dup_of"], "solana:toka:2026-10-20T12:00:00Z")

    def test_missing_db_is_empty(self):
        self.assertEqual(TokenCooldown(60).load_from_db("/nonexistent/x.db"), 0)


class CliFlagTest(unittest.TestCase):
    def test_default_and_flag(self):
        import decision_engine.run as r
        import inspect
        src = inspect.getsource(r.main)
        self.assertIn("--token-cooldown-min", src)
        self.assertIn("DEFAULT_TOKEN_COOLDOWN_MIN", src)


if __name__ == "__main__":
    unittest.main()
