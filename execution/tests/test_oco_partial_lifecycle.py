"""OCO residual lifecycle. Quantity is the full net; residual is a slice of it.

Rehang and the second inspection must not add that slice a second time.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

from execution.executors.binance_spot import BinanceRequestError, BinanceSpotAdapter
from execution.tests.test_live_bnb_boll_classic import _env, _trading_info


def _leg(order_id: int, kind: str, *, executed: str = "0", status: str = "NEW") -> dict[str, Any]:
    return {
        "symbol": "BNBUSDT",
        "orderId": order_id,
        "type": kind,
        "side": "SELL",
        "status": status,
        "origQty": "0.24",
        "executedQty": executed,
    }


def _working(list_id: int = 42, *, above_exec: str = "0", below_exec: str = "0", status: str = "EXECUTING") -> dict[str, Any]:
    above_status = "FILLED" if Decimal(above_exec) > 0 and status != "EXECUTING" else (
        "PARTIALLY_FILLED" if Decimal(above_exec) > 0 else "NEW"
    )
    return {
        "orderListId": list_id,
        "listOrderStatus": status,
        "listStatusType": "EXEC_STARTED" if status == "EXECUTING" else "ALL_DONE",
        "symbol": "BNBUSDT",
        "orderReports": [
            _leg(11, "LIMIT_MAKER", executed=above_exec, status=above_status if status != "EXECUTING" else ("PARTIALLY_FILLED" if Decimal(above_exec) > 0 else "NEW")),
            _leg(10, "STOP_LOSS_LIMIT", executed=below_exec, status="FILLED" if Decimal(below_exec) > 0 else "CANCELED"),
        ],
    }


class _Client:
    def __init__(self) -> None:
        self.mode = "working"
        self.list_id = 42
        self.above_exec = "0"
        self.below_exec = "0"
        self.list_status = "EXECUTING"
        self.trades: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        return _trading_info(symbol)

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append("new_oco")
        if self.mode == "timeout":
            raise TimeoutError("oco timed out")
        if self.mode == "unverifiable":
            return {"orderListId": None}
        if self.mode == "reject":
            raise BinanceRequestError(400, code=-1013, msg="Filter failure")
        return _working(self.list_id, above_exec=self.above_exec, below_exec=self.below_exec, status=self.list_status)

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append("get_order_list")
        return _working(self.list_id, above_exec=self.above_exec, below_exec=self.below_exec, status=self.list_status)

    def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        form = parse_qs(signed_query)
        order_id = (form.get("orderId") or [""])[0]
        executed = self.above_exec if order_id == "11" else self.below_exec if order_id == "10" else "0"
        status = "FILLED" if Decimal(executed) > 0 and self.list_status != "EXECUTING" else (
            "PARTIALLY_FILLED" if Decimal(executed) > 0 else "NEW"
        )
        return {
            "symbol": "BNBUSDT",
            "orderId": int(order_id) if order_id.isdigit() else order_id,
            "side": "SELL",
            "type": "LIMIT_MAKER" if order_id == "11" else "STOP_LOSS_LIMIT",
            "status": status,
            "origQty": "0.24",
            "executedQty": executed,
        }

    def my_trades(self, signed_query: str, *, api_key: str) -> list[dict[str, Any]]:
        form = parse_qs(signed_query)
        if form.get("fromId"):
            return []
        order_id = (form.get("orderId") or [""])[0]
        if order_id in self.trades:
            return list(self.trades[order_id])
        return []


class OcoPartialLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        )
        self._net.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = _Client()
        self.adapter = BinanceSpotAdapter(
            client=self.client,
            kill_switch_path=self.root / "absent-kill",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            auto_flatten_on_oco_fail=False,
            reanchor_oco_on_fill=False,
        )
        self.env = _env(self.root)

    def tearDown(self) -> None:
        self._net.stop()
        self.tmp.cleanup()

    def _row(self) -> dict[str, Any]:
        return {
            "id": 1,
            "decision_log_id": 249,
            "symbol": "BNBUSDT",
            "status": "open",
            "protection": "pending",
            "quantity": "0.249",
            "stop_price": "95",
            "take_profit_price": "110",
            "entry_price": "102",
        }

    def _place(self, row: dict[str, Any]) -> dict[str, Any]:
        saved: dict[str, Any] = {}

        def save(fields: dict[str, Any]) -> None:
            row.update(fields)
            saved.update(fields)

        with mock.patch.dict(os.environ, self.env):
            out = self.adapter._try_place_oco(row, "102", save)
        assert out is not None
        return out

    def test_first_place_keeps_net_and_names_the_slice(self) -> None:
        row = self._row()
        out = self._place(row)
        self.assertEqual(out["book"], "oco_partial")
        self.assertFalse(out["ok"])
        self.assertTrue(out["submitted"])
        self.assertEqual(out["error"], "oco_lot_residual")
        self.assertFalse(out["consumer_allowed"])
        self.assertFalse(out["protection_incident"]["delivered"])
        self.assertEqual(row["quantity"], "0.249")
        self.assertEqual(row["residual_qty"], "0.009")
        self.assertEqual(row["status"], "oco_partial")
        self.assertEqual(row["last_error"], "oco_residual_unprotected:0.009")
        signed = self.client.calls
        self.assertIn("new_oco", signed)

    def test_second_pass_does_not_promote_partial_to_working(self) -> None:
        row = self._row()
        self._place(row)
        with mock.patch.dict(os.environ, self.env):
            again = self.adapter.maintain_held_position(row, "102", persist=lambda fields: row.update(fields) or row)
        self.assertEqual(row["status"], "oco_partial")
        self.assertEqual(row["quantity"], "0.249")
        self.assertEqual(again["book"], "oco_partial")
        self.assertFalse(again["ok"])

    def test_timeout_claim_uses_the_same_partial_book(self) -> None:
        self.client.mode = "timeout"
        row = self._row()
        out = self._place(row)
        self.assertEqual(out["book"], "oco_partial")
        self.assertEqual(row["quantity"], "0.249")
        self.assertEqual(row["status"], "oco_partial")
        self.assertIn("get_order_list", self.client.calls)

    def test_unverifiable_200_requery_claims_partial(self) -> None:
        self.client.mode = "unverifiable"
        row = self._row()
        out = self._place(row)
        self.assertEqual(out["book"], "oco_partial")
        self.assertEqual(row["residual_qty"], "0.009")
        self.assertIn("get_order_list", self.client.calls)

    def test_filled_leg_with_dust_residual_is_not_a_flat_close(self) -> None:
        self.client.above_exec = "0.24"
        self.client.list_status = "ALL_DONE"
        self.client.trades["11"] = [{
            "symbol": "BNBUSDT",
            "id": 110,
            "orderId": 11,
            "qty": "0.24",
            "commission": "0",
            "commissionAsset": "BNB",
            "isBuyer": False,
        }]
        row = self._row()
        self._place(row)
        self.assertEqual(row["status"], "closed_dust")
        self.assertEqual(Decimal(row["quantity"]), Decimal("0.009"))
        self.assertEqual(Decimal(row["residual_qty"]), Decimal("0.009"))
        self.assertNotEqual(row["quantity"], "0")

    def test_unknown_fee_keeps_the_net(self) -> None:
        self.client.above_exec = "0.10"
        self.client.list_status = "ALL_DONE"
        row = self._row()
        out = self._place(row)
        self.assertIn("manual_intervention_required", str(out["error"]))
        self.assertEqual(row["quantity"], "0.249")
        self.assertNotEqual(row["status"], "closed")

    def test_second_inspection_does_not_subtract_the_same_sell_again(self) -> None:
        self.client.above_exec = "0.10"
        self.client.list_status = "EXECUTING"
        self.client.trades["11"] = [{
            "symbol": "BNBUSDT",
            "id": 111,
            "orderId": 11,
            "qty": "0.10",
            "commission": "0.001",
            "commissionAsset": "BNB",
            "isBuyer": False,
        }]
        row = self._row()
        self._place(row)
        once = row["quantity"]
        with mock.patch.dict(os.environ, self.env):
            self.adapter.maintain_held_position(row, "102", persist=lambda fields: row.update(fields) or row)
        self.assertEqual(row["quantity"], once)
        self.assertEqual(row["status"], "oco_partial")

    def test_sellable_residual_stays_pending_and_is_not_dust_closed(self) -> None:
        row = self._row()
        row["oco_order_list_id"] = "42"
        row["oco_above_order_id"] = "11"
        row["oco_below_order_id"] = "10"
        row["residual_qty"] = "0.009"
        self.client.above_exec = "0.24"
        self.client.list_status = "ALL_DONE"
        self.client.trades["11"] = [{
            "symbol": "BNBUSDT",
            "id": 112,
            "orderId": 11,
            "qty": "0.24",
            "commission": "0",
            "commissionAsset": "BNB",
            "isBuyer": False,
        }]
        recon = _working(42, above_exec="0.24", status="ALL_DONE")
        from execution.executors.binance_spot_protect import classify_oco

        found = classify_oco(recon, held_qty="0.24")
        found["order_reports"] = recon["orderReports"]
        saved: dict[str, Any] = {}
        with mock.patch.object(self.adapter, "_remainder_is_dust", return_value=False):
            state = self.adapter._account_oco_execution(row, found, lambda fields: saved.update(fields) or row.update(fields))
        self.assertEqual(state, "open")
        self.assertEqual(saved["status"], "oco_failed")
        self.assertEqual(saved["protection"], "oco_pending")
        self.assertEqual(Decimal(saved["quantity"]), Decimal("0.009"))
        self.assertEqual(saved["residual_qty"], "")

    def test_both_legs_partial_fill_counts_each_sell_once(self) -> None:
        row = self._row()
        row["oco_order_list_id"] = "42"
        row["oco_above_order_id"] = "11"
        row["oco_below_order_id"] = "10"
        self.client.above_exec = "0.10"
        self.client.below_exec = "0.10"
        self.client.list_status = "EXECUTING"
        for order_id, qty in (("11", "0.10"), ("10", "0.10")):
            self.client.trades[order_id] = [{
                "symbol": "BNBUSDT",
                "id": int(order_id) * 10,
                "orderId": int(order_id),
                "qty": qty,
                "commission": "0",
                "commissionAsset": "BNB",
                "isBuyer": False,
            }]
        recon = _working(42, above_exec="0.10", below_exec="0.10", status="EXECUTING")
        from execution.executors.binance_spot_protect import classify_oco

        found = classify_oco(recon, held_qty="0.24")
        found["order_reports"] = recon["orderReports"]
        saved: dict[str, Any] = {}
        state = self.adapter._account_oco_execution(
            row, found, lambda fields: saved.update(fields) or row.update(fields)
        )
        self.assertEqual(state, "open")
        self.assertEqual(Decimal(saved["quantity"]), Decimal("0.049"))
        self.assertEqual(Decimal(saved["exit_filled_qty"]), Decimal("0.20"))

    def test_new_list_does_not_subtract_the_previous_watermark(self) -> None:
        row = self._row()
        row["exit_filled_qty"] = "0.24"
        row["oco_order_list_id"] = "42"
        row["oco_above_order_id"] = "11"
        row["oco_below_order_id"] = "10"
        self.client.above_exec = "0.05"
        self.client.list_status = "EXECUTING"
        self.client.trades["11"] = [{
            "symbol": "BNBUSDT",
            "id": 113,
            "orderId": 11,
            "qty": "0.05",
            "commission": "0",
            "commissionAsset": "BNB",
            "isBuyer": False,
        }]
        recon = _working(99, above_exec="0.05", status="EXECUTING")
        from execution.executors.binance_spot_protect import classify_oco

        found = classify_oco(recon, held_qty="0.24")
        found["order_list_id"] = "99"
        found["order_reports"] = recon["orderReports"]
        saved: dict[str, Any] = {}
        state = self.adapter._account_oco_execution(
            row, found, lambda fields: saved.update(fields) or row.update(fields)
        )
        self.assertEqual(state, "open")
        self.assertEqual(Decimal(saved["quantity"]), Decimal("0.199"))
        self.assertNotEqual(row["status"], "oco_unknown")

    def test_terminal_cancel_without_a_fill_does_not_drop_the_net(self) -> None:
        row = self._row()
        row["residual_qty"] = "0.009"
        recon = {
            "state": "unfilled",
            "executed_qty": "0",
            "order_list_id": "42",
            "above_order_id": "11",
            "below_order_id": "10",
            "order_reports": [],
        }
        saved: dict[str, Any] = {}
        state = self.adapter._account_oco_execution(
            row, recon, lambda fields: saved.update(fields) or row.update(fields)
        )
        self.assertEqual(state, "open")
        self.assertEqual(row["quantity"], "0.249")
        self.assertEqual(saved, {})

    def test_closed_dust_without_exit_filled_qty_is_nonterminal_and_blocks_buys(self) -> None:
        from decision_engine.core.protection_halt import purpose_halt_reason, read_protection_book
        from execution.executors.binance_spot_gate import (
            PURPOSE_LIVE_BNB_BOLL_CLASSIC,
            PURPOSE_LIVE_BNB_BOLL_FADE,
            buy_exposure_block,
            cross_arm_symbol_block,
            partial_dust_residual,
            reserved_quote_usdt,
        )
        from execution.ledger import ExecutionLedger, read_spot_buy_blocks_readonly

        for exit_filled in (None, "", "0"):
            dust_row = {
                "id": 7,
                "decision_log_id": 707,
                "symbol": "BNBUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "closed_dust",
                "protection": "dust_residual",
                "quantity": "0.004",
                "entry_price": "600",
                "entry_order_id": "9001",
                "exit_filled_qty": exit_filled,
                "residual_qty": None,
            }
            self.assertTrue(partial_dust_residual(dust_row))
            self.assertEqual(
                buy_exposure_block(
                    [dust_row],
                    [],
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    symbol="ETHUSDT",
                ),
                "unprotected_position_blocks_buy",
            )
            self.assertEqual(
                cross_arm_symbol_block(
                    [dust_row],
                    purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC,
                    symbol="BNBUSDT",
                ),
                "cross_arm_symbol_blocks_buy",
            )
            self.assertEqual(reserved_quote_usdt([dust_row]), Decimal("2.4"))

        db_path = self.root / "dust_gate.db"
        with ExecutionLedger(db_path) as ledger:
            pos = ledger.arm_binance_spot_position(
                {
                    "decision_log_id": 708,
                    "symbol": "BNBUSDT",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                    "status": "open",
                    "quantity": "0.004",
                    "stop_price": "570",
                    "take_profit_price": "630",
                    "entry_price": "600",
                    "entry_order_id": "9002",
                    "protection": "pending",
                }
            )
            ledger.update_binance_spot_position(
                int(pos["id"]),
                {
                    "status": "closed_dust",
                    "protection": "dust_residual",
                    "exit_reason": "dust_abandon",
                    "last_error": "dust_untradable",
                },
            )
            blocks = ledger.list_binance_spot_buy_blocks(PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(len(blocks), 1)
            self.assertIsNone(blocks[0].get("exit_filled_qty"))
            self.assertEqual(ledger.list_binance_spot_positions_for_exit(PURPOSE_LIVE_BNB_BOLL_FADE), [])

        ro_blocks = read_spot_buy_blocks_readonly(db_path)
        self.assertEqual(len(ro_blocks), 1)
        book = read_protection_book(db_path)
        self.assertEqual(
            purpose_halt_reason(PURPOSE_LIVE_BNB_BOLL_FADE, book),
            "unprotected_position_blocks_buy",
        )

