"""C-exit shadow runner: accept-purpose, regime split, gap_stop, notional cap."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution import c_exit_size_mult as sm
from execution import c_exit_shadow_run as run
from execution.c_exit_shadow_ledger import CExitShadowLedger, is_gap_stop

BOUNDS = sm.load_regime_bounds(Path("/nonexistent"))  # fallback constants


class TestRegime(unittest.TestCase):
    def test_three_segments(self):
        self.assertEqual(sm.regime_at("2026-10-20T16:40:24Z", BOUNDS), "old20")
        self.assertEqual(sm.regime_at("2026-10-20T16:40:25Z", BOUNDS), "exhausted")
        self.assertEqual(sm.regime_at("2026-10-21T07:59:59+08:00", BOUNDS), "exhausted")
        self.assertEqual(sm.regime_at("2026-10-21T00:00:00Z", BOUNDS), "new4")

    def test_reads_json(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "r.json"
            f.write_text(json.dumps({"regimes": [{"regime": "new4",
                "effective_at_utc": "2026-10-20T16:40:25Z",
                "first_fresh_budget_at_hkt": "2026-10-21T08:00:00+08:00"}]}))
            self.assertEqual(sm.load_regime_bounds(f), BOUNDS)

    def test_labels_both_names(self):
        a = sm.normalize_labels({"extras": {"liq_bucket": "missing", "budget_regime": "4h"}},
                                "2026-10-21T01:00:00Z", BOUNDS)
        self.assertEqual(a, {"liq_bucket": "missing", "x_budget_regime_raw": "4h",
                             "x_budget_regime": "new4"})
        b = sm.normalize_labels({"extras": {"x_budget_regime": "20h"}}, "2026-10-20T10:00:00Z", BOUNDS)
        self.assertEqual(b["x_budget_regime"], "old20")


class TestGapAndCap(unittest.TestCase):
    def test_gap_stop(self):
        self.assertTrue(is_gap_stop(-64.0, 100.0, 5.0))
        self.assertTrue(is_gap_stop(-99.9, 100.0, 5.0))
        self.assertFalse(is_gap_stop(-10.0, 100.0, 5.0))
        self.assertFalse(is_gap_stop(-5.2, 100.0, 5.0))

    def test_gap_stop_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            with CExitShadowLedger(Path(d) / "c.db") as led:
                pid = led._open_one(tag="c_sl5_h60", chain_id="s", pair_address="p",
                    token_address=None, entry_price=1.0, size_usd=100.0, tp_pct=10,
                    sl_pct=5, max_hold_min=60, source_decision_id=1, meta={})
                led.tick(lambda p: 0.30)
                r = led.conn.execute("SELECT gap_stop, exit_reason FROM positions WHERE id=?", (pid,)).fetchone()
                self.assertEqual(tuple(r), (1, "stop_loss"))
                self.assertEqual(led.status()["tags"]["c_sl5_h60"]["n_gap_stop"], 1)

    def test_notional_cap(self):
        self.assertAlmostEqual(sm.notional_cap_for_mult(1.0), 193.0)
        self.assertAlmostEqual(round(sm.notional_cap_for_mult(1.5)), 129)
        self.assertAlmostEqual(round(sm.notional_cap_for_mult(3.0)), 64)

    def test_tail_loss_cap_takes_min(self):
        with tempfile.TemporaryDirectory() as d:
            with CExitShadowLedger(Path(d) / "c.db") as led:
                for px in (1.0, 0.5):  # tail worst loss 50%
                    pid = led._open_one(tag="c_sl5_h60", chain_id="s", pair_address="p",
                        token_address=None, entry_price=1.0, size_usd=100.0, tp_pct=10,
                        sl_pct=5, max_hold_min=60, source_decision_id=1, meta={})
                    led.close_position(pid, price=px, reason="t")
                self.assertAlmostEqual(sm.cell_notional_cap(led.conn, "c_sl5_h60", 1.0, max_loss_usd=50), 100.0)
                self.assertAlmostEqual(sm.cell_notional_cap(led.conn, "c_sl5_h60", 1.0), 193.0)


def _mk_decisions(path: Path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE decision_log (id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT, action TEXT,"
              " chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT, risk_passed INTEGER,"
              " mode TEXT, raw_json TEXT, purpose TEXT)")
    for i, purpose in enumerate(rows, 1):
        raw = {"action": "buy", "chain_id": "solana", "pair_address": f"pair{i}", "size_pct": 0.05,
               "purpose": purpose, "ts": "2026-10-21T01:00:00Z",
               "extras": {"liq_bucket": "present", "budget_regime": "4h"}}
        c.execute("INSERT INTO decision_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (i, raw["ts"], "s", "buy", "solana", f"tok{i}", 0.05, "", 1, "dry_run",
                   json.dumps(raw), purpose))
    c.commit()
    c.close()


class TestAcceptPurpose(unittest.TestCase):
    def _run(self, accept):
        with tempfile.TemporaryDirectory() as d:
            dec = Path(d) / "decision_shadow_entry_010.db"
            _mk_decisions(dec, ["shadow_010", "live_candidate", "sample"])
            with mock.patch.object(run, "_latest_mid", return_value=(1.0, 1e6)), \
                 CExitShadowLedger(Path(d) / "c.db") as led:
                out = run.process_once(led, decision_db=dec, crawler_db=dec, accept_purposes=accept)
                rows = led.conn.execute("SELECT meta_json, size_usd FROM positions").fetchall()
                return out, [json.loads(r[0]) for r in rows], [r[1] for r in rows]

    def test_default_ignores_shadow(self):
        out, metas, _ = self._run(("live_candidate",))
        self.assertEqual({m["signal_purpose"] for m in metas}, {"live_candidate"})
        self.assertEqual(len(metas), 8)

    def test_explicit_shadow_plus_legacy(self):
        out, metas, sizes = self._run(("shadow_010", "live_candidate"))
        self.assertEqual(len(metas), 16)
        self.assertEqual(metas[0]["x_budget_regime"], "new4")
        self.assertEqual(metas[0]["liq_bucket"], "present")
        self.assertTrue(all(s <= 193.0 + 1e-9 for s in sizes))


if __name__ == "__main__":
    unittest.main()


class TestDedup(unittest.TestCase):
    def test_same_token_rewritten_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            dec = Path(d) / "decision_shadow_entry_010.db"
            _mk_decisions(dec, ["shadow_010", "shadow_010"])
            c = sqlite3.connect(dec)  # second row: same token, different pair
            c.execute("UPDATE decision_log SET token_address='tok1' WHERE id=2")
            raw = json.loads(c.execute("SELECT raw_json FROM decision_log WHERE id=2").fetchone()[0])
            raw["token_address"] = "tok1"
            c.execute("UPDATE decision_log SET raw_json=? WHERE id=2", (json.dumps(raw),))
            c.commit(); c.close()
            with mock.patch.object(run, "_latest_mid", return_value=(1.0, 1e6)), \
                 CExitShadowLedger(Path(d) / "c.db") as led:
                out = run.process_once(led, decision_db=dec, crawler_db=dec,
                                       accept_purposes=("shadow_010",))
                self.assertEqual(out["raw_buys"], 2)
                self.assertEqual(out["opened"], 8)
                self.assertEqual(out["dedup_skipped"], 8)
                # close all, still within 60 min → still blocked
                for pos in led.list_open():
                    led.close_position(pos["id"], price=1.0, reason="t")
                self.assertEqual(run._dedup_block(led, "c_sl5_h60", "solana", "x", "tok1"), "dup_recent")
                self.assertIsNone(run._dedup_block(led, "c_sl5_h60", "solana", "x", "tok1", window_min=-1))
