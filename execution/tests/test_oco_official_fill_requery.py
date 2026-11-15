"""Official ALL_DONE OCO must be requeried and booked, even with no query client.

    python -m unittest execution.tests.test_oco_official_fill_requery
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_spot import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    BinanceSpotAdapter,
)
from execution.executors.binance_spot_gate import buy_exposure_block  # noqa: E402
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.ledger import ExecutionLedger  # noqa: E402
from execution.run import tick_live_bnb_exits  # noqa: E402

_ZEC_LIST_ID = "26026914687"
_ZEC_STOP_ID = "6952313182"
_ZEC_TP_ID = "6952313183"


def _zec_stop_done() -> dict[str, Any]:
    """Official list 26026914687 after the stop filled and the take-profit expired."""
    return {
        "orderListId": int(_ZEC_LIST_ID),
        "symbol": "ZECUSDT",
        "listOrderStatus": "ALL_DONE",
        "listStatusType": "ALL_DONE",
        "orderReports": [
            {
                "symbol": "ZECUSDT",
                "orderId": int(_ZEC_TP_ID),
                "type": "LIMIT_MAKER",
                "side": "SELL",
                "status": "EXPIRED",
                "executedQty": "0",
                "origQty": "0.041",
            },
            {
                "symbol": "ZECUSDT",
                "orderId": int(_ZEC_STOP_ID),
                "type": "STOP_LOSS_LIMIT",
                "side": "SELL",
                "status": "FILLED",
                "executedQty": "0.041",
                "origQty": "0.041",
                "price": "942.77",
            },
        ],
    }


class _ListHttp:
    """Stand-in for BinanceSpotHttpClient. One outcome for every list read."""

    made: list["_ListHttp"] = []
    outcome: Any = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls: list[tuple[Any, ...]] = []
        _ListHttp.made.append(self)

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order_list", signed_query, api_key))
        outcome = _ListHttp.outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        raise AssertionError("the list payload already has the legs")

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        raise AssertionError("an official fill must not market-sell")

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        raise AssertionError("an official fill must not post another OCO")


class _NoQuery:
    """Order client with no order-list read. Maintenance must not sell through it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append("new_order")
        raise AssertionError("must not sell")

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append("new_oco")
        raise AssertionError("must not repost")


def _stuck_zec() -> dict[str, Any]:
    return {
        "id": 12,
        "decision_log_id": 148652,
        "symbol": "ZECUSDT",
        "status": "oco_working",
        "protection": "exchange_oco",
        "quantity": "0.041",
        "stop_price": "942.77",
        "take_profit_price": "1100",
        "entry_order_id": "6950000001",
        "oco_order_list_id": _ZEC_LIST_ID,
        "oco_list_status": "EXECUTING",
        "last_error": "oco_requery_unsupported",
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
    }


def _assert_terminal_stop(test: unittest.TestCase, row: dict[str, Any]) -> None:
    test.assertEqual(row["status"], "oco_filled", row)
    test.assertEqual(row["oco_list_status"], "ALL_DONE", row)
    test.assertEqual(row["exit_reason"], "stop_loss", row)
    test.assertEqual(row["exit_filled_qty"], "0.041", row)
    test.assertEqual(row["residual_qty"], "0", row)
    test.assertEqual(str(row["exit_order_id"]), _ZEC_STOP_ID, row)
    test.assertIsNone(row["last_error"], row)
    test.assertNotEqual(row["status"], "oco_working")
    test.assertNotEqual(row["oco_list_status"], "EXECUTING")


class OfficialOcoFillRequeryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _ListHttp.made = []
        _ListHttp.outcome = _zec_stop_done()

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _env(self) -> dict[str, str]:
        return {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "BINANCE_SPOT_PRODUCTION_BOOK": "rest_api_key",
            "BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH": "",
            "BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL": "0",
            "BINANCE_API_KEY": "test-key",
            "BINANCE_API_SECRET": "test-secret",
            "LIVE_EXCHANGE_API_KEY": "",
            "LIVE_EXCHANGE_API_SECRET": "",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
        }

    def test_non_queryable_client_books_official_stop_and_releases_other_symbol(self) -> None:
        stub = _NoQuery()
        adapter = BinanceSpotAdapter(
            client=stub,
            allow_rest_http=True,
            production_book="rest_api_key",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            kill_switch_path=self.root / "absent-kill",
        )
        saved: dict[str, Any] = {}

        def persist(fields: dict[str, Any]) -> dict[str, Any]:
            saved.update(fields)
            return {**_stuck_zec(), **saved}

        with mock.patch.dict(os.environ, self._env()):
            with mock.patch(
                "execution.executors.binance_spot.BinanceSpotHttpClient",
                _ListHttp,
            ):
                outcome = adapter.maintain_held_position(_stuck_zec(), None, persist=persist)
        row = {**_stuck_zec(), **saved}
        _assert_terminal_stop(self, row)
        self.assertEqual(outcome["book"], "oco_filled", outcome)
        self.assertIsNone(outcome.get("error"), outcome)
        self.assertTrue(_ListHttp.made)
        listed = _ListHttp.made[0].calls
        self.assertTrue(listed and listed[0][0] == "get_order_list", listed)
        self.assertIn(f"orderListId={_ZEC_LIST_ID}", listed[0][1])
        self.assertIs(adapter.client, stub)
        self.assertEqual(stub.calls, [])
        blocked = buy_exposure_block(
            [row],
            [],
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            symbol="SOPHUSDT",
        )
        self.assertIsNone(blocked, "SOPHUSDT refused while ZEC is an in-flight readback")

    def test_live_loop_without_a_client_books_the_stop_and_drops_the_inflight_block(self) -> None:
        ledger = ExecutionLedger(self.root / "execution_ledger_live_bnb_boll_fade.db")
        armed = ledger.arm_binance_spot_position(
            {
                "decision_log_id": 148652,
                "symbol": "ZECUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "oco_working",
                "quantity": "0.041",
                "stop_price": "942.77",
                "take_profit_price": "1100",
                "entry_price": "980",
                "entry_order_id": "6950000001",
                "protection": "exchange_oco",
            }
        )
        ledger.update_binance_spot_position(
            int(armed["id"]),
            {
                "status": "oco_working",
                "protection": "exchange_oco",
                "oco_order_list_id": _ZEC_LIST_ID,
                "oco_list_status": "EXECUTING",
                "last_error": "oco_requery_unsupported",
                "quantity": "0.041",
            },
        )
        ledger.close()
        with mock.patch.dict(os.environ, self._env()):
            with mock.patch(
                "execution.executors.binance_spot.BinanceSpotHttpClient",
                _ListHttp,
            ):
                with ExecutionGateway(
                    decision_db=self.root / "decision.db",
                    ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
                    paper_enabled=False,
                    pm_enabled=False,
                    accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                ) as gw:
                    done = tick_live_bnb_exits(gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}))
                    row = gw.ledger.binance_spot_position_by_decision(148652)
                    blocks = gw.ledger.list_binance_spot_buy_blocks(
                        purpose=PURPOSE_LIVE_BNB_BOLL_FADE
                    )
                    adapter = gw._binance_spot
        self.assertEqual(done[0]["position_status"], "oco_filled", done[0])
        assert row is not None
        _assert_terminal_stop(self, row)
        self.assertEqual(blocks, [])
        self.assertIsNone(
            buy_exposure_block(
                blocks,
                [],
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                symbol="SOPHUSDT",
            )
        )
        self.assertTrue(_ListHttp.made)
        self.assertIn("get_order_list", [call[0] for call in _ListHttp.made[0].calls])
        assert adapter is not None
        self.assertIsNone(adapter.client)

    def test_timeout_requery_is_not_booked_as_filled(self) -> None:
        _ListHttp.outcome = TimeoutError("timed out")
        ledger = ExecutionLedger(self.root / "execution_ledger_live_bnb_boll_fade.db")
        armed = ledger.arm_binance_spot_position(
            {
                "decision_log_id": 148652,
                "symbol": "ZECUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "oco_working",
                "quantity": "0.041",
                "stop_price": "942.77",
                "take_profit_price": "1100",
                "entry_order_id": "6950000001",
                "protection": "exchange_oco",
            }
        )
        ledger.update_binance_spot_position(
            int(armed["id"]),
            {
                "oco_order_list_id": _ZEC_LIST_ID,
                "oco_list_status": "EXECUTING",
                "last_error": "oco_requery_unsupported",
                "status": "oco_working",
            },
        )
        ledger.close()
        with mock.patch.dict(os.environ, self._env()):
            with mock.patch(
                "execution.executors.binance_spot.BinanceSpotHttpClient",
                _ListHttp,
            ):
                with ExecutionGateway(
                    decision_db=self.root / "decision.db",
                    ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
                    paper_enabled=False,
                    pm_enabled=False,
                    accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                ) as gw:
                    done = tick_live_bnb_exits(gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}))
                    row = gw.ledger.binance_spot_position_by_decision(148652)
        self.assertNotEqual(done[0]["position_status"], "oco_filled", done[0])
        self.assertEqual(done[0]["error"], "oco_requery_transport", done[0])
        assert row is not None
        self.assertEqual(row["status"], "oco_working")
        self.assertEqual(row["oco_list_status"], "EXECUTING")
        self.assertEqual(row["last_error"], "oco_requery_transport")
        self.assertIsNone(row["exit_filled_qty"])
        self.assertIsNone(row["exit_reason"])
        self.assertTrue(_ListHttp.made)
        self.assertIn("get_order_list", [call[0] for call in _ListHttp.made[0].calls])


if __name__ == "__main__":
    unittest.main()
