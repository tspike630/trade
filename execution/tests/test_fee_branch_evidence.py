"""Calls the branches a later review described only in prose.

Those descriptions were not an attached script or stdout. These tests are the
persistent evidence. A reproduced defect fails here until the branch changes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_spot import BinanceRequestError  # noqa: E402
from execution.tests.test_live_bnb_boll_classic import (  # noqa: E402
    _adapter,
    _env,
    _sig,
    _trading_info,
    _working,
)


def _trade(order_id: int, qty: str, fee: str, *, buyer: bool) -> dict[str, Any]:
    return {
        "symbol": "BNBUSDT",
        "id": order_id * 10 + 1,
        "orderId": order_id,
        "qty": qty,
        "commission": fee,
        "commissionAsset": "BNB",
        "isBuyer": buyer,
    }


class _Book:
    def __init__(self) -> None:
        self.orders: list[str] = []
        self.oco: list[str] = []
        self.trade_reads = 0
        self.entry_status = "FILLED"
        self.entry_executed = "0.25"
        self.trades: list[dict[str, Any]] | None = [_trade(99, "0.25", "0.001", buyer=True)]

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        return _trading_info(symbol)

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "price": "102"}

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.orders.append(signed_query)
        form = parse_qs(signed_query)
        if form.get("side") == ["SELL"]:
            qty = (form.get("quantity") or ["0"])[0]
            return {
                "orderId": 77,
                "status": "FILLED",
                "executedQty": qty,
                "symbol": "BNBUSDT",
                "side": "SELL",
            }
        return {
            "orderId": 99,
            "status": "FILLED",
            "executedQty": "0.25",
            "symbol": "BNBUSDT",
            "side": "BUY",
            "fills": [{"qty": "0.25", "commission": "0.001", "commissionAsset": "BNB"}],
        }

    def my_trades(self, signed_query: str, *, api_key: str) -> list[dict[str, Any]]:
        self.trade_reads += 1
        form = parse_qs(signed_query)
        if form.get("fromId"):
            return []
        if self.trades is None:
            return []
        order_id = str((form.get("orderId") or ["99"])[0])
        return [dict(item) for item in self.trades if str(item["orderId"]) == order_id]

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.oco.append(signed_query)
        body = _working()
        body["symbol"] = "BNBUSDT"
        for report in body["orderReports"]:
            report["symbol"] = "BNBUSDT"
            report["origQty"] = "0.24"
        return body

    def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        form = parse_qs(signed_query)
        if form.get("origClientOrderId"):
            raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")
        order_id = (form.get("orderId") or ["99"])[0]
        return {
            "symbol": "BNBUSDT",
            "orderId": int(order_id) if str(order_id).isdigit() else order_id,
            "side": "BUY",
            "status": self.entry_status,
            "executedQty": self.entry_executed,
            "origQty": "0.30",
        }

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        body = _working()
        body["symbol"] = "BNBUSDT"
        return body


class FeeBranchEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.client = _Book()
        self.adapter = _adapter(
            self.root,
            self.client,
            auto_flatten_on_oco_fail=True,
        )

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def test_residual_oco_is_not_a_safe_working_receipt(self) -> None:
        with mock.patch.dict(os.environ, _env(self.root)):
            result = self.adapter.execute(
                _sig(mode="live_stub", decision_log_id=147990, size_pct=0.05)
            )
        self.assertGreater(self.client.trade_reads, 0, result)
        self.assertEqual(len(self.client.oco), 1, result)
        self.assertEqual(parse_qs(self.client.oco[0]).get("quantity"), ["0.24"])
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["consumer_allowed"], result)
        self.assertNotEqual(result.get("protection_state"), "oco_working")
        self.assertEqual(result["protection_state"], "dust_residual")
        self.assertEqual(
            result["manual_action"],
            "residual_below_min_is_not_a_complete_exit",
        )
        self.assertFalse(result["protection_incident"]["delivered"])
        arm = result["arm_position"]
        self.assertEqual(Decimal(arm["quantity"]), Decimal("0.249"))
        self.assertEqual(Decimal(arm["unprotected_qty"]), Decimal("0.009"))
        self.assertFalse(any(parse_qs(query).get("side") == ["SELL"] for query in self.client.orders))

    def test_settle_reduce_sell_does_not_close_on_gross_executed_qty(self) -> None:
        row = {
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "oco_failed",
            "quantity": "0.249",
            "last_error": "oco_rejected:test",
            "stop_price": "95",
            "take_profit_price": "110",
        }
        placed = {
            "orderId": 77,
            "status": "FILLED",
            "executedQty": "0.249",
            "symbol": "BNBUSDT",
            "side": "SELL",
        }
        self.client.trades = None

        def save(fields: dict[str, Any]) -> None:
            row.update(fields)

        with mock.patch.dict(os.environ, _env(self.root)):
            out = self.adapter._settle_reduce_sell(
                row,
                save,
                placed,
                "bnbfade-x-11",
                reason="oco_failed_auto_flatten",
                protection="oco_pending",
                error_prefix="oco_failed_auto_flattened",
            )
        self.assertNotEqual(out["book"], "closed", out)
        self.assertEqual(out["error"], "fee_unverified", out)
        self.assertEqual(row["quantity"], "0.249")
        self.assertNotEqual(row.get("status"), "closed")
        self.assertEqual(
            row["manual_action"],
            "halt_new_buys_and_manually_close_or_protect_the_fill",
        )
        self.assertFalse(out["protection_incident"]["delivered"])

    def test_partial_fill_does_not_sell_the_stored_quantity(self) -> None:
        self.client.entry_status = "PARTIALLY_FILLED"
        self.client.entry_executed = "0.10"
        row = {
            "id": 1,
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "open",
            "quantity": "0.30",
            "entry_order_id": "40",
            "stop_price": "95",
            "take_profit_price": "110",
        }
        with mock.patch.dict(os.environ, _env(self.root)):
            out = self.adapter.exit_held_position(row, "90")
        self.assertEqual(out["error"], "fee_unverified", out)
        self.assertFalse(out["submitted"])
        self.assertFalse(any("side=SELL" in query for query in self.client.orders))
        self.assertNotIn("quantity=0.3", " ".join(self.client.orders))
        self.assertNotIn("quantity=0.30", " ".join(self.client.orders))

    def test_maker_unknown_fee_keeps_quantity_and_maintain_names_it(self) -> None:
        self.client.trades = None
        placed = {
            "orderId": "99",
            "status": "CANCELED",
            "executedQty": "0.25",
            "symbol": "BNBUSDT",
            "side": "BUY",
        }
        with mock.patch.dict(os.environ, _env(self.root)):
            result = self.adapter._resting_maker_ack(
                _sig(mode="live_stub", decision_log_id=147991),
                plan={
                    "stop_price": "95",
                    "take_profit_price": "110",
                    "entry_price": "100",
                },
                gates=self.adapter._gates(),
                intent=None,
                pairs=[("symbol", "BNBUSDT"), ("side", "BUY")],
                order_id="99",
                placed=placed,
                symbol="BNBUSDT",
            )
            arm = dict(result["arm_position"])
            arm["id"] = 1
            maintained = self.adapter.maintain_held_position(arm, "102")
        self.assertEqual(result["error"], "fee_unverified", result)
        self.assertEqual(arm["quantity"], "0.25")
        self.assertEqual(arm["unprotected_qty"], "0.25")
        self.assertEqual(
            result["manual_action"],
            "halt_new_buys_and_manually_close_or_protect_the_fill",
        )
        self.assertFalse(result["protection_incident"]["delivered"])
        self.assertEqual(maintained["error"], "fee_unverified", maintained)
        self.assertEqual(maintained["quantity"], "0.25")
        self.assertEqual(
            maintained["manual_action"],
            "halt_new_buys_and_manually_close_or_protect_the_fill",
        )
        self.assertFalse(maintained["protection_incident"]["delivered"])
        self.assertFalse(any("side=SELL" in query for query in self.client.orders))
