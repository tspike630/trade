"""C-exit 8-cell shadow: partial TP, trailing stop, size-mult switch."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution import c_exit_size_mult as sm
from execution.c_exit_grid import cell_by_tag, cell_exit_action, cells
from execution.c_exit_shadow_ledger import CExitShadowLedger
from execution.pricing import slippage_rate

EXPECTED = [
    "c_sl4_h60", "c_sl5_h60", "c_sl4_h90", "c_sl5_h90",
    "c_sl3_h60", "c_sl3_h30", "c_tp5half_sl5_h60", "c_trail3_h60",
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "c.db"
        self.led = CExitShadowLedger(self.db)

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def open(self, tag, entry=1.0, size=100.0, mult=1.0):
        c = cell_by_tag(tag)
        return self.led._open_one(
            tag=tag, chain_id="solana", pair_address="p", token_address=None,
            entry_price=entry, size_usd=size, tp_pct=c.tp_pct, sl_pct=c.sl_pct,
            max_hold_min=c.max_hold_min, source_decision_id=1, meta={},
            trail_pct=c.trail_pct, partial_tp_pct=c.partial_tp_pct,
            partial_frac=c.partial_frac, mult=mult,
        )

    def row(self, pid):
        return dict(self.led.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone())


class TestCells(Base):
    def test_eight_cells(self):
        self.assertEqual([c.tag for c in cells()], EXPECTED)
        self.assertEqual(set(self.led.status()["tags"]), set(EXPECTED))

    def test_original_semantics_unchanged(self):
        now = datetime.now(timezone.utc)
        pos = {"entry_price": 1.0, "tp_pct": 10.0, "sl_pct": 5.0, "max_hold_min": 60,
               "opened_at": now.isoformat()}
        self.assertEqual(cell_exit_action(pos, 0.95, now), ("close", "stop_loss"))
        self.assertEqual(cell_exit_action(pos, 1.10, now), ("close", "take_profit"))
        self.assertIsNone(cell_exit_action(pos, 1.05, now))
        self.assertEqual(cell_exit_action(pos, 1.0, now + timedelta(minutes=61)), ("close", "max_hold"))

    def test_sl3_h30(self):
        pid = self.open("c_sl3_h30")
        self.led.tick(lambda p: 0.97)
        self.assertEqual(self.row(pid)["exit_reason"], "stop_loss")


class TestPartial(Base):
    def test_half_then_tp(self):
        pid = self.open("c_tp5half_sl5_h60")
        self.led.tick(lambda p: 1.05)
        r = self.row(pid)
        self.assertEqual(r["status"], "open")
        self.assertAlmostEqual(r["qty"], 50.0)
        self.assertEqual(r["partial_done"], 1)
        self.led.tick(lambda p: 1.06)  # no second partial
        self.assertAlmostEqual(self.row(pid)["qty"], 50.0)
        self.led.tick(lambda p: 1.10)
        r = self.row(pid)
        self.assertEqual(r["exit_reason"], "take_profit")
        # Empty meta → unknown liquidity → sell slip. Triggers stayed on the marks above.
        slip = slippage_rate(None)
        partial_fill = 1.05 * (1.0 - slip)
        exit_fill = 1.10 * (1.0 - slip)
        pnl = 50 * (partial_fill - 1.0) + 50 * (exit_fill - 1.0)
        self.assertAlmostEqual(r["pnl_usd"], pnl)
        self.assertAlmostEqual(r["exit_price"], exit_fill)
        self.assertAlmostEqual(self.led.get_cash("c_tp5half_sl5_h60"), 10_000 + pnl)

    def test_half_then_sl(self):
        pid = self.open("c_tp5half_sl5_h60")
        self.led.tick(lambda p: 1.05)
        self.led.tick(lambda p: 0.95)
        r = self.row(pid)
        self.assertEqual(r["exit_reason"], "stop_loss")
        slip = slippage_rate(None)
        pnl = 50 * (1.05 * (1.0 - slip) - 1.0) + 50 * (0.95 * (1.0 - slip) - 1.0)
        self.assertAlmostEqual(r["pnl_usd"], pnl)


class TestTrail(Base):
    def test_trailing(self):
        pid = self.open("c_trail3_h60")
        self.led.tick(lambda p: 1.20)  # no fixed TP
        self.assertEqual(self.row(pid)["status"], "open")
        self.assertAlmostEqual(self.row(pid)["peak_price"], 1.20)
        self.led.tick(lambda p: 1.17)  # -2.5% from peak
        self.assertEqual(self.row(pid)["status"], "open")
        self.led.tick(lambda p: 1.16)  # -3.3%
        r = self.row(pid)
        self.assertEqual(r["exit_reason"], "trailing_stop")
        slip = slippage_rate(None)
        self.assertAlmostEqual(r["peak_price"], 1.20)
        self.assertAlmostEqual(r["pnl_usd"], 100 * (1.16 * (1.0 - slip) - 1.0))
        self.assertLess(r["exit_price"], 1.16)

    def test_floor_sl(self):
        pid = self.open("c_trail3_h60")
        self.led.tick(lambda p: 0.98)  # below entry but peak==entry, no trail
        self.assertEqual(self.row(pid)["status"], "open")
        self.led.tick(lambda p: 0.95)
        self.assertEqual(self.row(pid)["exit_reason"], "stop_loss")


class TestSizeMult(Base):
    def test_default_off(self):
        for c in cells():
            st = sm.get_state(self.led.conn, c.tag)
            self.assertFalse(st["enabled"])
            self.assertEqual(st["mult"], 1.0)
            self.assertEqual(sm.effective_mult(self.led.conn, c.tag), 1.0)

    def test_disabled_mult_ignored(self):
        sm.set_state(self.led.conn, "c_sl5_h60", mult=2.0, enabled=False, reason="t")
        self.assertEqual(sm.effective_mult(self.led.conn, "c_sl5_h60"), 1.0)

    def test_invalid_mult(self):
        with self.assertRaises(ValueError):
            sm.set_state(self.led.conn, "c_sl5_h60", mult=5.0, enabled=True, reason="t")

    def test_capped_size(self):
        self.assertEqual(sm.capped_size(1000, 1.0, 5.0), 1000)
        self.assertEqual(sm.capped_size(1000, 1.5, 5.0), 1500)
        # 3x * 2000 = 6000 -> 6000*5% = 300 = cap, ok; 3x*3000 clipped to 6000
        self.assertEqual(sm.capped_size(3000, 3.0, 5.0), 6000)

    def test_auto_revert_single(self):
        sm.set_state(self.led.conn, "c_sl5_h60", mult=2.0, enabled=True, reason="t")
        self.assertEqual(sm.effective_mult(self.led.conn, "c_sl5_h60"), 2.0)
        pid = self.open("c_sl5_h60", size=4000.0, mult=2.0)
        self.led.close_position(pid, price=0.95, reason="stop_loss")  # -200
        st = sm.get_state(self.led.conn, "c_sl5_h60")
        self.assertFalse(st["enabled"])
        self.assertEqual(st["mult"], 1.0)
        self.assertIn("single_loss_cap", st["last_reason"])

    def test_auto_revert_drawdown(self):
        sm.set_state(self.led.conn, "c_sl4_h60", mult=1.5, enabled=True, reason="t")
        for _ in range(2):
            pid = self.open("c_sl4_h60", size=2500.0, mult=1.5)
            self.led.close_position(pid, price=0.96, reason="stop_loss")  # -100 each
        st = sm.get_state(self.led.conn, "c_sl4_h60")
        self.assertFalse(st["enabled"])
        self.assertIn("drawdown", st["last_reason"])


class TestMigration(unittest.TestCase):
    def test_old_db_gets_new_cells(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.db"
            con = sqlite3.connect(p)
            con.executescript(
                "CREATE TABLE positions (id TEXT PRIMARY KEY, grid_tag TEXT NOT NULL, chain_id TEXT NOT NULL,"
                " pair_address TEXT NOT NULL, token_address TEXT, qty REAL NOT NULL, entry_price REAL NOT NULL,"
                " size_usd REAL NOT NULL, opened_at TEXT NOT NULL, status TEXT NOT NULL, tp_pct REAL, sl_pct REAL,"
                " max_hold_min REAL, exit_price REAL, closed_at TEXT, exit_reason TEXT, pnl_usd REAL,"
                " meta_json TEXT, source_decision_id INTEGER);"
            )
            con.close()
            with CExitShadowLedger(p) as led:
                cols = {r[1] for r in led.conn.execute("PRAGMA table_info(positions)")}
                self.assertTrue({"trail_pct", "partial_done", "proceeds_usd", "size_mult"} <= cols)
                self.assertEqual(len(led.status()["tags"]), 8)


if __name__ == "__main__":
    unittest.main()
