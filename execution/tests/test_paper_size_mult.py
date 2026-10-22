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

from execution.executors.paper_executor import PaperExecutor  # noqa: E402

ENV = "EXECUTION_PAPER_SIZE_MULT"
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
        self._prev = os.environ.get(ENV)
        os.environ.pop(ENV, None)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        _PriceDB(root / "crawler.db")
        self.ex = PaperExecutor(paper_db=root / "paper.db", crawler_db=root / "crawler.db")
        self.ex.ledger.set_cash(CASH)

    def tearDown(self) -> None:
        self.ex.close()
        self._tmp.cleanup()
        if self._prev is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self._prev

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


if __name__ == "__main__":
    unittest.main()
