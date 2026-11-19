"""OCO error codes do not authorize a buyback. Socket is banned before import.

These cases are mock coverage. They are not exchange-empirical proof.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from typing import Any

from execution.margin_cross.api_semantics import (
    build_borrow_payload,
    build_cancel_payload,
    build_oco_payload,
    build_repay_payload,
    make_client_id,
)
from execution.margin_cross.errors import MarginExchangeError
from execution.margin_cross.fixture import NOW_MS, short_case
from execution.margin_cross.http_client import BinanceCrossMarginHttpClient
from execution.margin_cross.track import CrossMarginTrack


def _counts(client: Any) -> dict[str, int]:
    names = [name for name, _payload in client.write_log]
    return {name: names.count(name) for name in ("borrow", "new_order", "new_oco", "repay", "cancel_order")}


def _zero_interest(case: dict[str, Any]) -> dict[str, Any]:
    case["client"].interest_on_borrow = Decimal("0")
    return case


class OcoCodeIsNotProofTest(unittest.TestCase):
    def test_integer_codes_never_count_as_deterministic_rejects(self) -> None:
        samples = [
            (-2010, "Duplicate order sent."),
            (-2010, "Duplicate clientOrderId"),
            (-2010, "duplicate list client id"),
            (-2010, "Account has insufficient balance for requested action."),
            (-1013, "Filter failure: MIN_NOTIONAL"),
            (-1013, "Duplicate order sent."),
            (-1102, "Mandatory parameter was not sent."),
            (-1102, "Duplicate order sent."),
            (-1111, ""),
            (-3006, "EXCEED_MAX_BORROWABLE"),
            (-3045, "INSUFFICIENT_INVENTORY"),
            (-9999, "unknown"),
            (-1000, ""),
        ]
        for code, message in samples:
            err = MarginExchangeError(code, message)
            self.assertFalse(
                err.is_deterministic_pre_exec_oco_reject,
                f"{code} {message!r} must not authorize flatten",
            )
        self.assertFalse(
            MarginExchangeError(-2010, "Account has insufficient balance", http_status=400)
            .is_deterministic_pre_exec_oco_reject
        )
        self.assertFalse(
            MarginExchangeError(-1013, "MIN_NOTIONAL", http_status=500).is_deterministic_pre_exec_oco_reject
        )
        self.assertFalse(
            MarginExchangeError(-1003, "Too many requests", http_status=429).is_deterministic_pre_exec_oco_reject
        )

    def test_duplicate_contradictory_empty_and_transport_errors_do_not_buy_back(self) -> None:
        scripts = [
            (-2010, "Duplicate order sent."),
            (-2010, "Duplicate clientOrderId"),
            (-2010, "duplicate list client id"),
            (-2010, "Account has insufficient balance for requested action."),
            (-1013, "Duplicate order sent."),
            (-1102, ""),
            (-1116, "unknown"),
            (-9999, ""),
        ]
        for code, message in scripts:
            with self.subTest(code=code, message=message):
                self._assert_oco_error_holds(MarginExchangeError(code, message))
        self._assert_oco_error_holds(MarginExchangeError(-1000, "upstream", http_status=503))
        self._assert_oco_error_holds(MarginExchangeError(-1003, "Too many requests", http_status=429))

    def _assert_oco_error_holds(self, error: MarginExchangeError) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = _zero_interest(short_case())
            client = case["client"]
            original = client.new_oco

            def fail_once(payload: dict[str, Any]) -> dict[str, Any]:
                client.write_log.append(("new_oco", dict(payload)))
                raise error

            client.new_oco = fail_once  # type: ignore[method-assign]
            ledger = os.path.join(tmpdir, "oco-hold.db")
            first = CrossMarginTrack(ledger, client).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(first.state, "protection_unknown")
            self.assertNotEqual(first.state, "protection_failed_flattened")
            self.assertIsNone(first.account_flat)
            counts = _counts(client)
            self.assertEqual(counts["borrow"], 1)
            self.assertEqual(counts["new_order"], 1)
            self.assertEqual(counts["new_oco"], 1)
            self.assertEqual(counts["repay"], 0)
            flatten_id = make_client_id(str(case["intent"]["intent_id"]), "flatten")
            self.assertFalse(
                any(
                    payload.get("newClientOrderId") == flatten_id
                    for name, payload in client.write_log
                    if name == "new_order"
                )
            )
            client.new_oco = original  # type: ignore[method-assign]
            second = CrossMarginTrack(ledger, client).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(second.state, "protection_unknown")
            self.assertEqual(_counts(client), counts)

    def test_duplicate_after_list_exists_binds_without_repost_or_repay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = _zero_interest(short_case())
            case["client"].fail_next(
                "new_oco",
                when="after",
                kind="reject",
                code=-2010,
                message="Duplicate order sent.",
            )
            ledger = os.path.join(tmpdir, "landed.db")
            opened = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(opened.state, "protected")
            self.assertTrue(opened.protection_confirmed)
            counts = _counts(case["client"])
            self.assertEqual(counts["new_oco"], 1)
            self.assertEqual(counts["repay"], 0)
            self.assertEqual(counts["new_order"], 1)
            again = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(again.state, "protected")
            self.assertEqual(_counts(case["client"]), counts)

    def test_delayed_oco_query_does_not_repost(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = _zero_interest(short_case())
            case["client"].fail_next(
                "new_oco",
                when="before",
                kind="reject",
                code=-2010,
                message="Duplicate order sent.",
            )
            ledger = os.path.join(tmpdir, "delayed.db")
            intent_id = str(case["intent"]["intent_id"])
            first = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(first.state, "protection_unknown")
            self.assertEqual(_counts(case["client"])["new_oco"], 1)
            list_id = make_client_id(intent_id, "oco-list")
            limit_id = make_client_id(intent_id, "oco-limit")
            stop_id = make_client_id(intent_id, "oco-stop")
            case["client"]._lists[list_id] = {
                "orderListId": 77,
                "listStatusType": "EXEC_STARTED",
                "listOrderStatus": "EXECUTING",
                "listClientOrderId": list_id,
                "symbol": "BTCUSDT",
                "isIsolated": False,
                "orders": [
                    {"symbol": "BTCUSDT", "orderId": 78, "clientOrderId": limit_id},
                    {"symbol": "BTCUSDT", "orderId": 79, "clientOrderId": stop_id},
                ],
            }
            for cid, order_id, price in ((limit_id, 78, "99500"), (stop_id, 79, "100500")):
                case["client"]._orders[cid] = {
                    "symbol": "BTCUSDT",
                    "orderId": order_id,
                    "clientOrderId": cid,
                    "status": "NEW",
                    "side": "BUY",
                    "type": "LIMIT",
                    "origQty": "0.01",
                    "executedQty": "0",
                    "price": price,
                    "isIsolated": "FALSE",
                }
            second = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(second.state, "protected")
            self.assertEqual(_counts(case["client"])["new_oco"], 1)
            self.assertEqual(_counts(case["client"])["repay"], 0)
            self.assertEqual(_counts(case["client"])["new_order"], 1)

    def test_timeout_and_empty_open_orders_do_not_buy_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = _zero_interest(short_case())
            case["client"].fail_next("new_oco", when="before", kind="timeout")
            case["client"].query_open_orders = lambda payload: []  # type: ignore[method-assign]
            ledger = os.path.join(tmpdir, "timeout.db")
            opened = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(opened.state, "protection_unknown")
            self.assertEqual(_counts(case["client"])["new_oco"], 1)
            self.assertEqual(_counts(case["client"])["repay"], 0)
            self.assertEqual(_counts(case["client"])["new_order"], 1)
            restarted = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(restarted.state, "protection_unknown")
            self.assertEqual(_counts(case["client"])["new_oco"], 1)

    def test_sqlite_restart_does_not_duplicate_borrow_sell_oco_buyback_or_repay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = _zero_interest(short_case())
            ledger = os.path.join(tmpdir, "restart.db")
            case["client"].interrupt_after = "borrow"
            paused = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(paused.state, "interrupted_after_borrow")
            self.assertEqual(_counts(case["client"])["borrow"], 1)
            case["client"].interrupt_after = "entry"
            sold = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(sold.state, "interrupted_after_entry")
            self.assertEqual(_counts(case["client"])["new_order"], 1)
            self.assertEqual(_counts(case["client"])["borrow"], 1)
            case["client"].interrupt_after = None
            case["client"].fail_next(
                "new_oco",
                when="before",
                kind="reject",
                code=-2010,
                message="Duplicate order sent.",
            )
            held = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(held.state, "protection_unknown")
            frozen = _counts(case["client"])
            self.assertEqual(frozen, {"borrow": 1, "new_order": 1, "new_oco": 1, "repay": 0, "cancel_order": 0})
            again = CrossMarginTrack(ledger, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(again.state, "protection_unknown")
            self.assertEqual(_counts(case["client"]), frozen)

    def test_external_interest_is_not_repaid_as_this_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = short_case()
            case["snapshot"]["hourly_interest_rate"]["BTC"] = "0.01"
            case["client"].hourly_rate["BTC"] = Decimal("0.01")
            case["contract"]["interest_horizon_hours"] = "1"
            case["contract"]["max_base_over_liability"] = "0.0001"
            track = CrossMarginTrack(os.path.join(tmpdir, "ext.db"), case["client"])
            self.assertEqual(
                track.open_position(
                    case["intent"], case["snapshot"], case["contract"], case["gate"]
                ).state,
                "protected",
            )
            oco = next(payload for name, payload in case["client"].write_log if name == "new_oco")
            case["client"].accrue_interest("BTC", Decimal("0.0001"), principal=Decimal("0.015"))
            case["client"].fill_oco_limit(oco["listClientOrderId"])
            closed = track.complete_exit(case["intent"]["intent_id"], case["gate"])
            self.assertEqual(closed.reason, "same_asset_external_interest_conflict")
            self.assertFalse(closed.account_flat)
            self.assertEqual(_counts(case["client"])["repay"], 0)

    def test_six_write_gates_reject_unpinned_approval_without_network(self) -> None:
        calls: list[str] = []

        def opener(request: object, timeout: float) -> tuple[int, bytes]:
            del request, timeout
            calls.append("open")
            raise AssertionError("write gate bypassed into the opener")

        client = BinanceCrossMarginHttpClient(
            api_key="fixture-key",
            api_secret="fixture-secret",
            account_uid="fixture-uid-not-real",
            opener=opener,
            clock_ms=lambda: NOW_MS,
            environ={
                "CROSS_MARGIN_PARAMETERS_APPROVED": "1",
                "CROSS_MARGIN_LIVE_ENABLED": "1",
            },
        )
        borrow = build_borrow_payload("BTC", Decimal("0.01"))
        repay = build_repay_payload("BTC", Decimal("0.01"))
        order = {
            "symbol": "BTCUSDT",
            "isIsolated": "FALSE",
            "side": "SELL",
            "type": "LIMIT",
            "quantity": "0.01",
            "price": "100000",
            "timeInForce": "GTC",
            "newClientOrderId": make_client_id("gate", "entry"),
            "newOrderRespType": "FULL",
            "sideEffectType": "NO_SIDE_EFFECT",
        }
        cancel = build_cancel_payload("BTCUSDT", make_client_id("gate", "entry"))
        oco = build_oco_payload(
            symbol="BTCUSDT",
            side="BUY",
            quantity=Decimal("0.01"),
            limit_price=Decimal("99500"),
            stop_price=Decimal("100400"),
            stop_limit_price=Decimal("100500"),
            list_client_order_id=make_client_id("gate", "oco-list"),
            limit_client_order_id=make_client_id("gate", "oco-limit"),
            stop_client_order_id=make_client_id("gate", "oco-stop"),
        )
        cancel_oco = {
            "symbol": "BTCUSDT",
            "isIsolated": "FALSE",
            "listClientOrderId": make_client_id("gate", "oco-list"),
        }
        from execution.margin_cross.errors import LiveNetworkDisabled

        for method, payload in (
            ("borrow", borrow),
            ("repay", repay),
            ("new_order", order),
            ("cancel_order", cancel),
            ("new_oco", oco),
            ("cancel_oco", cancel_oco),
        ):
            with self.assertRaises(LiveNetworkDisabled):
                getattr(client, method)(payload)
        self.assertEqual(calls, [])

    def test_import_does_not_open_a_socket(self) -> None:
        script = """
import socket
class Banned(socket.socket):
    def connect(self, address, /):
        raise SystemExit("connect")
    def connect_ex(self, address, /):
        raise SystemExit("connect")
socket.socket = Banned
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(SystemExit("connect"))
import execution.margin_cross.track
import execution.margin_cross.entry
import execution.margin_cross.http_client
print("import-ok")
"""
        env = os.environ.copy()
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("import-ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
