"""Paper TP fill clamp + dead-pool close accounting.

Run from the repo root:

    python -m unittest execution.tests.test_tp_fill_clamp
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

from execution.executors.paper_executor import PaperExecutor  # noqa: E402
from execution.pricing import (  # noqa: E402
    limit_exit_fill_price,
    paper_close_min_liq_usd,
    paper_tp_clamp_enabled,
    sell_fill_price,
)
from paper_trade import tick as paper_tick  # noqa: E402

TP_CLAMP_ENV = "EXECUTION_PAPER_TP_CLAMP"
MIN_LIQ_ENV = "EXECUTION_PAPER_CLOSE_MIN_LIQ_USD"
CASH = 10_000.0


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _PriceDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(
            """
            CREATE TABLE pair_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                token_address TEXT,
                recorded_at TEXT NOT NULL,
                price_usd REAL,
                volume_h24 REAL,
                volume_h1 REAL,
                liquidity_usd REAL,
                txns_h24 INTEGER,
                tier TEXT
            );
            CREATE TABLE pairs (
                chain_id TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                token_address TEXT,
                raw_json TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain_id, pair_address)
            );
            """
        )
        self.set_quote(1.0, liquidity=1_000_000.0)

    def set_quote(self, price: float, liquidity: float = 1_000_000.0) -> None:
        now = _iso(datetime.now(timezone.utc))
        self.conn.execute("DELETE FROM pair_history")
        self.conn.execute("DELETE FROM pairs")
        self.conn.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, token_address, recorded_at,
                price_usd, liquidity_usd
            ) VALUES ('solana', 'pair1', 'token1', ?, ?, ?)
            """,
            (now, price, liquidity),
        )
        raw = json.dumps({"priceUsd": price, "liquidity": {"usd": liquidity}})
        self.conn.execute(
            """
            INSERT INTO pairs (
                chain_id, pair_address, token_address, raw_json, source, updated_at
            ) VALUES ('solana', 'pair1', 'token1', ?, 'test', ?)
            """,
            (raw, now),
        )
        self.conn.commit()


def _buy(ex: PaperExecutor, size_pct: float = 0.1, **extra: Any) -> dict[str, Any]:
    sig: dict[str, Any] = {
        "action": "buy",
        "chain_id": "solana",
        "pair_address": "pair1",
        "token_address": "token1",
        "size_pct": size_pct,
        "strategy_id": "strat_a",
    }
    sig.update(extra)
    return ex.execute(sig)


class LimitExitFillPriceUnitTests(unittest.TestCase):
    def test_mid_far_above_limit_clamps(self) -> None:
        fill, clamped, limit = limit_exit_fill_price(
            entry_price=1.0, tp_pct=10.0, mid=2.0, slip=0.01
        )
        self.assertTrue(clamped)
        self.assertAlmostEqual(limit, 1.1)
        self.assertAlmostEqual(fill, 1.1)

    def test_mid_below_limit_unchanged(self) -> None:
        mid = 1.05
        slip = 0.01
        expected = sell_fill_price(mid, slip)
        fill, clamped, limit = limit_exit_fill_price(
            entry_price=1.0, tp_pct=10.0, mid=mid, slip=slip
        )
        self.assertFalse(clamped)
        self.assertAlmostEqual(limit, 1.1)
        self.assertAlmostEqual(fill, expected)

    def test_tp_pct_none_no_clamp(self) -> None:
        fill, clamped, limit = limit_exit_fill_price(
            entry_price=1.0, tp_pct=None, mid=5.0, slip=0.02
        )
        self.assertFalse(clamped)
        self.assertIsNone(limit)
        self.assertAlmostEqual(fill, sell_fill_price(5.0, 0.02))

    def test_env_defaults(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TP_CLAMP_ENV, None)
            os.environ.pop(MIN_LIQ_ENV, None)
            self.assertTrue(paper_tp_clamp_enabled())
            self.assertAlmostEqual(paper_close_min_liq_usd(), 1000.0)
        with mock.patch.dict(os.environ, {TP_CLAMP_ENV: "0"}):
            self.assertFalse(paper_tp_clamp_enabled())
        with mock.patch.dict(os.environ, {MIN_LIQ_ENV: "0"}):
            self.assertAlmostEqual(paper_close_min_liq_usd(), 0.0)


class TpClampIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {
            k: os.environ.get(k)
            for k in (TP_CLAMP_ENV, MIN_LIQ_ENV, "EXECUTION_PAPER_SIZE_MULT")
        }
        for k in (TP_CLAMP_ENV, MIN_LIQ_ENV, "EXECUTION_PAPER_SIZE_MULT"):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.prices = _PriceDB(root / "crawler.db")
        self.ex = PaperExecutor(paper_db=root / "paper.db", crawler_db=self.prices.path)
        self.ex.ledger.set_cash(CASH)

    def tearDown(self) -> None:
        self.ex.close()
        self._tmp.cleanup()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_tp_mid_far_above_clamps_to_limit(self) -> None:
        opened = _buy(self.ex, 0.1, tp_pct=10.0)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        entry = float(self.ex.ledger.get_position(pid)["entry_price"])
        size = float(self.ex.ledger.get_position(pid)["size_usd"])
        # Huge mid → raw fill would be ~+100%, clamp to +10% limit.
        self.prices.set_quote(entry * 2.0, liquidity=1_000_000.0)
        actions = self.ex.tick()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertTrue(act["tp_clamped"])
        self.assertFalse(act["dead_pool_clipped"])
        limit = entry * 1.10
        self.assertAlmostEqual(float(act["tp_limit_price"]), limit)
        self.assertAlmostEqual(float(act["fill_price"]), limit)
        closed = self.ex.ledger.get_position(pid)
        self.assertEqual(closed["exit_reason"], "take_profit")
        self.assertAlmostEqual(float(closed["exit_price"]), limit)
        expected_pnl = float(closed["qty"]) * limit - size
        self.assertAlmostEqual(float(closed["pnl_usd"]), expected_pnl, places=6)

    def test_tp_mid_below_limit_no_clamp(self) -> None:
        opened = _buy(self.ex, 0.1, tp_pct=10.0)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        entry = float(self.ex.ledger.get_position(pid)["entry_price"])
        # Mid just above TP trigger but slipped fill still near mid.
        mid = entry * 1.101
        self.prices.set_quote(mid, liquidity=1_000_000.0)
        actions = self.ex.tick()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        # slip at 1M liq is 0.3%; fill = mid*(1-0.003) may be above or below limit
        limit = entry * 1.10
        raw = sell_fill_price(mid, 0.003)
        if raw > limit:
            self.assertTrue(act["tp_clamped"])
            self.assertAlmostEqual(float(act["fill_price"]), limit)
        else:
            self.assertFalse(act["tp_clamped"])
            self.assertAlmostEqual(float(act["fill_price"]), raw)

    def test_stop_loss_gap_not_clamped(self) -> None:
        opened = _buy(self.ex, 0.1, tp_pct=10.0, sl_pct=5.0)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        entry = float(self.ex.ledger.get_position(pid)["entry_price"])
        # Gap through SL to ~50% of entry — must book the worse fill, not clamp up.
        mid = entry * 0.50
        self.prices.set_quote(mid, liquidity=1_000_000.0)
        actions = self.ex.tick()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertEqual(act["reason"], "stop_loss")
        self.assertFalse(act["tp_clamped"])
        expected = sell_fill_price(mid, 0.003)
        self.assertAlmostEqual(float(act["fill_price"]), expected)
        closed = self.ex.ledger.get_position(pid)
        self.assertEqual(closed["exit_reason"], "stop_loss")
        self.assertAlmostEqual(float(closed["exit_price"]), expected)
        self.assertLess(float(closed["pnl_usd"]), 0.0)

    def test_max_hold_not_clamped(self) -> None:
        opened = _buy(self.ex, 0.1, tp_pct=50.0, sl_pct=50.0, max_hold_min=1.0)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        entry = float(self.ex.ledger.get_position(pid)["entry_price"])
        # Age the position past max_hold; mid mildly up but below TP.
        mid = entry * 1.05
        self.prices.set_quote(mid, liquidity=1_000_000.0)
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.ex.ledger.conn.execute(
            "UPDATE positions SET opened_at=? WHERE id=?",
            (_iso(past), pid),
        )
        actions = self.ex.tick()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertEqual(act["reason"], "max_hold")
        self.assertFalse(act["tp_clamped"])
        expected = sell_fill_price(mid, 0.003)
        self.assertAlmostEqual(float(act["fill_price"]), expected)

    def test_tp_clamp_env_off_restores_old_behavior(self) -> None:
        os.environ[TP_CLAMP_ENV] = "0"
        opened = _buy(self.ex, 0.1, tp_pct=10.0)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        entry = float(self.ex.ledger.get_position(pid)["entry_price"])
        mid = entry * 1.40
        self.prices.set_quote(mid, liquidity=1_000_000.0)
        actions = self.ex.tick()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertFalse(act["tp_clamped"])
        expected = sell_fill_price(mid, 0.003)
        self.assertAlmostEqual(float(act["fill_price"]), expected)
        # Move clearly above +10% limit
        self.assertGreater(expected, entry * 1.10)

    def test_dead_pool_flattens_at_entry(self) -> None:
        opened = _buy(self.ex, 0.1, tp_pct=10.0)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        entry = float(self.ex.ledger.get_position(pid)["entry_price"])
        size = float(self.ex.ledger.get_position(pid)["size_usd"])
        # Spike mid + dead liquidity (rug tip).
        self.prices.set_quote(entry * 10.0, liquidity=0.02)
        actions = self.ex.tick()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertTrue(act["dead_pool_clipped"])
        self.assertIn("dead_pool", str(act["reason"]))
        self.assertAlmostEqual(float(act["fill_price"]), entry)
        self.assertAlmostEqual(float(act["close_liq_usd"]), 0.02)
        closed = self.ex.ledger.get_position(pid)
        self.assertIn("dead_pool", str(closed["exit_reason"]))
        self.assertAlmostEqual(float(closed["exit_price"]), entry)
        self.assertAlmostEqual(float(closed["pnl_usd"]), 0.0, places=6)
        meta = json.loads(closed["meta_json"] or "{}")
        self.assertTrue(meta.get("dead_pool_clipped"))
        self.assertAlmostEqual(float(meta["close_liq_usd"]), 0.02)
        self.assertAlmostEqual(float(self.ex.ledger.get_cash()), CASH, places=4)
        self.assertAlmostEqual(size, 1000.0)

    def test_dead_pool_env_zero_disables(self) -> None:
        os.environ[MIN_LIQ_ENV] = "0"
        opened = _buy(self.ex, 0.1, tp_pct=10.0)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        entry = float(self.ex.ledger.get_position(pid)["entry_price"])
        self.prices.set_quote(entry * 1.5, liquidity=0.02)
        actions = self.ex.tick()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertFalse(act["dead_pool_clipped"])
        # TP clamp still applies (default on)
        self.assertTrue(act["tp_clamped"])
        self.assertAlmostEqual(float(act["fill_price"]), entry * 1.10)


if __name__ == "__main__":
    unittest.main()
