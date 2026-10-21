"""Open paper positions pinned hot. Temp DBs only; no network."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import position_pin
from pipeline import classify_token_tier


def _mk(path: Path, rows, with_token=True):
    c = sqlite3.connect(path)
    tok = ", token_address TEXT" if with_token else ""
    c.execute(f"CREATE TABLE positions (id TEXT, chain_id TEXT, pair_address TEXT{tok}, status TEXT)")
    for r in rows:
        c.execute(f"INSERT INTO positions VALUES ({','.join('?'*len(r))})", r)
    c.commit()
    c.close()


class DbStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"PIN_OPEN_POSITIONS_HOT": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _load(self, dbs):
        with self.assertLogs(position_pin.log, level="INFO") as cm:
            position_pin.load_pins(base_dir=self.d, dbs=dbs)
        return [m for m in cm.output if m.startswith("WARNING")]

    def test_missing_file_warns(self):
        w = self._load(("nope.db",))
        self.assertTrue(any("nope.db" in m and "not found" in m for m in w))
        self.assertEqual(position_pin.last_status, {"nope.db": "missing"})
        self.assertEqual(position_pin.last_failed_count(), 1)
        self.assertFalse((self.d / "nope.db").exists())

    def test_no_positions_table_warns(self):
        c = sqlite3.connect(self.d / "x.db"); c.execute("CREATE TABLE other (a)"); c.commit(); c.close()
        w = self._load(("x.db",))
        self.assertTrue(any("x.db" in m and "no positions table" in m for m in w))
        self.assertEqual(position_pin.last_status["x.db"], "no_table")

    def test_missing_columns_warns(self):
        c = sqlite3.connect(self.d / "y.db"); c.execute("CREATE TABLE positions (id TEXT, token_address TEXT)"); c.commit(); c.close()
        w = self._load(("y.db",))
        self.assertTrue(any("y.db" in m and "status" in m and "chain_id" in m for m in w))
        self.assertEqual(position_pin.last_status["y.db"], "missing_cols")

    def test_normal_db_counts(self):
        _mk(self.d / "a.db", [("1", "solana", "P1", "T1", "open"), ("2", "solana", "P2", "T2", "open"), ("3", "solana", "P3", "T3", "closed")])
        _mk(self.d / "b.db", [("4", "bsc", "P4", "open")], with_token=False)
        w = self._load(("a.db", "b.db"))
        self.assertEqual(w, [])
        self.assertEqual(position_pin.last_status, {"a.db": 2, "b.db": 1})
        self.assertEqual(position_pin.last_failed_count(), 0)

    def test_startup_info_once(self):
        _mk(self.d / "a.db", [])
        with mock.patch.object(position_pin, "_startup_logged", False):
            with self.assertLogs(position_pin.log, level="INFO") as cm:
                position_pin.load_pins(base_dir=self.d, dbs=("a.db", "gone.db"))
                position_pin.load_pins(base_dir=self.d, dbs=("a.db", "gone.db"))
        starts = [m for m in cm.output if "startup" in m]
        self.assertEqual(len(starts), 2)
        self.assertTrue(any("a.db found" in m for m in starts))
        self.assertTrue(any("gone.db missing" in m for m in starts))


class LoadPinsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        _mk(self.d / "paper_trading.db", [("1", "solana", "PairA", "TokA", "open"), ("2", "solana", "PairB", "TokB", "closed")])
        _mk(self.d / "paper_trading_c_exit.db", [("3", "solana", "PairC", "open")], with_token=False)
        # third DB missing on purpose → skipped

    def tearDown(self):
        self.tmp.cleanup()

    def test_open_only_and_missing_db_skipped(self):
        with mock.patch.dict(os.environ, {"PIN_OPEN_POSITIONS_HOT": "1"}):
            pairs, toks = position_pin.load_pins(base_dir=self.d)
        self.assertIn(("solana", "PairA"), pairs)
        self.assertIn(("solana", "PairC"), pairs)
        self.assertNotIn(("solana", "PairB"), pairs)
        self.assertIn(("solana", "TokA"), toks)
        self.assertNotIn(("solana", "TokB"), toks)

    def test_readonly_does_not_create(self):
        with mock.patch.dict(os.environ, {"PIN_OPEN_POSITIONS_HOT": "1"}):
            position_pin.load_pins(base_dir=self.d)
        self.assertFalse((self.d / "paper_trading_c_exit_shadow010.db").exists())

    def test_disabled_returns_empty(self):
        with mock.patch.dict(os.environ, {"PIN_OPEN_POSITIONS_HOT": "0"}):
            self.assertEqual(position_pin.load_pins(base_dir=self.d), (frozenset(), frozenset()))
            self.assertFalse(position_pin.is_pinned("solana", "TokA"))


class ClassifyPinnedTests(unittest.TestCase):
    def test_pinned_stale_token_is_hot(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {"has_pairs": 1, "last_enriched_at": old, "last_seen_at": old}
        self.assertEqual(classify_token_tier(row, cycle_number=1, now=now, pinned=True), "hot")
        self.assertIsNone(classify_token_tier(row, cycle_number=1, now=now, pinned=False))


if __name__ == "__main__":
    unittest.main()
