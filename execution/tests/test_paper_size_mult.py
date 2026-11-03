"""Main-book paper buy notional multiplier. Live and c_exit shadow are untouched.

Run from the repo root:

    python -m unittest execution.tests.test_paper_size_mult
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

from execution.executors.paper_executor import (  # noqa: E402
    PaperExecutor,
    cap_requested_notional_usd,
    close_pnl_jump_verdict,
    paper_hard_cap_usd,
)

ENV = "EXECUTION_PAPER_SIZE_MULT"
HARD_ENV = "EXECUTION_PAPER_MAX_NOTIONAL_USD"
LIQ_ENV = "EXECUTION_PAPER_MAX_LIQ_FRAC"
JUMP_ENV = "EXECUTION_PAPER_JUMP_RATIO"
CASH = 10_000.0


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _PriceDB:
    def __init__(self, path: Path) -> None:
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
        now = _iso(datetime.now(timezone.utc))
        self.conn.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, token_address, recorded_at, price_usd
            ) VALUES ('solana', 'pair1', 'token1', ?, 2.0)
            """,
            (now,),
        )
        raw = json.dumps({"priceUsd": 2.0, "liquidity": {"usd": 1_000_000}})
        self.conn.execute(
            """
            INSERT INTO pairs (
                chain_id, pair_address, token_address, raw_json, source, updated_at
            ) VALUES ('solana', 'pair1', 'token1', ?, 'test', ?)
            """,
            (raw, now),
        )
        self.conn.commit()
        self.conn.close()


def _buy(ex: PaperExecutor, size_pct: float) -> dict[str, Any]:
    return ex.execute(
        {
            "action": "buy",
            "chain_id": "solana",
            "pair_address": "pair1",
            "token_address": "token1",
            "size_pct": size_pct,
            "strategy_id": "strat_a",
        }
    )


class PaperSizeMultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {k: os.environ.get(k) for k in (ENV, HARD_ENV, LIQ_ENV, JUMP_ENV)}
        for k in (ENV, HARD_ENV, LIQ_ENV, JUMP_ENV):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.crawler_db = root / "crawler.db"
        _PriceDB(self.crawler_db)
        self.ex = PaperExecutor(paper_db=root / "paper.db", crawler_db=self.crawler_db)
        self.ex.ledger.set_cash(CASH)

    def tearDown(self) -> None:
        self.ex.close()
        self._tmp.cleanup()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _meta(self, result: dict[str, Any]) -> dict[str, Any]:
        self.assertTrue(result["ok"], result)
        pid = result["position_id"]
        stored = json.loads(self.ex.ledger.get_position(pid)["meta_json"])
        self.assertEqual(stored["paper_size_mult"], result["paper_size_mult"])
        self.assertEqual(
            stored["requested_usd_before_mult"], result["requested_usd_before_mult"]
        )
        self.assertEqual(stored["requested_usd"], result["requested_usd"])
        self.assertEqual(stored["size_pct"], result["meta"]["size_pct"])
        return stored

    def test_unset_mult_is_one(self) -> None:
        result = _buy(self.ex, 0.1)
        meta = self._meta(result)
        self.assertAlmostEqual(result["requested_usd_before_mult"], 1000.0)
        self.assertAlmostEqual(result["requested_usd"], 1000.0)
        self.assertAlmostEqual(result["size_usd"], 1000.0)
        self.assertEqual(result["paper_size_mult"], 1.0)
        self.assertFalse(result["truncated"])
        self.assertAlmostEqual(meta["size_pct"], 0.1)
        self.assertAlmostEqual(self.ex.ledger.get_cash(), 9000.0)

    def test_mult_two_doubles_requested_before_cash_cap(self) -> None:
        os.environ[ENV] = "2"
        result = _buy(self.ex, 0.1)
        meta = self._meta(result)
        self.assertAlmostEqual(result["requested_usd_before_mult"], 1000.0)
        self.assertAlmostEqual(result["requested_usd"], 2000.0)
        self.assertAlmostEqual(result["size_usd"], 2000.0)
        self.assertEqual(result["paper_size_mult"], 2.0)
        self.assertFalse(result["truncated"])
        self.assertAlmostEqual(meta["size_pct"], 0.1)
        self.assertAlmostEqual(self.ex.ledger.get_cash(), 8000.0)

    def test_mult_two_still_truncates_to_cash(self) -> None:
        os.environ[ENV] = "2"
        result = _buy(self.ex, 0.8)
        self._meta(result)
        self.assertAlmostEqual(result["requested_usd_before_mult"], 8000.0)
        self.assertAlmostEqual(result["requested_usd"], 16000.0)
        self.assertAlmostEqual(result["size_usd"], CASH)
        self.assertEqual(result["paper_size_mult"], 2.0)
        self.assertTrue(result["truncated"])
        self.assertAlmostEqual(self.ex.ledger.get_cash(), 0.0)
        self.assertGreaterEqual(self.ex.ledger.get_cash(), -1e-9)

    def _reset_flat_book(self) -> None:
        self.ex.ledger.conn.execute("DELETE FROM positions")
        self.ex.ledger.conn.execute("DELETE FROM fills")
        self.ex.ledger.set_cash(CASH)

    def test_invalid_mult_falls_back_to_one(self) -> None:
        for raw in ("", "0", "-1", "nope", "nan", "inf"):
            with self.subTest(raw=raw):
                os.environ[ENV] = raw
                self._reset_flat_book()
                result = _buy(self.ex, 0.1)
                self._meta(result)
                self.assertEqual(result["paper_size_mult"], 1.0)
                self.assertAlmostEqual(result["requested_usd"], 1000.0)
                self.assertAlmostEqual(result["size_usd"], 1000.0)

    def test_zero_cash_records_mult_on_the_reject(self) -> None:
        os.environ[ENV] = "2"
        opened = _buy(self.ex, 0.1)
        self.assertTrue(opened["ok"], opened)
        self.ex.ledger.set_cash(0.0)
        rejected = _buy(self.ex, 0.1)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"], "insufficient_cash")
        self.assertEqual(rejected["paper_size_mult"], 2.0)
        self.assertGreater(rejected["requested_usd_before_mult"], 0.0)
        self.assertAlmostEqual(
            rejected["requested_usd"], rejected["requested_usd_before_mult"] * 2.0
        )
        self.assertAlmostEqual(self.ex.ledger.get_cash(), 0.0)

    def test_sell_ignores_mult(self) -> None:
        opened = _buy(self.ex, 0.1)
        self.assertTrue(opened["ok"], opened)
        opened_size = float(self.ex.ledger.get_position(opened["position_id"])["size_usd"])
        os.environ[ENV] = "2"
        sold = self.ex.execute(
            {
                "action": "sell",
                "chain_id": "solana",
                "pair_address": "pair1",
                "token_address": "token1",
                "size_pct": 1.0,
                "strategy_id": "strat_a",
            }
        )
        self.assertTrue(sold["ok"], sold)
        self.assertNotIn("paper_size_mult", sold)
        closed = self.ex.ledger.get_position(opened["position_id"])
        self.assertEqual(closed["status"], "closed")
        self.assertAlmostEqual(float(closed["size_usd"]), opened_size)
        self.assertAlmostEqual(opened_size, 1000.0)



class PaperNotionalCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {k: os.environ.get(k) for k in (ENV, HARD_ENV, LIQ_ENV, JUMP_ENV)}
        for k in (ENV, HARD_ENV, LIQ_ENV, JUMP_ENV):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.crawler_db = root / "crawler.db"
        _PriceDB(self.crawler_db)
        self.ex = PaperExecutor(paper_db=root / "paper.db", crawler_db=self.crawler_db)
        self.ex.ledger.set_cash(CASH)

    def tearDown(self) -> None:
        self.ex.close()
        self._tmp.cleanup()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set_pair(self, *, price: float = 2.0, liquidity: float | None = 1_000_000.0) -> None:
        now = _iso(datetime.now(timezone.utc))
        conn = sqlite3.connect(str(self.crawler_db))
        conn.execute("DELETE FROM pair_history")
        conn.execute("DELETE FROM pairs")
        conn.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, token_address, recorded_at, price_usd
            ) VALUES ('solana', 'pair1', 'token1', ?, ?)
            """,
            (now, price),
        )
        raw_obj: dict[str, Any] = {"priceUsd": price}
        if liquidity is not None:
            raw_obj["liquidity"] = {"usd": liquidity}
        conn.execute(
            """
            INSERT INTO pairs (
                chain_id, pair_address, token_address, raw_json, source, updated_at
            ) VALUES ('solana', 'pair1', 'token1', ?, 'test', ?)
            """,
            (json.dumps(raw_obj), now),
        )
        conn.commit()
        conn.close()

    def test_hard_cap_default_20000(self) -> None:
        self.assertAlmostEqual(paper_hard_cap_usd(), 20_000.0)

    def test_mult20_huge_equity_cannot_exceed_hard_cap(self) -> None:
        os.environ[ENV] = "20"
        # Inject huge equity via cash; size_pct=0.05 → before_mult=5e5 → *20=1e7
        self.ex.ledger.set_cash(10_000_000.0)
        result = _buy(self.ex, 0.05)
        self.assertTrue(result["ok"], result)
        self.assertAlmostEqual(result["requested_usd_before_mult"], 500_000.0)
        self.assertEqual(result["paper_size_mult"], 20.0)
        self.assertAlmostEqual(result["requested_usd"], 20_000.0)
        self.assertAlmostEqual(result["size_usd"], 20_000.0)
        self.assertTrue(result["notional_capped"])
        self.assertAlmostEqual(result["hard_cap_usd"], 20_000.0)
        self.assertLessEqual(result["requested_usd"], result["hard_cap_usd"] + 1e-9)

    def test_hard_cap_env_override(self) -> None:
        os.environ[ENV] = "20"
        os.environ[HARD_ENV] = "5000"
        self.ex.ledger.set_cash(10_000_000.0)
        result = _buy(self.ex, 0.05)
        self.assertTrue(result["ok"], result)
        self.assertAlmostEqual(result["requested_usd"], 5_000.0)
        self.assertAlmostEqual(result["hard_cap_usd"], 5_000.0)

    def test_liq_frac_caps_notional(self) -> None:
        os.environ[ENV] = "20"
        os.environ[LIQ_ENV] = "0.25"
        self._set_pair(price=2.0, liquidity=10_000.0)  # liq cap = 2500
        self.ex.ledger.set_cash(100_000.0)
        result = _buy(self.ex, 0.05)
        self.assertTrue(result["ok"], result)
        # before: 0.05*100k*20=100k; hard=20k; liq=2500 → 2500
        self.assertAlmostEqual(result["requested_usd"], 2_500.0)
        self.assertAlmostEqual(result["liq_cap_usd"], 2_500.0)
        self.assertTrue(result["notional_capped"])

    def test_missing_liquidity_rejects_open(self) -> None:
        self._set_pair(price=2.0, liquidity=None)
        result = _buy(self.ex, 0.05)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_liquidity")
        self.assertEqual(len(self.ex.ledger.list_open()), 0)

    def test_cap_helper_min_formula(self) -> None:
        capped, audit = cap_requested_notional_usd(
            requested_usd=1_000_000.0,
            equity_usd=10_000_000.0,
            size_pct=0.05,
            mult=20.0,
            liquidity_usd=100_000.0,
            hard_cap_usd=20_000.0,
            liq_frac=0.25,
        )
        self.assertAlmostEqual(capped, 20_000.0)  # hard binds before liq 25k
        self.assertTrue(audit["notional_capped"])


class PaperJumpRejectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {k: os.environ.get(k) for k in (ENV, HARD_ENV, LIQ_ENV, JUMP_ENV)}
        for k in (ENV, HARD_ENV, LIQ_ENV, JUMP_ENV):
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.crawler_db = root / "crawler.db"
        _PriceDB(self.crawler_db)
        self.ex = PaperExecutor(paper_db=root / "paper.db", crawler_db=self.crawler_db)
        self.ex.ledger.set_cash(CASH)

    def tearDown(self) -> None:
        self.ex.close()
        self._tmp.cleanup()
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set_price(self, price: float, liquidity: float = 1_000_000.0) -> None:
        now = _iso(datetime.now(timezone.utc))
        conn = sqlite3.connect(str(self.crawler_db))
        conn.execute("DELETE FROM pair_history")
        conn.execute("DELETE FROM pairs")
        conn.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, token_address, recorded_at, price_usd
            ) VALUES ('solana', 'pair1', 'token1', ?, ?)
            """,
            (now, price),
        )
        raw = json.dumps({"priceUsd": price, "liquidity": {"usd": liquidity}})
        conn.execute(
            """
            INSERT INTO pairs (
                chain_id, pair_address, token_address, raw_json, source, updated_at
            ) VALUES ('solana', 'pair1', 'token1', ?, 'test', ?)
            """,
            (raw, now),
        )
        conn.commit()
        conn.close()

    def test_verdict_ratio_50x(self) -> None:
        reject, why = close_pnl_jump_verdict(
            entry_price=1.0,
            exit_price=51.0,
            size_usd=1000.0,
            qty=1000.0,
            equity_usd=10_000.0,
        )
        self.assertTrue(reject)
        self.assertIn("jump_reject:ratio", why)

    def test_verdict_ratio_reciprocal(self) -> None:
        reject, why = close_pnl_jump_verdict(
            entry_price=1.0,
            exit_price=0.01,
            size_usd=1000.0,
            qty=1000.0,
            equity_usd=10_000.0,
        )
        self.assertTrue(reject)
        self.assertIn("jump_reject:ratio", why)

    def test_verdict_pnl_hard_cap(self) -> None:
        # ratio 2x < 50, but pnl = +30k > hard 20k
        reject, why = close_pnl_jump_verdict(
            entry_price=1.0,
            exit_price=2.0,
            size_usd=30_000.0,
            qty=30_000.0,
            equity_usd=1_000_000.0,
            hard_cap_usd=20_000.0,
        )
        self.assertTrue(reject)
        self.assertIn("pnl_hard_cap", why)

    def test_verdict_pnl_gt_equity(self) -> None:
        reject, why = close_pnl_jump_verdict(
            entry_price=1.0,
            exit_price=3.0,
            size_usd=5_000.0,
            qty=5_000.0,
            equity_usd=8_000.0,  # pnl=+10k > 8k equity; ratio=3 < 50
            hard_cap_usd=100_000.0,
        )
        self.assertTrue(reject)
        self.assertIn("pnl_gt_equity", why)

    def test_verdict_ok_normal(self) -> None:
        reject, why = close_pnl_jump_verdict(
            entry_price=1.0,
            exit_price=1.1,
            size_usd=1_000.0,
            qty=1_000.0,
            equity_usd=10_000.0,
        )
        self.assertFalse(reject)
        self.assertEqual(why, "")

    def test_signal_sell_rejects_absurd_jump(self) -> None:
        opened = _buy(self.ex, 0.1)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        cash_before = float(self.ex.ledger.get_cash())
        # ~100x mid jump (entry fill ≈ 2*1.003)
        self._set_price(200.0)
        sold = self.ex.execute(
            {
                "action": "sell",
                "chain_id": "solana",
                "pair_address": "pair1",
                "token_address": "token1",
                "size_pct": 1.0,
                "strategy_id": "strat_a",
            }
        )
        self.assertFalse(sold["ok"], sold)
        self.assertIn("jump_reject", str(sold.get("error")))
        pos = self.ex.ledger.get_position(pid)
        self.assertEqual(pos["status"], "open")
        self.assertAlmostEqual(float(self.ex.ledger.get_cash()), cash_before)

    def test_tick_clips_absurd_jump_flat(self) -> None:
        opened = _buy(self.ex, 0.1)
        self.assertTrue(opened["ok"], opened)
        pid = opened["position_id"]
        pos = self.ex.ledger.get_position(pid)
        entry = float(pos["entry_price"])
        size = float(pos["size_usd"])
        # Force take_profit via huge mid; jump guard should flat-close.
        self._set_price(entry * 100.0)
        # shorten max_hold so we don't depend solely on TP path — TP will fire first
        actions = self.ex.tick()
        self.assertTrue(actions, "expected a tick close")
        closed = self.ex.ledger.get_position(pid)
        self.assertEqual(closed["status"], "closed")
        self.assertIn("jump_reject", str(closed.get("exit_reason") or ""))
        self.assertAlmostEqual(float(closed["exit_price"]), entry)
        self.assertAlmostEqual(float(closed["pnl_usd"]), 0.0, places=6)
        # cash restored to start
        self.assertAlmostEqual(float(self.ex.ledger.get_cash()), CASH, places=4)
        self.assertAlmostEqual(size, 1000.0)



if __name__ == "__main__":
    unittest.main()
