"""pair_history order-flow columns (txns m5/h1 buys/sells, volume m5/h1).

No network, no production DB. Missing values are NULL, never 0.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from history import TXN_FLOW_COLUMNS, extract_txn_flow, maybe_snapshot_pair
from store import Store

PAIR = {
    "chainId": "solana",
    "pairAddress": "flowpair1111111111111111111111111111111111",
    "baseToken": {"address": "flowmint1111111111111111111111111111111111"},
    "priceUsd": "0.01",
    "txns": {
        "m5": {"buys": 7, "sells": 3},
        "h1": {"buys": 50, "sells": 20},
        "h6": {"buys": 100, "sells": 90},
        "h24": {"buys": 400, "sells": 300},
    },
    "volume": {"m5": 1234.5, "h1": 9000, "h24": 100000},
    "liquidity": {"usd": 50000},
}

NEW_COLS = [c for c, _ in TXN_FLOW_COLUMNS]


class TxnFlowHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.store = Store(self.db)

    def tearDown(self) -> None:
        self.store.close() if hasattr(self.store, "close") else None
        self.tmp.cleanup()

    def _cols(self) -> list[str]:
        c = sqlite3.connect(self.db)
        try:
            return [r[1] for r in c.execute("PRAGMA table_info(pair_history)")]
        finally:
            c.close()

    def _last_row(self) -> dict:
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        try:
            r = c.execute("SELECT * FROM pair_history ORDER BY id DESC LIMIT 1").fetchone()
            return dict(r)
        finally:
            c.close()

    def test_columns_added_and_idempotent(self) -> None:
        cols = self._cols()
        for name in NEW_COLS + ["volume_h1"]:
            self.assertIn(name, cols)
        self.assertEqual(cols.count("volume_h1"), 1)
        # Re-open runs migrations again without error or duplicates.
        Store(self.db)
        self.store._migrate_txn_flow_columns()
        self.assertEqual(self._cols(), cols)

    def test_extract_full(self) -> None:
        self.assertEqual(
            extract_txn_flow(PAIR),
            {
                "txns_m5_buys": 7,
                "txns_m5_sells": 3,
                "txns_h1_buys": 50,
                "txns_h1_sells": 20,
                "volume_m5": 1234.5,
            },
        )

    def test_extract_missing_is_none_not_zero(self) -> None:
        out = extract_txn_flow({"txns": {"m5": {"buys": 2}}, "volume": {"h1": 5}})
        self.assertEqual(out["txns_m5_buys"], 2)
        self.assertIsNone(out["txns_m5_sells"])
        self.assertIsNone(out["txns_h1_buys"])
        self.assertIsNone(out["txns_h1_sells"])
        self.assertIsNone(out["volume_m5"])
        empty = extract_txn_flow({})
        self.assertTrue(all(v is None for v in empty.values()))
        bad = extract_txn_flow(
            {"txns": {"m5": {"buys": "x", "sells": -1}, "h1": "nope"}, "volume": {"m5": ""}}
        )
        self.assertTrue(all(v is None for v in bad.values()))

    def test_zero_is_kept_as_zero(self) -> None:
        out = extract_txn_flow({"txns": {"m5": {"buys": 0, "sells": 0}}, "volume": {"m5": 0}})
        self.assertEqual(out["txns_m5_buys"], 0)
        self.assertEqual(out["volume_m5"], 0.0)

    def test_snapshot_writes_flow(self) -> None:
        with mock.patch.object(config, "HISTORY_TXN_FLOW", True):
            self.assertTrue(maybe_snapshot_pair(self.store, "solana", PAIR["pairAddress"], PAIR))
        row = self._last_row()
        self.assertEqual(row["txns_m5_buys"], 7)
        self.assertEqual(row["txns_m5_sells"], 3)
        self.assertEqual(row["txns_h1_buys"], 50)
        self.assertEqual(row["txns_h1_sells"], 20)
        self.assertEqual(row["volume_m5"], 1234.5)
        self.assertEqual(row["volume_h1"], 9000)
        self.assertEqual(row["txns_h24"], 700)

    def test_snapshot_missing_writes_null(self) -> None:
        pair = {k: v for k, v in PAIR.items() if k not in ("txns", "volume")}
        with mock.patch.object(config, "HISTORY_TXN_FLOW", True):
            maybe_snapshot_pair(self.store, "solana", pair["pairAddress"], pair)
        row = self._last_row()
        for name in NEW_COLS + ["volume_h1"]:
            self.assertIsNone(row[name], name)

    def test_switch_off_writes_null(self) -> None:
        with mock.patch.object(config, "HISTORY_TXN_FLOW", False):
            maybe_snapshot_pair(self.store, "solana", PAIR["pairAddress"], PAIR)
        row = self._last_row()
        for name in NEW_COLS:
            self.assertIsNone(row[name], name)
        self.assertEqual(row["volume_h1"], 9000)  # pre-existing column unchanged

    def test_default_on(self) -> None:
        self.assertTrue(config.HISTORY_TXN_FLOW)


if __name__ == "__main__":
    unittest.main()
