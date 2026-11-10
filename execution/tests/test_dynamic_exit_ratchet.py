"""Tests for Milestone 15: Dynamic OCO Breakeven/Trailing Ratchet & Max-Hold Time-Stop."""

from __future__ import annotations

import json
import os
import unittest
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from execution.executors.binance_spot import (
    BinanceSpotAdapter,
    BinanceSpotHttpClient,
)
from execution.executors.binance_spot_protect import (
    PROTECTION_EXCHANGE,
    PROTECTION_TIME_STOP,
    compute_ratcheted_stop_price,
    oco_above_client_id,
    oco_below_client_id,
    oco_list_client_id,
)


def _sample_filters() -> list[dict[str, Any]]:
    return [
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.01",
            "maxPrice": "100000.00",
            "tickSize": "0.01",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.01",
            "maxQty": "900000.00",
            "stepSize": "0.01",
        },
        {
            "filterType": "NOTIONAL",
            "minNotional": "5.00",
            "applyMinToMarket": True,
        },
    ]


def _sample_exchange_info(symbol: str = "BNBUSDT") -> dict[str, Any]:
    return {
        "symbols": [
            {
                "symbol": symbol,
                "status": "TRADING",
                "baseAsset": "BNB",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
                "filters": _sample_filters(),
            }
        ]
    }


class _FakeRatchetClient:
    def __init__(self) -> None:
        self.canceled_lists: list[str] = []
        self.placed_ocos: list[str] = []
        self.placed_orders: list[str] = []

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        return _sample_exchange_info(symbol)

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        return {
            "orderListId": 5001,
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "listClientOrderId": "bnbfade-ol-42",
            "symbol": "BNBUSDT",
            "orders": [
                {"symbol": "BNBUSDT", "orderId": 6001, "clientOrderId": "bnbfade-tp-42"},
                {"symbol": "BNBUSDT", "orderId": 6002, "clientOrderId": "bnbfade-sl-42"},
            ],
            "orderReports": [
                {
                    "symbol": "BNBUSDT",
                    "orderId": 6001,
                    "clientOrderId": "bnbfade-tp-42",
                    "type": "LIMIT_MAKER",
                    "status": "NEW",
                    "executedQty": "0.00",
                },
                {
                    "symbol": "BNBUSDT",
                    "orderId": 6002,
                    "clientOrderId": "bnbfade-sl-42",
                    "type": "STOP_LOSS_LIMIT",
                    "status": "NEW",
                    "executedQty": "0.00",
                },
            ],
        }

    def cancel_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.canceled_lists.append(signed_query)
        return {
            "orderListId": 5001,
            "listStatusType": "ALL_DONE",
            "listOrderStatus": "ALL_DONE",
            "symbol": "BNBUSDT",
            "orderReports": [
                {
                    "symbol": "BNBUSDT",
                    "orderId": 6001,
                    "type": "LIMIT_MAKER",
                    "status": "CANCELED",
                    "executedQty": "0.00",
                },
                {
                    "symbol": "BNBUSDT",
                    "orderId": 6002,
                    "type": "STOP_LOSS_LIMIT",
                    "status": "CANCELED",
                    "executedQty": "0.00",
                },
            ],
        }

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.placed_ocos.append(signed_query)
        return {
            "orderListId": 5002,
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "listClientOrderId": "bnbfade-ol-42-r1",
            "symbol": "BNBUSDT",
            "orders": [
                {"symbol": "BNBUSDT", "orderId": 7001, "clientOrderId": "bnbfade-tp-42-r1"},
                {"symbol": "BNBUSDT", "orderId": 7002, "clientOrderId": "bnbfade-sl-42-r1"},
            ],
            "orderReports": [
                {
                    "symbol": "BNBUSDT",
                    "orderId": 7001,
                    "clientOrderId": "bnbfade-tp-42-r1",
                    "type": "LIMIT_MAKER",
                    "status": "NEW",
                    "executedQty": "0.00",
                },
                {
                    "symbol": "BNBUSDT",
                    "orderId": 7002,
                    "clientOrderId": "bnbfade-sl-42-r1",
                    "type": "STOP_LOSS_LIMIT",
                    "status": "NEW",
                    "executedQty": "0.00",
                },
            ],
        }

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.placed_orders.append(signed_query)
        return {
            "symbol": "BNBUSDT",
            "orderId": 8888,
            "status": "FILLED",
            "executedQty": "1.50",
            "cummulativeQuoteQty": "151.50",
        }


