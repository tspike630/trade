"""Gates for the MCP broker. No transport and no order.

    python -m unittest execution.tests.test_binance_mcp_broker
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

from execution.executors.binance_mcp_broker import (  # noqa: E402
    DOCUMENTED_OCO_OPERATION,
    DOCUMENTED_OCO_REST,
    WEBSOCKET_NOT_MCP,
    LiveMCPBroker,
    availability,
)

_FILLED = {
    "order_id": "3735698106",
    "client_order_id": "bnbfade-147831",
    "symbol": "FETUSDT",
    "executed_qty": "140.1",
}


def _book(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "canonical_positions": [
            {
                "purpose": "live_bnb_boll_fade",
                "symbol": "FETUSDT",
                "status": "held_naked",
                "decision_log_id": 147831,
            }
        ],
        "spent_orders": [dict(_FILLED)],
        "open_orders": [],
        "base_free": "140.1",
    }
    base.update(overrides)
    return base


def _buy(decision_id: int, **extras: Any) -> dict[str, Any]:
    body = {"binance_symbol": "FETUSDT", "quote_order_qty": "21.93966"}
    body.update(extras)
    return {
        "purpose": "live_bnb_boll_fade",
        "action": "buy",
        "mode": "live_stub",
        "decision_log_id": decision_id,
        "extras": body,
    }


class BrokerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = LiveMCPBroker()

    def test_original_api_key_availability_is_false(self) -> None:
        self.assertFalse(availability("original_api_key"))
        self.assertFalse(self.broker.availability("original_api_key"))
        for expr in (
            "protection_tool",
            "resume_signals",
            "buy",
            "sell",
            "oco",
            "poll_naked",
        ):
            self.assertFalse(availability(expr))  # type: ignore[arg-type]

    def test_env_key_is_not_read_and_passed_key_is_rejected(self) -> None:
        sentinel = "sentinelsecret-not-a-channel"
        os.environ["BINANCE_API_KEY"] = sentinel
        os.environ["BINANCE_API_SECRET"] = sentinel
        try:
            result = self.broker.evaluate(_buy(147841), _book())
        finally:
            os.environ.pop("BINANCE_API_KEY", None)
            os.environ.pop("BINANCE_API_SECRET", None)
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertFalse(result["original_api_key_available"])
        rejected = self.broker.evaluate(_buy(147841), api_key=sentinel)
        self.assertEqual(rejected["reason"], "raw_api_key_rejected")
        self.assertNotIn(sentinel, json.dumps(rejected))
        self.assertEqual(rejected["parent_queue"], [])
        self.assertFalse(rejected["transport_called"])

    def test_new_decision_id_cannot_buy_over_canonical_position(self) -> None:
        book = _book(spent_orders=[])
        result = self.broker.evaluate(_buy(147841), book)
        self.assertEqual(result["reason"], "canonical_position_blocks_new_id")
        self.assertEqual(result["idem"]["client_order_id"], "bnbfade-147841")
        self.assertTrue(result["idem"]["duplicate_prevented"])
        self.assertFalse(result["allow_buy"])
        self.assertFalse(result["resume_signals"])
        self.assertFalse(result["new_trade"])
        self.assertEqual(result["executed"], 0)

    def test_spent_fill_blocks_a_different_decision_id(self) -> None:
        book = _book(canonical_positions=[])
        result = self.broker.evaluate(_buy(147841), book)
        self.assertEqual(result["reason"], "spent_order_symbol")
        self.assertTrue(result["idem"]["duplicate_prevented"])
        self.assertFalse(result["submitted"])

    def test_same_client_id_is_not_duplicated(self) -> None:
        result = self.broker.evaluate(_buy(147831), _book(canonical_positions=[]))
        self.assertEqual(result["idem"]["client_order_id"], "bnbfade-147831")
        self.assertEqual(result["reason"], "spent_client_order")
        self.assertFalse(result["oco_placed"])
        self.assertFalse(result["oco_promised"])

    def test_quote_floors_to_two_decimals_and_is_not_sent(self) -> None:
        result = self.broker.evaluate(_buy(147841), None)
        self.assertEqual(result["reason"], "protection_tool_absent")
        quote = result["mapping_trace"][0]
        self.assertEqual(quote["raw"], "21.93966")
        self.assertEqual(quote["floored"], "21.93")
        self.assertEqual(quote["precision"], 2)
        self.assertNotEqual(quote["floored"], "21.94")
        self.assertTrue(quote["within_cap"])
        self.assertFalse(quote["sent"])

    def test_quote_reduction_over_five_percent_is_refused(self) -> None:
        result = self.broker.evaluate(
            _buy(147841, quote_order_qty="0.019"),
            None,
        )
        quote = result["mapping_trace"][0]
        self.assertEqual(quote["floored"], "0.01")
        self.assertFalse(quote["within_cap"])
        self.assertEqual(quote["error"], "qty_reduce_exceeds_cap")
        self.assertFalse(quote["sent"])
        self.assertFalse(result["allow_buy"])

    def test_quantity_uses_the_given_step_and_caps_reduction(self) -> None:
        inside = self.broker.evaluate(
            _buy(147841, quantity="140.14", lot_step="0.1"),
            None,
        )
        qty = inside["mapping_trace"][1]
        self.assertEqual(qty["floored"], "140.1")
        self.assertTrue(qty["within_cap"])
        self.assertFalse(qty["sent"])
        outside = self.broker.evaluate(
            _buy(147842, quantity="10.9", lot_step="1"),
            None,
        )
        refused = outside["mapping_trace"][1]
        self.assertEqual(refused["floored"], "10")
        self.assertEqual(refused["error"], "qty_reduce_exceeds_cap")
        missing = self.broker.evaluate(_buy(147843, quantity="140.1"), None)
        self.assertEqual(missing["mapping_trace"][1]["error"], "missing_symbol_filters")
        self.assertIsNone(missing["mapping_trace"][1]["floored"])

    def test_sell_and_oco_stay_blocked_without_a_poll(self) -> None:
        sell = self.broker.evaluate(
            {
                "purpose": "live_bnb_boll_fade",
                "action": "sell",
                "decision_log_id": 147831,
                "extras": {"binance_symbol": "FETUSDT", "quantity": "140.1"},
            },
            _book(),
        )
        self.assertEqual(sell["reason"], "hold_naked_until_native_oco")
        self.assertFalse(sell["allow_sell"])
        self.assertFalse(sell["poll_naked_position"])
        self.assertEqual(sell["parent_queue"][0]["kind"], "sell")
        self.assertFalse(sell["parent_queue"][0]["release"])
        self.assertFalse(sell["parent_queue"][0]["invoke"])

        blocked = self.broker.evaluate(_buy(147841), _book())
        kinds = [item["kind"] for item in blocked["parent_queue"]]
        self.assertEqual(kinds, ["buy", "oco", "sell"])
        self.assertTrue(all(item["release"] is False for item in blocked["parent_queue"]))
        self.assertTrue(all(item["invoke"] is False for item in blocked["parent_queue"]))
        oco = blocked["parent_queue"][1]
        self.assertIsNone(oco["tool"])
        self.assertEqual(oco["documented_rest"], DOCUMENTED_OCO_REST)
        self.assertEqual(oco["operation_id"], DOCUMENTED_OCO_OPERATION)
        self.assertFalse(oco["in_connector"])
        encoded = json.dumps(blocked)
        self.assertNotIn(WEBSOCKET_NOT_MCP, encoded)
        self.assertNotIn("order/oco", encoded)
        self.assertFalse(self.broker.background)
        self.assertFalse(blocked["background"])
        self.assertFalse(blocked["transport_called"])


if __name__ == "__main__":
    unittest.main()
