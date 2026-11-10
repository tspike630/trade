"""Spot book labels at the current ledger contract.

``execution_log`` keeps the first receipt. Later base qty is
``binance_spot_positions.quantity``. ``confirm_binance_spot_qty`` promotes
``qty_unconfirmed`` to ``open``. A verified OCO is ``status=oco_working`` and
``protection=exchange_oco``. ``reconcile_resting_entry`` can write
``closed_canceled``, but the maintenance tick does not set that flag, so a
cancel is shown only when the row already says so.

Nothing here calls Binance or opens a paper database that is missing.
The module lives outside ``dashboard/`` so the production read-only scan,
which still checks every other ``*.py`` in that package, does not see these
temporary-database statements.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dashboard.data import DataStore, DbPaths, connect_ro
from dashboard.parsing import rejection_key
from dashboard.spot_book import spot_book_view


def _receipt(
    *,
    status: str,
    error: str | None,
    order_qty: str | None,
    filled_qty: str | None = None,
    entry_order_id: str | None = "91001",
    arm_status: str = "qty_unconfirmed",
) -> str:
    result: dict[str, object] = {}
    if order_qty is not None:
        result["order"] = {"type": "LIMIT_MAKER", "quantity": order_qty}
    if filled_qty is not None:
        result["filled_qty"] = filled_qty
    if entry_order_id is not None:
        result["arm_position"] = {
            "status": arm_status,
            "quantity": None,
            "protection": "held_position_exit",
            "entry_order_id": entry_order_id,
        }
    body: dict[str, object] = {
        "step": "binance_spot",
        "status": status,
        "exec_result": result,
    }
    if error:
        body["error"] = error
    return json.dumps(body)


class FixtureLocationTests(unittest.TestCase):
    def test_fixture_module_is_outside_the_dashboard_package(self) -> None:
        import dashboard

        here = Path(__file__).resolve()
        package = Path(dashboard.__file__).resolve().parent
        self.assertNotIn(package, here.parents)
        text = here.read_text(encoding="utf-8")
        self.assertIn("sqlite3.connect", text)
        self.assertIn("INSERT INTO execution_log", text)
        self.assertIn("CREATE TABLE binance_spot_positions", text)
        self.assertIn("tempfile.TemporaryDirectory", text)


class SpotBookViewTests(unittest.TestCase):
    def test_new_zero_fill_is_not_a_rejection(self) -> None:
        receipt = json.loads(
            _receipt(status="live_refused", error="parent_fill_incomplete", order_qty="1.00")
        )
        position = {
            "status": "qty_unconfirmed",
            "quantity": None,
            "protection": "held_position_exit",
            "entry_order_id": "91001",
        }
        view = spot_book_view(receipt, "rejected", position, position_table=True)
        self.assertIsNotNone(view)
        assert view is not None
        self.assertFalse(view.is_rejection)
        self.assertEqual(view.label, "已接受未成交")
        self.assertEqual(view.filled_text, "未入账")
        self.assertEqual(view.order_text, "1.00")
        self.assertIn("无交易所OCO", view.protection_text)
        self.assertNotIn("整单", view.summary)
        self.assertNotIn("拒绝", view.summary)
        self.assertIsNone(
            rejection_key(1, "", "rejected", receipt, spot_position=position, spot_table=True)
        )

    def test_stale_ack_qty_does_not_override_empty_book(self) -> None:
        receipt = json.loads(
            _receipt(
                status="live_refused",
                error="parent_fill_incomplete",
                order_qty="1.00",
                filled_qty="0.40",
            )
        )
        position = {
            "status": "qty_unconfirmed",
            "quantity": None,
            "protection": "held_position_exit",
            "entry_order_id": "91001",
        }
        view = spot_book_view(receipt, "rejected", position, position_table=True)
        assert view is not None
        self.assertEqual(view.label, "已接受未成交")
        self.assertEqual(view.filled_text, "未入账")
        self.assertNotIn("0.40", view.filled_text)

    def test_partial_open_is_not_a_full_fill_or_oco(self) -> None:
        receipt = json.loads(
            _receipt(status="live_refused", error="parent_fill_incomplete", order_qty="1.00")
        )
        position = {
            "status": "open",
            "quantity": "0.40",
            "protection": "held_position_exit",
            "entry_order_id": "91001",
        }
        view = spot_book_view(receipt, "rejected", position, position_table=True)
        assert view is not None
        self.assertEqual(view.label, "部分成交")
        self.assertEqual(view.filled_text, "0.40")
        self.assertEqual(view.order_text, "1.00")
        self.assertIn("无交易所OCO", view.protection_text)
        self.assertNotIn("整单", view.summary)
        self.assertFalse(view.is_rejection)

    def test_oco_covers_filled_qty_only(self) -> None:
        receipt = json.loads(
            _receipt(status="live_refused", error="parent_fill_incomplete", order_qty="1.00")
        )
        position = {
            "status": "oco_working",
            "quantity": "0.40",
            "protection": "exchange_oco",
            "entry_order_id": "91001",
            "oco_list_status": "EXECUTING",
        }
        view = spot_book_view(receipt, "rejected", position, position_table=True)
        assert view is not None
        self.assertEqual(view.label, "部分成交")
        self.assertIn("OCO仅覆盖已成交 0.40", view.protection_text)
        self.assertIn("list=EXECUTING", view.protection_text)
        self.assertNotIn("整单", view.summary)
        self.assertNotIn("与委托数量一致", view.protection_text)

    def test_full_fill_matches_order_without_claiming_the_whole_order(self) -> None:
        receipt = json.loads(
            _receipt(status="binance_spot_submitted", error=None, order_qty="1.00", filled_qty="1.00")
        )
        position = {
            "status": "oco_working",
            "quantity": "1.00",
            "protection": "exchange_oco",
            "entry_order_id": "91001",
        }
        view = spot_book_view(receipt, "executed", position, position_table=True)
        assert view is not None
        self.assertEqual(view.label, "已成交")
        self.assertEqual(view.filled_text, "1.00")
        self.assertEqual(view.order_text, "1.00")
        self.assertIn("与委托数量一致", view.protection_text)
        self.assertIn("OCO仅覆盖已成交 1.00", view.protection_text)
        self.assertNotIn("整单", view.summary)

    def test_cancel_without_fill(self) -> None:
        receipt = json.loads(
            _receipt(status="live_refused", error="parent_fill_incomplete", order_qty="1.00")
        )
        position = {
            "status": "closed_canceled",
            "quantity": None,
            "protection": "held_position_exit",
            "entry_order_id": "91001",
            "exit_reason": "entry_ttl_expired",
        }
        view = spot_book_view(receipt, "rejected", position, position_table=True)
        assert view is not None
        self.assertEqual(view.label, "已撤单")
        self.assertEqual(view.filled_text, "未入账")
        self.assertIn("无交易所OCO", view.protection_text)
        self.assertFalse(view.is_rejection)

    def test_cancel_row_with_residual_qty(self) -> None:
        receipt = json.loads(
            _receipt(status="live_refused", error="parent_fill_incomplete", order_qty="1.00")
        )
        position = {
            "status": "closed_canceled",
            "quantity": "0.20",
            "protection": "held_position_exit",
            "entry_order_id": "91001",
            "exit_reason": "entry_ttl_expired",
        }
        view = spot_book_view(receipt, "rejected", position, position_table=True)
        assert view is not None
        self.assertEqual(view.label, "已撤单·残余成交")
        self.assertEqual(view.filled_text, "0.20")
        self.assertEqual(view.order_text, "1.00")
        self.assertNotIn("整单", view.summary)
        self.assertFalse(view.is_rejection)

    def test_real_reject_has_no_book_row(self) -> None:
        receipt = json.loads(
            _receipt(
                status="live_refused",
                error="binance_order_rejected",
                order_qty="1.00",
                entry_order_id=None,
            )
        )
        view = spot_book_view(receipt, "rejected", None, position_table=True)
        assert view is not None
        self.assertTrue(view.is_rejection)
        self.assertIn("binance_order_rejected", view.label)
        self.assertEqual(
            rejection_key(1, "", "rejected", receipt, spot_position=None, spot_table=True),
            "rejected",
        )

    def test_missing_position_table_stays_on_the_first_ack(self) -> None:
        receipt = json.loads(
            _receipt(status="live_refused", error="parent_fill_incomplete", order_qty="1.00")
        )
        view = spot_book_view(receipt, "rejected", None, position_table=False)
        assert view is not None
        self.assertTrue(view.snapshot_only)
        self.assertIn("首包已接受未成交", view.label)
        self.assertIn("无仓表，后续状态不可用", view.protection_text)
        self.assertFalse(view.is_rejection)
        self.assertNotIn("0.40", view.summary)

    def test_paper_rejection_is_unchanged(self) -> None:
        self.assertEqual(
            rejection_key(1, "", "rejected", {"status": "pm_rejected"}),
            "pm_rejected",
        )


class SpotBookStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.execution = self.root / "execution_ledger.db"
        self.main = self.root / "dexscreener.db"
        self.paper = self.root / "paper_trading.db"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(self.execution)
        conn.executescript(
            """
            CREATE TABLE execution_log (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                decision_log_id INTEGER,
                strategy_id TEXT,
                action TEXT,
                token_address TEXT,
                executed INTEGER,
                executor TEXT,
                receipt_json TEXT,
                validation_ok INTEGER,
                validation_errors TEXT,
                status TEXT
            );
            CREATE TABLE binance_spot_positions (
                id INTEGER PRIMARY KEY,
                decision_log_id INTEGER,
                status TEXT,
                quantity TEXT,
                protection TEXT,
                entry_order_id TEXT,
                exit_reason TEXT,
                oco_list_status TEXT
            );
            """
        )
        rows = [
            (
                1,
                now,
                101,
                "live",
                "buy",
                "SOLUSDT",
                0,
                "binance_spot",
                _receipt(status="live_refused", error="parent_fill_incomplete", order_qty="1.00"),
                1,
                "",
                "rejected",
            ),
            (
                2,
                now,
                102,
                "live",
                "buy",
                "SOLUSDT",
                0,
                "binance_spot",
                _receipt(status="live_refused", error="parent_fill_incomplete", order_qty="1.00"),
                1,
                "",
                "rejected",
            ),
            (
                3,
                now,
                103,
                "live",
                "buy",
                "SOLUSDT",
                1,
                "binance_spot",
                _receipt(status="binance_spot_submitted", error=None, order_qty="1.00", filled_qty="1.00"),
                1,
                "",
                "executed",
            ),
            (
                4,
                now,
                104,
                "live",
                "buy",
                "SOLUSDT",
                0,
                "binance_spot",
                _receipt(
                    status="live_refused",
                    error="binance_order_rejected",
                    order_qty="1.00",
                    entry_order_id=None,
                ),
                1,
                "",
                "rejected",
            ),
        ]
        conn.executemany(
            "INSERT INTO execution_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO binance_spot_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, 101, "qty_unconfirmed", None, "held_position_exit", "91001", None, None),
                (2, 102, "open", "0.40", "held_position_exit", "91002", None, None),
                (3, 103, "oco_working", "1.00", "exchange_oco", "91003", None, "EXECUTING"),
            ],
        )
        conn.commit()
        conn.close()
        sqlite3.connect(self.main).close()
        self.store = DataStore(
            DbPaths(
                main=self.main,
                execution=self.execution,
                paper=self.paper,
                backtest=self.main,
                news=self.main,
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_recent_rows_follow_the_book(self) -> None:
        result = self.store.recent_executions(10)
        self.assertTrue(result.available, result.reason)
        by_decision = {row.decision_log_id: row for row in result.data}
        resting = by_decision["101"]
        self.assertEqual(resting.book_label, "已接受未成交")
        self.assertNotIn("拒绝", resting.receipt_summary)
        partial = by_decision["102"]
        self.assertEqual(partial.book_label, "部分成交")
        self.assertEqual(partial.filled_text, "0.40")
        self.assertEqual(partial.order_text, "1.00")
        full = by_decision["103"]
        self.assertEqual(full.book_label, "已成交")
        self.assertIn("OCO仅覆盖已成交 1.00", full.protection_text)
        rejected = by_decision["104"]
        self.assertIn("拒绝", rejected.book_label)
        self.assertIn("binance_order_rejected", rejected.book_label)

    def test_rejection_bucket_keeps_only_the_real_reject(self) -> None:
        result = self.store.rejection_buckets(7)
        self.assertTrue(result.available, result.reason)
        self.assertEqual(result.data.total, 1)
        self.assertEqual(result.data.buckets[0].key, "rejected")

    def test_failures_skip_the_accepted_resting_order(self) -> None:
        result = self.store.recent_failures(10)
        self.assertTrue(result.available, result.reason)
        decisions = {row.decision_log_id for row in result.data}
        self.assertNotIn("101", decisions)
        self.assertNotIn("102", decisions)
        self.assertIn("104", decisions)

    def test_missing_paper_db_is_not_created(self) -> None:
        self.assertFalse(self.paper.exists())
        with self.assertRaises(FileNotFoundError):
            connect_ro(self.paper)
        self.assertFalse(self.paper.exists())
        aside = self.store.paper_equity_aside()
        self.assertFalse(aside.available)
        self.assertIn("不存在", aside.reason)
        self.assertFalse(self.paper.exists())

    def test_execution_read_stays_query_only(self) -> None:
        conn = connect_ro(self.execution)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE execution_log SET status = 'mutated'")
        finally:
            conn.close()
        untouched = sqlite3.connect(self.execution)
        try:
            status = untouched.execute(
                "SELECT status FROM execution_log WHERE decision_log_id = 101"
            ).fetchone()[0]
        finally:
            untouched.close()
        self.assertEqual(status, "rejected")


if __name__ == "__main__":
    unittest.main()
