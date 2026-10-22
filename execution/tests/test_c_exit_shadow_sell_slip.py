"""C-exit shadow sells: fill uses sell slippage; the trigger stays on the mark.

Buy fills stay mid × (1 + slip). Direct close_position / partial_close record
the price they are given — only tick converts a mark into a slipped sell.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution import c_exit_shadow_run as run
from execution import price_confirm as pc
from execution.c_exit_shadow_ledger import CExitShadowLedger
from execution.pricing import buy_fill_price, sell_fill_price, slippage_rate


def _open(led: CExitShadowLedger, tag: str, *, entry: float = 1.0, size: float = 100.0, liq=None):
    meta = {} if liq is None else {"liquidity_usd": liq}
    return led._open_one(
        tag=tag,
        chain_id="solana",
        pair_address="pair",
        token_address=None,
        entry_price=entry,
        size_usd=size,
        tp_pct=10.0 if tag != "c_trail3_h60" else None,
        sl_pct=5.0,
        max_hold_min=60.0,
        source_decision_id=1,
        meta=meta,
        trail_pct=3.0 if tag == "c_trail3_h60" else None,
        partial_tp_pct=5.0 if tag == "c_tp5half_sl5_h60" else None,
        partial_frac=0.5 if tag == "c_tp5half_sl5_h60" else 0.0,
    )


class TestShadowSellSlip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = CExitShadowLedger(Path(self.tmp.name) / "c.db")

    def tearDown(self):
        self.led.close()
        self.tmp.cleanup()

    def test_sell_fill_below_mid_for_positive_slip(self):
        liq = 100_000.0  # 50k~500k → 1%
        slip = slippage_rate(liq)
        self.assertGreater(slip, 0.0)
        pid = _open(self.led, "c_sl5_h60", liq=liq)
        mid = 1.10
        closed = self.led.tick(lambda p: (mid, liq))
        self.assertEqual(len(closed), 1)
        fill = sell_fill_price(mid, slip)
        self.assertLess(fill, mid)
        row = self.led.conn.execute(
            "SELECT exit_price, exit_reason, pnl_usd FROM positions WHERE id=?", (pid,)
        ).fetchone()
        self.assertEqual(row["exit_reason"], "take_profit")
        self.assertAlmostEqual(row["exit_price"], fill)
        self.assertAlmostEqual(row["pnl_usd"], 100.0 * (fill - 1.0))
        self.assertAlmostEqual(closed[0]["mid_price"], mid)
        self.assertAlmostEqual(closed[0]["fill_price"], fill)
        self.assertAlmostEqual(closed[0]["slippage"], slip)
        self.assertAlmostEqual(closed[0]["liquidity_usd"], liq)
        sell = self.led.conn.execute(
            "SELECT price, notional_usd FROM fills WHERE position_id=? AND side='sell'",
            (pid,),
        ).fetchone()
        self.assertAlmostEqual(sell["price"], fill)
        self.assertAlmostEqual(sell["notional_usd"], 100.0 * fill)

    def test_trigger_stays_on_mid(self):
        liq = 10_000.0  # <50k → 2%. Slipped fill of 0.951 is ~0.932, below the 5% SL.
        pid = _open(self.led, "c_sl5_h60", liq=liq)
        self.assertEqual(self.led.tick(lambda p: (0.951, liq)), [])
        row = self.led.conn.execute(
            "SELECT status, exit_reason FROM positions WHERE id=?", (pid,)
        ).fetchone()
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["exit_reason"])

        # Just under the mark trigger: stop fires, fill is the slipped price.
        closed = self.led.tick(lambda p: (0.95, liq))
        self.assertEqual(closed[0]["reason"], "stop_loss")
        self.assertAlmostEqual(closed[0]["mid_price"], 0.95)
        self.assertAlmostEqual(closed[0]["fill_price"], sell_fill_price(0.95, slippage_rate(liq)))
        self.assertLess(closed[0]["fill_price"], 0.95)

    def test_tp_trigger_stays_on_mid(self):
        liq = 1_000_000.0  # ≥500k → 0.3%
        pid = _open(self.led, "c_sl5_h60", liq=liq)
        self.assertEqual(self.led.tick(lambda p: (1.099, liq)), [])
        self.assertEqual(
            self.led.conn.execute("SELECT status FROM positions WHERE id=?", (pid,)).fetchone()[0],
            "open",
        )
        closed = self.led.tick(lambda p: (1.10, liq))
        self.assertEqual(closed[0]["reason"], "take_profit")
        self.assertAlmostEqual(closed[0]["mid_price"], 1.10)
        self.assertAlmostEqual(
            closed[0]["fill_price"], sell_fill_price(1.10, slippage_rate(liq))
        )

    def test_partial_sell_uses_sell_fill(self):
        liq = 100_000.0
        slip = slippage_rate(liq)
        pid = _open(self.led, "c_tp5half_sl5_h60", liq=liq)
        # 4.9% is under the +5% partial trigger even though a 1% sell slip
        # would print a price well below the line. No fill yet.
        self.led.tick(lambda p: (1.049, liq))
        held = self.led.conn.execute(
            "SELECT status, qty, partial_done FROM positions WHERE id=?", (pid,)
        ).fetchone()
        self.assertEqual(held["status"], "open")
        self.assertEqual(held["partial_done"], 0)
        self.assertAlmostEqual(held["qty"], 100.0)

        self.led.tick(lambda p: (1.05, liq))
        row = self.led.conn.execute(
            "SELECT status, qty, partial_done, proceeds_usd FROM positions WHERE id=?",
            (pid,),
        ).fetchone()
        fill = sell_fill_price(1.05, slip)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["partial_done"], 1)
        self.assertAlmostEqual(row["qty"], 50.0)
        self.assertAlmostEqual(row["proceeds_usd"], 50.0 * fill)
        sell = self.led.conn.execute(
            "SELECT price, qty, notional_usd FROM fills WHERE position_id=? AND side='sell'",
            (pid,),
        ).fetchone()
        self.assertAlmostEqual(sell["price"], fill)
        self.assertAlmostEqual(sell["qty"], 50.0)
        self.assertLess(sell["price"], 1.05)
        self.assertAlmostEqual(sell["notional_usd"], 50.0 * fill)

    def test_current_liquidity_beats_entry_meta(self):
        entry_liq = 10_000.0  # 2%
        current_liq = 1_000_000.0  # 0.3%
        pid = _open(self.led, "c_sl5_h60", liq=entry_liq)
        mid = 1.10
        closed = self.led.tick(lambda p: (mid, current_liq))
        fill = sell_fill_price(mid, slippage_rate(current_liq))
        self.assertNotAlmostEqual(fill, sell_fill_price(mid, slippage_rate(entry_liq)))
        self.assertAlmostEqual(closed[0]["fill_price"], fill)
        self.assertAlmostEqual(closed[0]["slippage"], slippage_rate(current_liq))
        self.assertAlmostEqual(closed[0]["liquidity_usd"], current_liq)
        exit_px = self.led.conn.execute(
            "SELECT exit_price FROM positions WHERE id=?", (pid,)
        ).fetchone()["exit_price"]
        self.assertAlmostEqual(exit_px, fill)

    def test_missing_current_liquidity_uses_entry_meta(self):
        entry_liq = 1_000_000.0
        pid = _open(self.led, "c_sl5_h60", liq=entry_liq)
        mid = 0.94
        closed = self.led.tick(lambda p: (mid, None))
        fill = sell_fill_price(mid, slippage_rate(entry_liq))
        self.assertAlmostEqual(closed[0]["fill_price"], fill)
        self.assertAlmostEqual(closed[0]["slippage"], 0.003)
        self.assertNotAlmostEqual(fill, sell_fill_price(mid, slippage_rate(None)))
        self.assertEqual(
            self.led.conn.execute(
                "SELECT exit_reason FROM positions WHERE id=?", (pid,)
            ).fetchone()["exit_reason"],
            "stop_loss",
        )

    def test_mark_only_lookup_uses_entry_meta(self):
        """A float lookup (no current sample) still slips off stored entry liquidity."""
        entry_liq = 100_000.0
        _open(self.led, "c_sl5_h60", liq=entry_liq)
        mid = 1.10
        closed = self.led.tick(lambda p: mid)
        self.assertAlmostEqual(closed[0]["fill_price"], sell_fill_price(mid, slippage_rate(entry_liq)))
        self.assertAlmostEqual(closed[0]["mid_price"], mid)

    def test_direct_close_price_is_not_reslipped(self):
        pid = _open(self.led, "c_sl5_h60", liq=10_000.0)
        out = self.led.close_position(pid, price=1.0, reason="manual")
        self.assertAlmostEqual(out["exit_price"], 1.0)
        self.assertNotIn("slippage", out)

    def test_peak_tracks_mark_not_fill(self):
        pid = _open(self.led, "c_trail3_h60", liq=10_000.0)
        self.assertEqual(self.led.tick(lambda p: (1.20, 10_000.0)), [])
        peak = self.led.conn.execute(
            "SELECT peak_price, status FROM positions WHERE id=?", (pid,)
        ).fetchone()
        self.assertEqual(peak["status"], "open")
        self.assertAlmostEqual(peak["peak_price"], 1.20)


class TestBuyPathUnchanged(unittest.TestCase):
    def test_open_still_uses_buy_fill_price(self):
        with tempfile.TemporaryDirectory() as d:
            dec = Path(d) / "decision_shadow_entry_010.db"
            con = sqlite3.connect(dec)
            con.execute(
                "CREATE TABLE decision_log (id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT,"
                " action TEXT, chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,"
                " risk_passed INTEGER, mode TEXT, raw_json TEXT, purpose TEXT)"
            )
            raw = {
                "action": "buy",
                "chain_id": "solana",
                "pair_address": "pairbuy",
                "size_pct": 0.05,
                "purpose": "live_candidate",
                "ts": "2026-10-21T01:00:00Z",
                "extras": {"liq_bucket": "present", "budget_regime": "4h"},
            }
            con.execute(
                "INSERT INTO decision_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (1, raw["ts"], "s", "buy", "solana", "tok", 0.05, "", 1, "dry_run",
                 json.dumps(raw), "live_candidate"),
            )
            con.commit()
            con.close()
            mid = 2.0
            liq = 100_000.0
            slip = slippage_rate(liq)
            with mock.patch.object(run, "_latest_mid", return_value=(mid, liq)), \
                 CExitShadowLedger(Path(d) / "c.db") as led:
                out = run.process_once(
                    led, decision_db=dec, crawler_db=dec, accept_purposes=("live_candidate",)
                )
                self.assertGreater(out["opened"], 0)
                self.assertEqual(out["tick_closed"], [])
                rows = led.conn.execute(
                    "SELECT entry_price, meta_json FROM positions"
                ).fetchall()
                self.assertEqual(len(rows), 8)
                for row in rows:
                    self.assertAlmostEqual(row["entry_price"], buy_fill_price(mid, slip))
                    meta = json.loads(row["meta_json"])
                    self.assertAlmostEqual(meta["mid_price"], mid)
                    self.assertAlmostEqual(meta["slippage"], slip)
                    self.assertAlmostEqual(meta["liquidity_usd"], liq)
                    self.assertGreater(row["entry_price"], mid)

    def test_runner_close_prefers_quote_liquidity(self):
        entry_liq = 10_000.0
        quote_liq = 1_000_000.0
        mid = 0.94
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(run, "CONFIRMER", pc.StopPriceConfirmer()), \
             mock.patch.object(run, "_fetch_pending", return_value=[]), \
             mock.patch.object(run, "_open_ro"), \
             CExitShadowLedger(Path(d) / "c.db") as led:
            pid = _open(led, "c_sl5_h60", liq=entry_liq)
            quote = {"price_usd": mid, "liquidity_usd": quote_liq, "recorded_at": "t0"}
            with mock.patch.object(run, "latest_quote", return_value=quote):
                out = run.process_once(led, decision_db=Path("x"), crawler_db=Path("x"))
            self.assertEqual(len(out["tick_closed"]), 1)
            fill = sell_fill_price(mid, slippage_rate(quote_liq))
            closed = out["tick_closed"][0]
            self.assertAlmostEqual(closed["mid_price"], mid)
            self.assertAlmostEqual(closed["fill_price"], fill)
            self.assertAlmostEqual(closed["slippage"], slippage_rate(quote_liq))
            self.assertAlmostEqual(closed["liquidity_usd"], quote_liq)
            self.assertLess(fill, mid)
            row = led.conn.execute(
                "SELECT exit_price, exit_reason FROM positions WHERE id=?", (pid,)
            ).fetchone()
            self.assertEqual(row["exit_reason"], "stop_loss")
            self.assertAlmostEqual(row["exit_price"], fill)


if __name__ == "__main__":
    unittest.main()
