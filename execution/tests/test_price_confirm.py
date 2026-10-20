"""Pre-stop anomaly confirmation (bzc9… 0.052 fake print case)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution import c_exit_shadow_run as run
from execution import price_confirm as pc
from execution.c_exit_shadow_ledger import CExitShadowLedger

K = ("solana", "bzc9")


class ConfirmerTests(unittest.TestCase):
    def setUp(self):
        self.c = pc.StopPriceConfirmer()
        self.c.check(K, 0.15, 7.7e6, "t0")

    def test_single_bad_print_held_then_recovers(self):
        self.assertEqual(self.c.check(K, 0.052, 5.17e6, "t1"), (None, "suspect_hold"))
        # same stale snapshot re-read does not confirm
        self.assertEqual(self.c.check(K, 0.052, 5.17e6, "t1"), (None, "suspect_hold"))
        self.assertEqual(self.c.check(K, 0.1536, 7.7e6, "t2"), (0.1536, "ok"))
        # pending cleared: a new single bad print is held again
        self.assertEqual(self.c.check(K, 0.0525, 5.16e6, "t3")[1], "suspect_hold")

    def test_two_consecutive_confirm(self):
        self.c.check(K, 0.052, 5.17e6, "t1")
        self.assertEqual(self.c.check(K, 0.05, 5.1e6, "t2"), (0.05, "confirmed"))

    def test_rug_immediate(self):
        self.assertEqual(self.c.check(K, 0.0001, 1.0e6, "t1"), (0.0001, "rug_immediate"))

    def test_small_drop_passes(self):
        self.assertEqual(self.c.check(K, 0.142, 7.6e6, "t1"), (0.142, "ok"))

    def test_main_switch_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PAPER_STOP_CONFIRM_ENABLED", None)
            self.assertFalse(pc.main_book_enabled())
        with mock.patch.dict(os.environ, {"PAPER_STOP_CONFIRM_ENABLED": "1"}):
            self.assertTrue(pc.main_book_enabled())

    def test_shadow_default_on(self):
        self.assertTrue(run.STOP_CONFIRM_ENABLED)


class ShadowTickTests(unittest.TestCase):
    def test_fake_print_no_stop_but_rug_stops(self):
        quotes = iter([
            {"price_usd": 0.15, "liquidity_usd": 7.7e6, "recorded_at": "a"},
            {"price_usd": 0.052, "liquidity_usd": 5.17e6, "recorded_at": "b"},
            {"price_usd": 0.153, "liquidity_usd": 7.7e6, "recorded_at": "c"},
            {"price_usd": 0.0002, "liquidity_usd": 1e5, "recorded_at": "d"},
        ])
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(run, "CONFIRMER", pc.StopPriceConfirmer()), \
             mock.patch.object(run, "_fetch_pending", return_value=[]), \
             mock.patch.object(run, "_open_ro"), \
             CExitShadowLedger(Path(d) / "c.db") as led:
            pid = led._open_one(tag="c_sl5_h60", chain_id="solana", pair_address="bzc9",
                token_address=None, entry_price=0.15, size_usd=100.0, tp_pct=10,
                sl_pct=5, max_hold_min=600, source_decision_id=1, meta={})
            state = lambda: led.conn.execute("SELECT status, exit_reason FROM positions WHERE id=?", (pid,)).fetchone()
            for _ in range(3):
                q = next(quotes)
                with mock.patch.object(run, "latest_quote", return_value=q):
                    run.process_once(led, decision_db=Path("x"), crawler_db=Path("x"))
                self.assertEqual(state()[0], "open")
            with mock.patch.object(run, "latest_quote", return_value=next(quotes)):
                run.process_once(led, decision_db=Path("x"), crawler_db=Path("x"))
            self.assertEqual(tuple(state()), ("closed", "stop_loss"))


if __name__ == "__main__":
    unittest.main()