class TestDynamicExitRatchet(unittest.TestCase):
    def test_client_id_ratchet_suffix_is_deterministic(self) -> None:
        self.assertEqual(oco_list_client_id(42, ratchet_seq=0), "bnbfade-ol-42")
        self.assertEqual(oco_list_client_id(42, ratchet_seq=1), "bnbfade-ol-42-r1")
        self.assertEqual(oco_above_client_id(42, ratchet_seq=2), "bnbfade-tp-42-r2")
        self.assertEqual(oco_below_client_id(42, ratchet_seq=2), "bnbfade-sl-42-r2")

    def test_compute_ratcheted_stop_price_breakeven_and_trailing_stages(self) -> None:
        filters = _sample_filters()
        row = {
            "entry_price": "100.00",
            "stop_price": "96.00",
            "take_profit_price": "104.00",
        }
        # 1) 40% progress (mark = 101.60 < 60% threshold 102.40) -> no ratchet
        stop_1, reason_1 = compute_ratcheted_stop_price(
            row, mark="101.60", filters=filters
        )
        self.assertIsNone(stop_1)
        self.assertIsNone(reason_1)

        # 2) 65% progress (mark = 102.60 >= 60% threshold):
        #    breakeven + 25 bps = 100.25, trail (1.5% below 102.60) = 101.061 -> 101.06
        #    With 3% trail dist, breakeven (100.25) > trail (99.52) -> breakeven_ratchet
        stop_be, reason_be = compute_ratcheted_stop_price(
            row,
            mark="102.60",
            filters=filters,
            trail_dist_pct=Decimal("0.03"),
        )
        self.assertEqual(stop_be, "100.25")
        self.assertEqual(reason_be, "breakeven_ratchet")

        # 3) 85% progress with 1.5% trail on wider TP (entry=100, TP=110, mark=108.50):
        #    breakeven = 100.25, trail = 108.50 * 0.985 = 106.8725 -> 106.87
        wide_row = {
            "entry_price": "100.00",
            "stop_price": "100.25",
            "take_profit_price": "110.00",
        }
        stop_tr, reason_tr = compute_ratcheted_stop_price(
            wide_row,
            mark="108.50",
            filters=filters,
            trail_dist_pct=Decimal("0.015"),
        )
        self.assertEqual(stop_tr, "106.87")
        self.assertEqual(reason_tr, "trailing_profit_ratchet")

        # 4) Monotonicity: if mark pulls back to 107.50, stop stays at 106.87 (no downward move)
        wide_row["stop_price"] = "106.87"
        stop_pullback, reason_pullback = compute_ratcheted_stop_price(
            wide_row,
            mark="107.50",
            filters=filters,
            trail_dist_pct=Decimal("0.015"),
        )
        self.assertIsNone(stop_pullback)
        self.assertIsNone(reason_pullback)

    def test_maintain_held_position_ratchets_working_oco_atomically(self) -> None:
        fake_client = _FakeRatchetClient()
        row: dict[str, Any] = {
            "id": 7,
            "decision_log_id": 42,
            "symbol": "BNBUSDT",
            "status": "oco_working",
            "protection": PROTECTION_EXCHANGE,
            "quantity": "1.50",
            "entry_price": "100.00",
            "stop_price": "96.00",
            "take_profit_price": "104.00",
            "oco_order_list_id": "5001",
            "oco_list_client_id": "bnbfade-ol-42",
        }
        saved_updates: list[dict[str, Any]] = []

        def persist(fields: dict[str, Any]) -> dict[str, Any]:
            saved_updates.append(dict(fields))
            row.update(fields)
            return dict(row)

        with patch.dict(os.environ, {"BINANCE_SPOT_LIVE_ENABLED": "1"}, clear=False):
            adapter = BinanceSpotAdapter(
                client=fake_client,
                api_key="test_key",
                api_secret="test_secret",
                clock_ms=lambda: 1_700_000_000_000,
                trailing_ratchet=True,
            )
            outcome = adapter.maintain_held_position(row, "102.80", persist=persist)

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["book"], "oco_working")
        self.assertTrue(outcome.get("oco_ratcheted"))
        self.assertEqual(outcome.get("oco_ratchet_count"), 1)
        self.assertEqual(outcome.get("previous_stop_price"), "96.00")
        self.assertEqual(len(fake_client.canceled_lists), 1)
        self.assertEqual(len(fake_client.placed_ocos), 1)
        self.assertIn("listClientOrderId=bnbfade-ol-42-r1", fake_client.placed_ocos[0])
        self.assertEqual(row["initial_stop_price"], "96.00")
        self.assertEqual(row["oco_ratchet_count"], 1)
        self.assertEqual(row["oco_order_list_id"], "5002")
        self.assertGreater(Decimal(row["stop_price"]), Decimal("100.00"))

    def test_maintain_held_position_executes_max_hold_time_stop(self) -> None:
        fake_client = _FakeRatchetClient()
        row: dict[str, Any] = {
            "id": 8,
            "decision_log_id": 42,
            "symbol": "BNBUSDT",
            "status": "oco_working",
            "protection": PROTECTION_EXCHANGE,
            "quantity": "1.50",
            "entry_price": "100.00",
            "stop_price": "96.00",
            "take_profit_price": "104.00",
            "oco_order_list_id": "5001",
            "oco_list_client_id": "bnbfade-ol-42",
            "created_at_ms": 1_700_000_000_000,
        }

        def persist(fields: dict[str, Any]) -> dict[str, Any]:
            row.update(fields)
            return dict(row)

        # Clock is 3700 seconds after created_at_ms (> max_hold_sec=3600)
        with patch.dict(os.environ, {"BINANCE_SPOT_LIVE_ENABLED": "1"}, clear=False):
            adapter = BinanceSpotAdapter(
                client=fake_client,
                api_key="test_key",
                api_secret="test_secret",
                clock_ms=lambda: 1_700_003_700_000,
                max_hold_sec=3600,
            )
            outcome = adapter.maintain_held_position(row, "100.40", persist=persist)

        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["submitted"])
        self.assertEqual(outcome["book"], "closed")
        self.assertEqual(outcome["reason"], "time_stop_max_hold")
        self.assertEqual(outcome["protection"], PROTECTION_TIME_STOP)
        self.assertEqual(len(fake_client.canceled_lists), 1)
        self.assertEqual(len(fake_client.placed_orders), 1)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["exit_reason"], "time_stop_max_hold")

    def test_http_client_cancel_order_list_sends_delete(self) -> None:
        captured: list[tuple[str, str, str | None]] = []

        def fake_opener(req: Any, timeout: float) -> tuple[int, bytes]:
            captured.append((req.get_method(), req.full_url, req.headers.get("X-mbx-apikey")))
            return 200, json.dumps({"orderListId": 5001, "listOrderStatus": "ALL_DONE"}).encode("utf-8")

        http_client = BinanceSpotHttpClient(
            base_url="https://api.binance.com",
            opener=fake_opener,
        )
        res = http_client.cancel_order_list("symbol=BNBUSDT&orderListId=5001&signature=abc", api_key="k1")
        self.assertEqual(res["orderListId"], 5001)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "DELETE")
        self.assertIn("/api/v3/orderList?symbol=BNBUSDT&orderListId=5001", captured[0][1])
        self.assertEqual(captured[0][2], "k1")


if __name__ == "__main__":
    unittest.main()
