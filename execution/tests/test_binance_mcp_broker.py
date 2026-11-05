"""Catalog gate. No network and no order.

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
    HOLD_NAKED_NO_AUTO_SELL,
    WEBSOCKET_NOT_MCP,
    LiveMCPBroker,
    parent_calls,
    report_availability,
)

_CAPTURED_WITHOUT_OCO = frozenset(
    {
        "spot.newOrder",
        "spot.getOrder",
        "spot.getOpenOrders",
        "spot.getAccount",
        "spot.deleteOrder",
        "spot.exchangeInfo",
    }
)
_OCO_TOOL = "spot.orderList.oco"

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
                "quantity": "140.1",
                "stop_price": "0.1400",
                "take_profit_price": "0.1800",
            }
        ],
        "spent_orders": [dict(_FILLED)],
        "open_orders": [],
        "base_free": "140.1",
    }
    base.update(overrides)
    return base


def _buy(decision_id: int, **extras: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"binance_symbol": "FETUSDT", "quote_order_qty": "21.93966"}
    body.update(extras)
    return {
        "purpose": "live_bnb_boll_fade",
        "action": "buy",
        "mode": "live_stub",
        "decision_log_id": decision_id,
        "extras": body,
    }


class _Probe:
    def __init__(self, names: frozenset[str]) -> None:
        self._names = names
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def names(self) -> frozenset[str]:
        return self._names

    def invoke(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append((tool, arguments))
        raise AssertionError("broker must not invoke")


def _assert_observed(test: unittest.TestCase, result: dict[str, Any], catalog: frozenset[str]) -> None:
    report = report_availability(catalog=catalog)
    test.assertEqual(result["availability"], report)
    test.assertTrue(report["original_api_key"])
    test.assertFalse(report["original_api_key_healthy"])
    test.assertFalse(report["buy"])
    test.assertFalse(report["sell"])
    test.assertFalse(report["poll_naked"])
    test.assertFalse(result["transport_called"])
    test.assertFalse(result["new_trade"])
    test.assertEqual(result["executed"], 0)


class BrokerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = LiveMCPBroker()

    def test_original_api_key_route_is_recognized_and_unhealthy(self) -> None:
        observed = report_availability(catalog=_CAPTURED_WITHOUT_OCO)
        self.assertTrue(observed["original_api_key"])
        self.assertFalse(observed["original_api_key_healthy"])
        self.assertFalse(observed["protection_tool"])
        self.assertFalse(observed["oco"])
        self.assertFalse(observed["resume_signals"])
        empty = self.broker.probe()
        self.assertFalse(empty["handshake"])
        self.assertEqual(empty["empty_handshake_observed_tool_count"], 72)
        self.assertEqual(empty["availability"], report_availability(catalog=()))
        self.assertTrue(self.broker.availability("original_api_key"))
        self.assertFalse(self.broker.availability("original_api_key_healthy"))
        self.assertFalse(self.broker.availability("protection_tool"))

    def test_env_key_is_not_read_and_a_passed_key_is_not_a_rejection(self) -> None:
        sentinel = "sentinelsecret-not-a-channel"
        os.environ["BINANCE_API_KEY"] = sentinel
        os.environ["BINANCE_API_SECRET"] = sentinel
        try:
            result = self.broker.evaluate(_buy(147841), _book(), api_key=sentinel)
        finally:
            os.environ.pop("BINANCE_API_KEY", None)
            os.environ.pop("BINANCE_API_SECRET", None)
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertNotEqual(result["reason"], "raw_api_key_rejected")
        self.assertTrue(result["original_api_key_available"])
        self.assertFalse(result["original_api_key_healthy"])
        self.assertNotEqual(result["parent_queue"], [])
        _assert_observed(self, result, frozenset())

    def test_new_decision_id_cannot_buy_over_naked_hold(self) -> None:
        book = _book(spent_orders=[])
        result = self.broker.evaluate(_buy(147841), book)
        self.assertEqual(result["reason"], "naked_holds_new_buys")
        self.assertEqual(result["idem"]["client_order_id"], "bnbfade-147841")
        self.assertTrue(result["idem"]["duplicate_prevented"])
        self.assertFalse(result["allow_buy"])
        self.assertFalse(result["resume_signals"])
        _assert_observed(self, result, frozenset())

    def test_spent_fill_does_not_block_a_later_client_on_the_symbol(self) -> None:
        book = _book(canonical_positions=[])
        result = self.broker.evaluate(_buy(147841), book)
        self.assertEqual(result["reason"], "protection_tool_absent")
        self.assertFalse(result["idem"]["duplicate_prevented"])
        self.assertFalse(result["submitted"])
        self.assertFalse(result["allow_buy"])
        _assert_observed(self, result, frozenset())

    def test_same_client_id_is_not_duplicated(self) -> None:
        result = self.broker.evaluate(_buy(147831), _book(canonical_positions=[]))
        self.assertEqual(result["idem"]["client_order_id"], "bnbfade-147831")
        self.assertEqual(result["reason"], "spent_client_order")
        self.assertFalse(result["oco_placed"])
        self.assertFalse(result["oco_promised"])
        _assert_observed(self, result, frozenset())

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
        _assert_observed(self, result, frozenset())

    def test_quote_cap_is_reversible_and_does_not_apply_to_base_floor(self) -> None:
        capped = self.broker.evaluate(_buy(147841, quote_order_qty="0.019"), None)
        quote = capped["mapping_trace"][0]
        self.assertEqual(quote["floored"], "0.01")
        self.assertFalse(quote["within_cap"])
        self.assertEqual(quote["error"], "qty_reduce_exceeds_cap")
        self.assertFalse(quote["sent"])
        _assert_observed(self, capped, frozenset())
        opened = LiveMCPBroker(quote_reduce_cap=None).evaluate(
            _buy(147841, quote_order_qty="0.019"),
            None,
        )
        self.assertEqual(opened["mapping_trace"][0]["floored"], "0.01")
        self.assertTrue(opened["mapping_trace"][0]["within_cap"])
        self.assertIsNone(opened["mapping_trace"][0]["error"])
        self.assertFalse(opened["mapping_trace"][0]["sent"])
        _assert_observed(self, opened, frozenset())

    def test_base_floor_is_allowed_without_the_quote_cap(self) -> None:
        inside = self.broker.evaluate(
            _buy(147841, quantity="140.14", lot_step="0.1"),
            None,
        )
        qty = inside["mapping_trace"][1]
        self.assertEqual(qty["floored"], "140.1")
        self.assertTrue(qty["allow_base_floor"])
        self.assertIsNone(qty["error"])
        self.assertFalse(qty["sent"])
        wide = self.broker.evaluate(
            _buy(147842, quantity="10.9", lot_step="1"),
            None,
        )
        floored = wide["mapping_trace"][1]
        self.assertEqual(floored["floored"], "10")
        self.assertTrue(floored["allow_base_floor"])
        self.assertIsNone(floored["error"])
        missing = self.broker.evaluate(_buy(147843, quantity="140.1"), None)
        self.assertEqual(missing["mapping_trace"][1]["error"], "missing_symbol_filters")
        self.assertFalse(missing["mapping_trace"][1]["allow_base_floor"])
        _assert_observed(self, inside, frozenset())
        _assert_observed(self, wide, frozenset())
        _assert_observed(self, missing, frozenset())

    def test_sell_stays_blocked_and_observed_oco_is_not_released(self) -> None:
        self.assertTrue(HOLD_NAKED_NO_AUTO_SELL)
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
        self.assertFalse(sell["auto_sell"])
        self.assertFalse(sell["poll_naked_position"])
        self.assertEqual(sell["parent_queue"][0]["kind"], "sell")
        self.assertFalse(sell["parent_queue"][0]["release"])
        self.assertFalse(sell["parent_queue"][0]["invoke"])
        self.assertEqual(parent_calls(sell), [])
        _assert_observed(self, sell, frozenset())

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
        _assert_observed(self, blocked, frozenset())

    def test_catalog_with_native_oco_releases_parent_call_and_not_a_buy(self) -> None:
        probe = _Probe(_CAPTURED_WITHOUT_OCO | {_OCO_TOOL, WEBSOCKET_NOT_MCP})
        broker = LiveMCPBroker(transport=probe)
        later = report_availability(catalog=_CAPTURED_WITHOUT_OCO | {_OCO_TOOL})
        self.assertTrue(later["protection_tool"])
        self.assertTrue(later["oco"])
        self.assertTrue(later["resume_signals"])
        self.assertTrue(later["original_api_key"])
        self.assertFalse(later["original_api_key_healthy"])
        self.assertFalse(later["buy"])
        self.assertFalse(later["sell"])
        websocket_only = report_availability(catalog={WEBSOCKET_NOT_MCP})
        self.assertFalse(websocket_only["protection_tool"])

        result = broker.evaluate(_buy(147841), _book())
        self.assertTrue(result["handshake"])
        self.assertTrue(result["protection_tool_present"])
        self.assertTrue(result["resume_signals"])
        self.assertEqual(result["availability"], later)
        self.assertFalse(result["allow_buy"])
        self.assertFalse(result["allow_sell"])
        self.assertFalse(result["oco_placed"])
        self.assertFalse(result["oco_promised"])
        self.assertEqual(probe.invocations, [])
        buy = result["parent_queue"][0]
        oco = result["parent_queue"][1]
        sell = result["parent_queue"][2]
        self.assertFalse(buy["release"])
        self.assertEqual(oco["tool"], _OCO_TOOL)
        self.assertTrue(oco["release"])
        self.assertFalse(oco["invoke"])
        self.assertTrue(oco["dry_plan"])
        self.assertEqual(oco["arguments"]["symbol"], "FETUSDT")
        self.assertEqual(oco["arguments"]["side"], "SELL")
        self.assertEqual(oco["arguments"]["aboveType"], "LIMIT_MAKER")
        self.assertEqual(oco["arguments"]["belowType"], "STOP_LOSS_LIMIT")
        self.assertFalse(sell["release"])
        self.assertEqual(parent_calls(result), [oco])
        self.assertNotIn(WEBSOCKET_NOT_MCP, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
