"""Production-path safety for ratchet, time-stop, unverified OCO, and maker rests.

Uses ``ExecutionLedger`` persistence, not an in-memory dict that keeps fields
the ledger drops. No live HTTP.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_spot import (  # noqa: E402
    BinanceRequestError,
    BinanceSpotAdapter,
)
from execution.executors.binance_spot_gate import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    REST_BOOK,
    buy_exposure_block,
    funding_after_reservation,
    reserved_quote_usdt,
    row_quote_usdt,
)
from execution.executors.binance_spot_protect import PROTECTION_EXCHANGE  # noqa: E402
from decision_engine.core.regime_strategy_router import evaluate_five_state_regime  # noqa: E402
from decision_engine.core.data_adapter import IndicatorDataAdapter  # noqa: E402
from decision_engine.core.engine import DecisionEngine  # noqa: E402
from decision_engine.core.risk_manager import RiskManager  # noqa: E402
from decision_engine.core.signal_bus import SignalBus  # noqa: E402
from decision_engine.strategies import get_strategy  # noqa: E402
from decision_engine.tests.test_published_signal_contract import _seed_state3  # noqa: E402
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.ledger import ExecutionLedger  # noqa: E402

_INFO = {
    "symbol": "BNBUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "isSpotTradingAllowed": True,
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01", "maxQty": "100000"},
        {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
        {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "stepSize": "0.01", "maxQty": "100000"},
    ],
}
_BOUND = {
    "accountType": "SPOT",
    "canTrade": True,
    "permissions": ["SPOT"],
    "restrictions": {
        "enableSpotAndMarginTrading": True,
        "enableReading": True,
        "ipRestrict": True,
    },
    "balances": [{"asset": "USDT", "free": "1000.00", "locked": "0"}],
}


def _working(list_id: int, above: int, below: int, client: str) -> dict[str, Any]:
    return {
        "orderListId": list_id,
        "listStatusType": "EXEC_STARTED",
        "listOrderStatus": "EXECUTING",
        "listClientOrderId": client,
        "symbol": "BNBUSDT",
        "orders": [{"orderId": above}, {"orderId": below}],
        "orderReports": [
            {"orderId": above, "type": "LIMIT_MAKER", "status": "NEW", "executedQty": "0"},
            {"orderId": below, "type": "STOP_LOSS_LIMIT", "status": "NEW", "executedQty": "0"},
        ],
    }


class _Exchange:
    def __init__(self) -> None:
        self.cancels: list[str] = []
        self.entry_cancels: list[str] = []
        self.list_queries: list[str] = []
        self.ocos: list[str] = []
        self.orders: list[str] = []
        self.sells: list[str] = []
        self.oco_mode = "ok"
        self.sell_mode = "ok"
        self.list_mode = "working"
        self.hold_list_timeout_after_cancel = False
        self.cancel_ack: dict[str, Any] | None = None
        self.after_cancel: dict[str, Any] | None = None
        self.cancel_error: BaseException | None = None
        self.cancel_list_once: dict[str, Any] | None = None
        self.sell_ack: dict[str, Any] | None = None
        self.entry: dict[str, Any] = {
            "orderId": 91001,
            "symbol": "BNBUSDT",
            "status": "NEW",
            "executedQty": "0.00",
            "cummulativeQuoteQty": "0",
            "price": "99.95",
            "time": 1_700_000_000_000,
        }
        self.exit_order: dict[str, Any] | None = None
        self.exit_lookup = "absent"

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        return dict(_INFO, symbol=symbol)

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "price": "100"}

    def order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        return {"bids": [["99.95", "50"]], "asks": [["100.05", "50"]]}

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.list_queries.append(signed_query)
        if self.list_mode == "absent":
            raise BinanceRequestError(400, code=-2018, message="Order list does not exist.")
        if self.list_mode == "timeout":
            raise TimeoutError("list timed out")
        if self.list_mode == "working_unverified_ack":
            return _working(5002, 11, 12, "bnbfade-ol-44")
        return _working(5001, 6001, 6002, "bnbfade-ol-42")

    def cancel_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.cancels.append(signed_query)
        if self.cancel_list_once is not None:
            body = self.cancel_list_once
            self.cancel_list_once = None
            return body
        if self.hold_list_timeout_after_cancel:
            self.list_mode = "timeout"
        elif self.list_mode != "timeout":
            self.list_mode = "absent"
        return {
            "orderListId": 5001,
            "listStatusType": "ALL_DONE",
            "listOrderStatus": "ALL_DONE",
            "orderReports": [
                {"orderId": 6001, "type": "LIMIT_MAKER", "status": "CANCELED", "executedQty": "0"},
                {"orderId": 6002, "type": "STOP_LOSS_LIMIT", "status": "CANCELED", "executedQty": "0"},
            ],
        }

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.ocos.append(signed_query)
        if self.oco_mode == "timeout":
            raise TimeoutError("oco timed out")
        if self.oco_mode == "reject":
            raise BinanceRequestError(400, code=-2010, message="Account has insufficient balance")
        if self.oco_mode == "unverified":
            return {
                "orderListId": 5002,
                "listOrderStatus": "EXECUTING",
                "listStatusType": "EXEC_STARTED",
            }
        client = parse_qs(signed_query).get("listClientOrderId", ["new"])[0]
        return _working(5000 + len(self.ocos), 7000 + len(self.ocos), 8000 + len(self.ocos), client)

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.orders.append(signed_query)
        form = parse_qs(signed_query)
        if form.get("side", [""])[0] == "SELL":
            self.sells.append(signed_query)
            if self.sell_mode == "timeout":
                raise TimeoutError("sell timed out")
            qty = form.get("quantity", ["1"])[0]
            self.exit_order = self.sell_ack or {
                "orderId": 77,
                "status": "FILLED",
                "executedQty": qty,
                "origQty": qty,
                "cummulativeQuoteQty": "100",
            }
            self.exit_lookup = "found"
            return self.exit_order
        return dict(self.entry)

    def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        form = parse_qs(signed_query)
        if "origClientOrderId" in form:
            if self.exit_lookup == "timeout":
                raise TimeoutError("lookup timed out")
            if self.exit_lookup == "absent" or self.exit_order is None:
                raise BinanceRequestError(400, code=-2013, message="Order does not exist.")
            return self.exit_order
        return self.entry

    def cancel_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.entry_cancels.append(signed_query)
        if self.cancel_error is not None:
            err = self.cancel_error
            self.cancel_error = None
            raise err
        ack = self.cancel_ack or dict(
            self.entry,
            status="CANCELED",
            executedQty=self.entry.get("executedQty") or "0",
        )
        if self.after_cancel is not None:
            self.entry = dict(self.after_cancel)
        else:
            self.entry = dict(self.entry, status="CANCELED", executedQty=self.entry.get("executedQty") or "0")
        return ack


class PersistenceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {
                "BINANCE_SPOT_LIVE_ENABLED": "1",
                "BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL": "0",
                "BINANCE_SPOT_TRAILING_RATCHET": "0",
                "BINANCE_SPOT_MAX_HOLD_SEC": "0",
                "BINANCE_SPOT_POST_ONLY_LIMIT": "0",
                "BINANCE_SPOT_REANCHOR_OCO_ON_FILL": "0",
            },
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _ledger(self) -> ExecutionLedger:
        return ExecutionLedger(self.root / "execution_ledger_live_bnb_boll_fade.db")

    def _held(self, ledger: ExecutionLedger, decision_id: int = 42) -> dict[str, Any]:
        row = ledger.arm_binance_spot_position(
            {
                "decision_log_id": decision_id,
                "symbol": "BNBUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "oco_working",
                "quantity": "1.00",
                "stop_price": "96",
                "take_profit_price": "104",
                "entry_price": "100",
                "entry_order_id": "9",
                "protection": PROTECTION_EXCHANGE,
                "created_at_ms": "1700000000000",
            }
        )
        saved = ledger.update_binance_spot_position(
            int(row["id"]),
            {
                "oco_order_list_id": "5001",
                "oco_list_client_id": f"bnbfade-ol-{decision_id}",
                "oco_above_order_id": "6001",
                "oco_below_order_id": "6002",
                "status": "oco_working",
            },
        )
        assert saved is not None
        return saved

    def test_ratchet_sequence_survives_the_ledger_and_does_not_reuse_the_id(self) -> None:
        ledger = self._ledger()
        row = self._held(ledger)
        client = _Exchange()
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_000_000,
            trailing_ratchet=True,
        )
        pid = int(row["id"])

        def persist(fields: dict[str, Any]) -> dict[str, Any] | None:
            return ledger.update_binance_spot_position(pid, fields)

        first = adapter.maintain_held_position(dict(row), "102.80", persist=persist)
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertTrue(first["ok"], first)
        self.assertEqual(stored.get("oco_ratchet_count"), "1")
        self.assertEqual(stored.get("initial_stop_price"), "96")
        self.assertEqual(
            parse_qs(client.ocos[0]).get("listClientOrderId"),
            ["bnbfade-ol-42-r1"],
        )
        client.list_mode = "working"
        client.get_order_list = lambda q, *, api_key: _working(  # type: ignore[method-assign]
            5002, 7001, 8001, "bnbfade-ol-42-r1"
        )
        second = adapter.maintain_held_position(
            dict(stored), "103.40", persist=persist
        )
        self.assertTrue(second["ok"], second)
        self.assertEqual(
            [parse_qs(q).get("listClientOrderId", ["?"])[0] for q in client.ocos],
            ["bnbfade-ol-42-r1", "bnbfade-ol-42-r2"],
        )
        self.assertEqual(ledger.binance_spot_position_by_id(pid).get("oco_ratchet_count"), "2")

    def test_time_stop_timeout_is_reloaded_without_a_second_oco(self) -> None:
        ledger = self._ledger()
        row = self._held(ledger, 43)
        client = _Exchange()
        client.sell_mode = "timeout"
        client.exit_lookup = "timeout"
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_003_700_000,
            max_hold_sec=3600,
        )
        pid = int(row["id"])

        def persist(fields: dict[str, Any]) -> dict[str, Any] | None:
            return ledger.update_binance_spot_position(pid, fields)

        first = adapter.maintain_held_position(dict(row), "100.5", persist=persist)
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertEqual(first["book"], "time_stop_unknown")
        self.assertEqual(first["error"], "time_stop_submit_unknown")
        self.assertEqual(len(client.sells), 1)
        self.assertEqual(len(client.ocos), 0)
        self.assertEqual(stored["status"], "time_stop_unknown")
        self.assertEqual(stored["exit_client_order_id"], "bnbfade-x-43")

        reloaded = ledger.binance_spot_position_by_id(pid)
        assert reloaded is not None
        second = adapter.maintain_held_position(dict(reloaded), "100.5", persist=persist)
        self.assertEqual(second["book"], "time_stop_unknown")
        self.assertEqual(len(client.sells), 1)
        self.assertEqual(len(client.ocos), 0)

        client.exit_lookup = "found"
        client.exit_order = {"orderId": 77, "status": "FILLED", "executedQty": "1.00"}
        third = adapter.maintain_held_position(
            dict(ledger.binance_spot_position_by_id(pid) or {}),
            "100.5",
            persist=persist,
        )
        self.assertEqual(third["book"], "closed")
        self.assertEqual(len(client.sells), 1)
        self.assertEqual(len(client.ocos), 0)
        self.assertEqual(ledger.binance_spot_position_by_id(pid).get("status"), "closed")

    def test_unverified_oco_ack_does_not_flatten_while_the_list_is_live(self) -> None:
        ledger = self._ledger()
        row = self._held(ledger, 44)
        ledger.update_binance_spot_position(
            int(row["id"]),
            {"status": "open", "protection": "oco_pending"},
        )
        client = _Exchange()
        client.oco_mode = "unverified"
        client.list_mode = "working_unverified_ack"
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_000_000,
            auto_flatten_on_oco_fail=True,
        )
        pid = int(row["id"])
        outcome = adapter.maintain_held_position(
            dict(ledger.binance_spot_position_by_id(pid) or {}),
            "100",
            persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
        )
        self.assertEqual(outcome["book"], "oco_working")
        self.assertEqual(client.sells, [])
        self.assertEqual(ledger.binance_spot_position_by_id(pid).get("status"), "oco_working")

        client.list_mode = "absent"
        client.oco_mode = "unverified"
        naked = self._held(ledger, 45)
        nid = int(naked["id"])
        ledger.update_binance_spot_position(nid, {"status": "open", "protection": "oco_pending"})
        flat = adapter.maintain_held_position(
            dict(ledger.binance_spot_position_by_id(nid) or {}),
            "100",
            persist=lambda fields: ledger.update_binance_spot_position(nid, fields),
        )
        self.assertEqual(flat["book"], "closed")
        self.assertEqual(len(client.sells), 1)
        self.assertIn("oco_failed_auto_flattened", str(flat.get("error")))

    def test_resting_maker_reserves_partial_qty_and_ttl_is_terminal(self) -> None:
        client = _Exchange()
        client.entry = {
            "orderId": 91001,
            "symbol": "BNBUSDT",
            "status": "NEW",
            "executedQty": "0.00",
            "cummulativeQuoteQty": "0",
            "price": "99.95",
            "time": 1_700_000_000_000,
        }
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected={"account_type": "SPOT", "usdt_free": "1000.00"},
            post_only_limit=True,
            entry_ttl_sec=180,
        )
        result = adapter.execute(
            {
                "decision_log_id": 7,
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "mode": "live_stub",
                "action": "buy",
                "token_address": "BNBUSDT",
                "extras": {
                    "binance_symbol": "BNBUSDT",
                    "order_type": "LIMIT",
                    "quantity": "1",
                    "price": "100.20",
                    "price_usd": "100",
                    "stop_price": "90",
                    "take_profit_price": "120",
                    "entry_reason": "large_trade_market",
                },
            }
        )
        self.assertEqual(result["error"], "maker_entry_resting")
        self.assertEqual(client.ocos, [])
        self.assertEqual(parse_qs(client.orders[0]).get("type"), ["LIMIT_MAKER"])
        self.assertNotIn("timeInForce", client.orders[0])
        arm = result["arm_position"]
        ledger = self._ledger()
        armed = ledger.arm_binance_spot_position(arm)
        self.assertEqual(armed["status"], "maker_resting")
        self.assertEqual(armed["entry_order_qty"], "1")
        self.assertIsNotNone(row_quote_usdt(armed))
        self.assertGreater(row_quote_usdt(armed) or 0, 0)
        self.assertEqual(
            buy_exposure_block([armed], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="BNBUSDT"),
            "maker_resting_blocks_buy",
        )
        self.assertIsNone(
            buy_exposure_block([armed], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ETHUSDT")
        )
        self.assertGreater(
            reserved_quote_usdt([armed]),
            0,
        )

        pid = int(armed["id"])
        gateway = ExecutionGateway(
            decision_db=self.root / "decision.db",
            ledger_path=ledger.path,
            paper_enabled=False,
            pm_enabled=False,
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            binance_spot=adapter,
        )
        adapter.clock_ms = lambda: 1_700_000_200_000
        gateway.tick_binance_protective_exits({"BNBUSDT": "100"})
        closed = ledger.binance_spot_position_by_id(pid)
        assert closed is not None
        self.assertEqual(closed["status"], "closed_canceled")
        self.assertEqual(client.ocos, [])
        self.assertEqual(len(client.entry_cancels), 1)
        self.assertIsNone(
            buy_exposure_block([closed], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="BNBUSDT")
        )

    def test_restart_adopts_a_filled_maker_and_protects_only_that_qty(self) -> None:
        ledger = self._ledger()
        armed = ledger.arm_binance_spot_position(
            {
                "decision_log_id": 8,
                "symbol": "BNBUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "maker_resting",
                "quantity": None,
                "stop_price": "90",
                "take_profit_price": "120",
                "entry_price": "99.95",
                "entry_order_id": "91001",
                "protection": "maker_resting",
                "entry_order_qty": "1",
                "entry_limit_price": "99.95",
                "created_at_ms": "1700000000000",
            }
        )
        client = _Exchange()
        client.entry = {
            "orderId": 91001,
            "symbol": "BNBUSDT",
            "status": "FILLED",
            "executedQty": "1.00",
            "cummulativeQuoteQty": "99.95",
            "price": "90",
            "time": 1_700_000_000_000,
        }
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_030_000,
        )
        gateway = ExecutionGateway(
            decision_db=self.root / "decision.db",
            ledger_path=ledger.path,
            paper_enabled=False,
            pm_enabled=False,
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            binance_spot=adapter,
        )
        gateway.tick_binance_protective_exits({"BNBUSDT": "100"})
        self.assertEqual(len(client.ocos), 1)
        form = parse_qs(client.ocos[0])
        self.assertEqual(form.get("quantity"), ["1"])
        self.assertEqual(form.get("aboveType"), ["LIMIT_MAKER"])
        self.assertNotIn("price", form)
        self.assertNotIn("stopPrice", form)
        self.assertNotIn("stopLimitPrice", form)
        stored = ledger.binance_spot_position_by_id(int(armed["id"]))
        assert stored is not None
        self.assertEqual(stored["status"], "oco_working")
        self.assertEqual(stored["entry_price"], "99.95")

    def _arm_resting(self, ledger: ExecutionLedger, *, created_at_ms: str = "1700000000000") -> dict[str, Any]:
        return ledger.arm_binance_spot_position(
            {
                "decision_log_id": 11,
                "symbol": "BNBUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "maker_resting",
                "quantity": None,
                "stop_price": "90",
                "take_profit_price": "120",
                "entry_price": "99.95",
                "entry_order_id": "91001",
                "protection": "maker_resting",
                "entry_order_qty": "1",
                "entry_limit_price": "99.95",
                "created_at_ms": created_at_ms,
            }
        )

    def _gateway(self, ledger: ExecutionLedger, adapter: BinanceSpotAdapter) -> ExecutionGateway:
        return ExecutionGateway(
            decision_db=self.root / "decision.db",
            ledger_path=ledger.path,
            paper_enabled=False,
            pm_enabled=False,
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            binance_spot=adapter,
        )

    def test_partial_fill_cancels_the_remainder_and_protects_only_the_fill(self) -> None:
        ledger = self._ledger()
        armed = self._arm_resting(ledger)
        client = _Exchange()
        client.entry = dict(
            client.entry,
            status="PARTIALLY_FILLED",
            executedQty="0.40",
            cummulativeQuoteQty="39.98",
        )
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_030_000,
        )
        self._gateway(ledger, adapter).tick_binance_protective_exits({"BNBUSDT": "100"})
        self.assertEqual(len(client.entry_cancels), 1)
        self.assertEqual(len(client.ocos), 1)
        form = parse_qs(client.ocos[0])
        self.assertEqual(Decimal(form["quantity"][0]), Decimal("0.40"))
        self.assertNotEqual(Decimal(form["quantity"][0]), Decimal("1"))
        stored = ledger.binance_spot_position_by_id(int(armed["id"]))
        assert stored is not None
        self.assertEqual(stored["status"], "oco_working")
        self.assertEqual(Decimal(stored["quantity"]), Decimal("0.40"))
        self.assertEqual(client.orders, [])

    def test_ttl_cancel_protects_a_fill_that_lands_during_the_cancel(self) -> None:
        ledger = self._ledger()
        armed = self._arm_resting(ledger, created_at_ms="1700000000000")
        client = _Exchange()
        client.entry = dict(client.entry, status="NEW", executedQty="0.00", cummulativeQuoteQty="0")
        client.cancel_ack = {
            "orderId": 91001,
            "symbol": "BNBUSDT",
            "status": "CANCELED",
            "executedQty": "0.00",
            "cummulativeQuoteQty": "0",
        }
        client.after_cancel = {
            "orderId": 91001,
            "symbol": "BNBUSDT",
            "status": "CANCELED",
            "executedQty": "0.40",
            "cummulativeQuoteQty": "39.98",
        }
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_200_000,
        )
        self._gateway(ledger, adapter).tick_binance_protective_exits({"BNBUSDT": "100"})
        self.assertEqual(len(client.ocos), 1)
        self.assertEqual(Decimal(parse_qs(client.ocos[0])["quantity"][0]), Decimal("0.40"))
        stored = ledger.binance_spot_position_by_id(int(armed["id"]))
        assert stored is not None
        self.assertEqual(stored["status"], "oco_working")
        self.assertNotEqual(stored["status"], "closed_canceled")

    def test_cancel_unknown_keeps_the_reservation_until_restart_confirms_the_fill(self) -> None:
        ledger = self._ledger()
        armed = self._arm_resting(ledger)
        pid = int(armed["id"])
        client = _Exchange()
        client.entry = dict(
            client.entry,
            status="PARTIALLY_FILLED",
            executedQty="0.40",
            cummulativeQuoteQty="39.98",
        )
        client.cancel_error = TimeoutError("cancel timed out")
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_030_000,
        )
        self._gateway(ledger, adapter).tick_binance_protective_exits({"BNBUSDT": "100"})
        unknown = ledger.binance_spot_position_by_id(pid)
        assert unknown is not None
        self.assertEqual(unknown["status"], "maker_cancel_unknown")
        self.assertEqual(client.ocos, [])
        self.assertEqual(client.orders, [])
        reserved = row_quote_usdt(unknown)
        assert reserved is not None
        self.assertGreater(reserved, 0)
        self.assertEqual(
            buy_exposure_block([unknown], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ETHUSDT"),
            "maker_resting_blocks_buy",
        )

        restarted = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_040_000,
        )
        self._gateway(ledger, restarted).tick_binance_protective_exits({"BNBUSDT": "100"})
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertEqual(stored["status"], "oco_working")
        self.assertEqual(len(client.ocos), 1)
        self.assertEqual(Decimal(parse_qs(client.ocos[0])["quantity"][0]), Decimal("0.40"))
        self.assertEqual(client.orders, [])

    def test_cancel_race_does_not_drop_or_inflate_the_fill(self) -> None:
        ledger = self._ledger()
        armed = self._arm_resting(ledger)
        client = _Exchange()
        client.entry = dict(
            client.entry,
            status="PARTIALLY_FILLED",
            executedQty="0.40",
            cummulativeQuoteQty="39.98",
        )
        client.cancel_ack = {
            "orderId": 91001,
            "status": "CANCELED",
            "executedQty": "0.00",
            "cummulativeQuoteQty": "0",
        }
        client.after_cancel = {
            "orderId": 91001,
            "symbol": "BNBUSDT",
            "status": "CANCELED",
            "executedQty": "0.55",
            "cummulativeQuoteQty": "54.97",
        }
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_030_000,
        )
        self._gateway(ledger, adapter).tick_binance_protective_exits({"BNBUSDT": "100"})
        self.assertEqual(len(client.ocos), 1)
        qty = Decimal(parse_qs(client.ocos[0])["quantity"][0])
        self.assertEqual(qty, Decimal("0.55"))
        stored = ledger.binance_spot_position_by_id(int(armed["id"]))
        assert stored is not None
        self.assertEqual(Decimal(stored["quantity"]), Decimal("0.55"))
        self.assertNotEqual(stored["status"], "closed_canceled")

    def test_persisted_created_at_ms_does_not_arm_max_hold_without_config(self) -> None:
        os.environ.pop("BINANCE_SPOT_MAX_HOLD_SEC", None)
        ledger = self._ledger()
        row = self._held(ledger)
        self.assertEqual(row.get("created_at_ms"), "1700000000000")
        self.assertIn(row.get("max_hold_sec"), (None, "", "0"))
        self.assertIn(row.get("max_hold_min"), (None, "", "0"))
        client = _Exchange()
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_800_000_000_000,
        )
        self.assertIsNone(adapter.max_hold_sec)
        self._gateway(ledger, adapter).tick_binance_protective_exits({"BNBUSDT": "120"})
        stored = ledger.binance_spot_position_by_id(int(row["id"]))
        assert stored is not None
        self.assertEqual(stored["status"], "oco_working")
        self.assertEqual(client.sells, [])
        self.assertEqual(client.orders, [])
        self.assertEqual(stored.get("created_at_ms"), "1700000000000")

    def test_oco_replacement_timeout_does_not_repost_or_flatten(self) -> None:
        ledger = self._ledger()
        row = self._held(ledger)
        pid = int(row["id"])
        client = _Exchange()
        client.oco_mode = "timeout"
        client.hold_list_timeout_after_cancel = True
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_000_000,
            trailing_ratchet=True,
            auto_flatten_on_oco_fail=True,
        )

        def tick(mark: str) -> None:
            self._gateway(ledger, adapter).tick_binance_protective_exits({"BNBUSDT": mark})

        tick("102.80")
        first = ledger.binance_spot_position_by_id(pid)
        assert first is not None
        self.assertEqual(first["status"], "oco_unknown")
        self.assertEqual(first.get("oco_ratchet_count"), "1")
        self.assertIn(first.get("oco_order_list_id"), (None, ""))
        self.assertEqual(len(client.ocos), 1)
        self.assertEqual(parse_qs(client.ocos[0]).get("listClientOrderId"), ["bnbfade-ol-42-r1"])
        self.assertEqual(client.sells, [])
        self.assertTrue(
            any("origClientOrderId=bnbfade-ol-42-r1" in query for query in client.list_queries)
        )
        self.assertFalse(any("orderListId=5001" in query for query in client.list_queries[1:]))

        tick("103.40")
        tick("103.80")
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertEqual(stored["status"], "oco_unknown")
        self.assertEqual(stored.get("oco_ratchet_count"), "1")
        self.assertEqual(len(client.ocos), 1)
        self.assertEqual(client.sells, [])
        self.assertFalse(any("bnbfade-ol-42-r2" in query for query in client.ocos))
        self.assertFalse(any("bnbfade-ol-42-r3" in query for query in client.ocos))

    def test_maker_buy_is_refused_before_post_when_cancel_is_unavailable(self) -> None:
        client = _Exchange()
        client.cancel_order = None  # type: ignore[method-assign]
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected={"account_type": "SPOT", "usdt_free": "1000.00"},
            post_only_limit=True,
        )
        result = adapter.execute(
            {
                "decision_log_id": 12,
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "mode": "live_stub",
                "action": "buy",
                "token_address": "BNBUSDT",
                "extras": {
                    "binance_symbol": "BNBUSDT",
                    "order_type": "LIMIT",
                    "quantity": "1",
                    "price": "100.20",
                    "price_usd": "100",
                    "stop_price": "90",
                    "take_profit_price": "120",
                    "entry_reason": "large_trade_market",
                },
            }
        )
        self.assertEqual(result["error"], "maker_lifecycle_unsupported")
        self.assertFalse(result["submitted"])
        self.assertEqual(result["executed"], 0)
        self.assertEqual(client.orders, [])
        self.assertEqual(client.ocos, [])

    def _buy(
        self,
        extras: dict[str, Any],
        decision_id: int = 70,
        *,
        limit: bool = True,
    ) -> dict[str, Any]:
        body = {
            "binance_symbol": "BNBUSDT",
            "price_usd": "100",
            "stop_price": "90",
            "take_profit_price": "120",
            "entry_reason": "large_trade_market",
        }
        if limit:
            body["quantity"] = "1"
            body["price"] = "99.90"
        body.update(extras)
        return {
            "decision_log_id": decision_id,
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "mode": "live_stub",
            "action": "buy",
            "token_address": "BNBUSDT",
            "extras": body,
        }

    def test_recommended_limit_maker_is_the_order_when_none_was_explicit(self) -> None:
        client = _Exchange()
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected={"account_type": "SPOT", "usdt_free": "1000.00"},
            post_only_limit=False,
        )
        result = adapter.execute(
            self._buy({"recommended_order_type": "LIMIT_MAKER", "enable_trailing_ratchet": True})
        )
        self.assertEqual(result["error"], "maker_entry_resting")
        self.assertEqual(parse_qs(client.orders[0]).get("type"), ["LIMIT_MAKER"])
        self.assertNotIn("timeInForce", client.orders[0])
        self.assertEqual(client.ocos, [])

        explicit = adapter.execute(
            self._buy(
                {
                    "order_type": "LIMIT",
                    "recommended_order_type": "LIMIT_MAKER",
                    "price": "99.90",
                },
                71,
            )
        )
        self.assertEqual(parse_qs(client.orders[-1]).get("type"), ["LIMIT"])
        self.assertEqual(parse_qs(client.orders[-1]).get("timeInForce"), ["GTC"])
        self.assertEqual(explicit["error"], "maker_entry_resting")
        self.assertEqual(client.ocos, [])

        none = adapter.execute(
            self._buy({"recommended_order_type": "NONE", "quote_order_qty": "20"}, 72, limit=False)
        )
        self.assertEqual(parse_qs(client.orders[-1]).get("type"), ["MARKET"])

        from decision_engine.core.signal_bus import normalize_signal

        norm = normalize_signal(
            {
                "action": "buy",
                "strategy_id": "tech",
                "token_address": "BNBUSDT",
                "binance_symbol": "BNBUSDT",
                "size_pct": 0.1,
                "recommended_order_type": "LIMIT_MAKER",
                "volume_z": 1.2,
                "stop_price": "90",
                "take_profit_price": "120",
                "quantity": "1",
                "price": "99.90",
                "price_usd": "100",
                "entry_reason": "large_trade_market",
            },
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
        )
        extras = norm["_extras"]
        self.assertEqual(extras.get("recommended_order_type"), "LIMIT_MAKER")
        self.assertEqual(extras.get("volume_z"), 1.2)
        self.assertNotIn("order_type", extras)
        carried = adapter.execute(
            {
                "decision_log_id": 73,
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "mode": "live_stub",
                "action": "buy",
                "token_address": "BNBUSDT",
                "extras": extras,
            }
        )
        self.assertEqual(carried["error"], "maker_entry_resting")
        self.assertEqual(parse_qs(client.orders[-1]).get("type"), ["LIMIT_MAKER"])

    def test_published_state3_signal_posts_limit_maker_without_network(self) -> None:
        now_ms = int(time.time() * 1000)
        db = self.root / "decision.db"
        book = self.root / "public_derivatives.db"
        _seed_state3(db, book, now_ms)
        saved = {
            key: os.environ.get(key)
            for key in (
                "PUBLIC_DERIVATIVES_DB_PATH",
                "NEWS_EVENTS_DB_PATH",
                "LIVE_BNB_FACTOR_SOURCE",
                "BINANCE_SPOT_BOOK_USDT",
            )
        }
        os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = str(book)
        os.environ["NEWS_EVENTS_DB_PATH"] = str(self.root / "missing-news.db")
        os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
        os.environ.pop("BINANCE_SPOT_BOOK_USDT", None)
        indicator = IndicatorDataAdapter(db)
        bus = SignalBus(db, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
        try:
            payload = DecisionEngine(
                indicator,
                bus,
                [get_strategy("tech_boll_fade_v1")],
                risk=RiskManager(cooldown_sec=0),
            ).tick()
        finally:
            bus.close()
            indicator.close()
        self.assertEqual(payload["emitted"], 1)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        stored = conn.execute(
            "SELECT action, size_pct, raw_json FROM decision_log ORDER BY id"
        ).fetchone()
        conn.close()
        assert stored is not None
        raw = json.loads(stored["raw_json"])
        extras = dict(raw["extras"])
        self.assertEqual(stored["action"], "buy")
        self.assertFalse(extras.get("news_shock_applied"))
        self.assertEqual(extras["recommended_order_type"], "LIMIT_MAKER")
        self.assertEqual(extras["order_type"], "MARKET")
        self.assertEqual(extras["obi_top5"], -0.10)
        self.assertEqual(extras["obi_top5_source_ts_ms"], now_ms)
        self.assertNotIn("orderbook_imbalance_20", extras)
        self.assertLess(extras["volume_z"], 3.8)
        os.environ["BINANCE_SPOT_BOOK_USDT"] = "1000"
        client = _Exchange()
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: now_ms,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected={"account_type": "SPOT", "usdt_free": "1000.00"},
            post_only_limit=False,
        )
        result = adapter.execute(
            {
                "decision_log_id": int(stored["id"]) if "id" in stored.keys() else 80,
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "mode": "live_stub",
                "action": stored["action"],
                "token_address": "FADEUSDT",
                "size_pct": stored["size_pct"],
                "extras": extras,
            }
        )
        self.assertEqual(result["error"], "maker_entry_resting", result)
        self.assertEqual(len(client.orders), 1)
        self.assertEqual(client.ocos, [])
        form = {key: values[0] for key, values in parse_qs(client.orders[0]).items()}
        form.pop("signature", None)
        self.assertEqual(form.get("symbol"), "FADEUSDT")
        self.assertEqual(form.get("side"), "BUY")
        self.assertEqual(form.get("type"), "LIMIT_MAKER")
        self.assertNotIn("timeInForce", form)
        self.assertNotIn("orderbook_imbalance_20", form)
        self.assertNotIn("-0.55", client.orders[0])
        self.assertTrue(form.get("quantity"))
        self.assertTrue(form.get("price"))
        artifact = {
            "news_shock_applied": extras.get("news_shock_applied"),
            "obi_top5": extras.get("obi_top5"),
            "obi_top5_source_ts_ms": extras.get("obi_top5_source_ts_ms"),
            "volume_z": extras.get("volume_z"),
            "order": {key: form[key] for key in ("symbol", "side", "type", "quantity", "price") if key in form},
        }
        out = self.root / "state3_order.json"
        out.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
        self.assertEqual(artifact["order"]["type"], "LIMIT_MAKER")
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _held_qty(self, ledger: ExecutionLedger, qty: str, decision_id: int = 61) -> tuple[int, BinanceSpotAdapter, _Exchange]:
        row = self._held(ledger, decision_id)
        pid = int(row["id"])
        ledger.update_binance_spot_position(pid, {"quantity": qty})
        client = _Exchange()
        adapter = BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_800_000_000_000,
        )
        return pid, adapter, client

    def _tick(self, ledger: ExecutionLedger, adapter: BinanceSpotAdapter, mark: str = "100") -> None:
        self._gateway(ledger, adapter).tick_binance_protective_exits({"BNBUSDT": mark})

    def test_ratchet_after_partial_oco_fill_keeps_remainder_1_10(self) -> None:
        ledger = self._ledger()
        pid, adapter, client = self._held_qty(ledger, "1.50")
        adapter.trailing_ratchet = True
        client.cancel_list_once = {
            "orderListId": 5001,
            "listOrderStatus": "ALL_DONE",
            "listStatusType": "ALL_DONE",
            "orderReports": [
                {
                    "orderId": 6001,
                    "type": "LIMIT_MAKER",
                    "status": "PARTIALLY_FILLED",
                    "executedQty": "0.40",
                    "origQty": "1.50",
                },
                {
                    "orderId": 6002,
                    "type": "STOP_LOSS_LIMIT",
                    "status": "CANCELED",
                    "executedQty": "0",
                    "origQty": "1.50",
                },
            ],
        }
        self._tick(ledger, adapter, "102.80")
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertEqual(Decimal(stored["quantity"]), Decimal("1.10"))
        self.assertNotEqual(stored["status"], "closed")
        self.assertGreaterEqual(len(client.ocos), 1)
        for query in client.ocos:
            self.assertLessEqual(Decimal(parse_qs(query)["quantity"][0]), Decimal("1.10"))
        self._tick(ledger, adapter, "103.40")
        again = ledger.binance_spot_position_by_id(pid)
        assert again is not None
        self.assertEqual(Decimal(again["quantity"]), Decimal("1.10"))
        for query in client.ocos:
            self.assertLessEqual(Decimal(parse_qs(query)["quantity"][0]), Decimal("1.10"))
        self.assertEqual(client.sells, [])

    def test_time_stop_new_zero_fill_does_not_close_or_resell(self) -> None:
        ledger = self._ledger()
        pid, adapter, client = self._held_qty(ledger, "1.50")
        adapter.max_hold_sec = 60
        client.sell_ack = {"orderId": 77, "status": "NEW", "executedQty": "0", "origQty": "1.50"}
        self._tick(ledger, adapter, "100")
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertEqual(stored["status"], "time_stop_unknown")
        self.assertEqual(Decimal(stored["quantity"]), Decimal("1.50"))
        self.assertEqual(len(client.sells), 1)
        self._tick(ledger, adapter, "100")
        again = ledger.binance_spot_position_by_id(pid)
        assert again is not None
        self.assertNotEqual(again["status"], "closed")
        self.assertEqual(Decimal(again["quantity"]), Decimal("1.50"))
        self.assertEqual(len(client.sells), 1)

    def test_time_stop_partial_fill_keeps_remainder_1_10(self) -> None:
        ledger = self._ledger()
        pid, adapter, client = self._held_qty(ledger, "1.50")
        adapter.max_hold_sec = 60
        client.sell_ack = {
            "orderId": 77,
            "status": "PARTIALLY_FILLED",
            "executedQty": "0.40",
            "origQty": "1.50",
        }
        self._tick(ledger, adapter, "100")
        self._tick(ledger, adapter, "100")
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertNotEqual(stored["status"], "closed")
        self.assertEqual(Decimal(stored["quantity"]), Decimal("1.10"))
        self.assertEqual(len(client.sells), 1)
        self.assertEqual(Decimal(parse_qs(client.sells[0])["quantity"][0]), Decimal("1.50"))

    def test_flatten_new_zero_fill_does_not_close_or_resell(self) -> None:
        ledger = self._ledger()
        pid, adapter, client = self._held_qty(ledger, "1.50", 62)
        adapter.auto_flatten_on_oco_fail = True
        client.list_mode = "absent"
        client.oco_mode = "reject"
        client.sell_ack = {"orderId": 78, "status": "NEW", "executedQty": "0.00", "origQty": "1.50"}
        ledger.update_binance_spot_position(pid, {"status": "open", "protection": "oco_pending"})
        self._tick(ledger, adapter, "100")
        self._tick(ledger, adapter, "100")
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertNotEqual(stored["status"], "closed")
        self.assertEqual(Decimal(stored["quantity"]), Decimal("1.50"))
        self.assertEqual(len(client.sells), 1)

    def test_flatten_partial_fill_keeps_remainder_1_10(self) -> None:
        ledger = self._ledger()
        pid, adapter, client = self._held_qty(ledger, "1.50", 63)
        adapter.auto_flatten_on_oco_fail = True
        client.list_mode = "absent"
        client.oco_mode = "reject"
        client.sell_ack = {
            "orderId": 79,
            "status": "PARTIALLY_FILLED",
            "executedQty": "0.40",
            "origQty": "1.50",
        }
        ledger.update_binance_spot_position(pid, {"status": "open", "protection": "oco_pending"})
        self._tick(ledger, adapter, "100")
        self._tick(ledger, adapter, "100")
        stored = ledger.binance_spot_position_by_id(pid)
        assert stored is not None
        self.assertNotEqual(stored["status"], "closed")
        self.assertEqual(Decimal(stored["quantity"]), Decimal("1.10"))
        self.assertEqual(len(client.sells), 1)
        self.assertLessEqual(Decimal(parse_qs(client.sells[0])["quantity"][0]), Decimal("1.50"))

    def _maker_adapter(self, client: _Exchange, ws: object | None = None) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            api_key="k",
            api_secret="s",
            kill_switch_path=self.root / "no-kill",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected={"account_type": "SPOT", "usdt_free": "1000.00"},
            post_only_limit=False,
            ws_cache=ws,
        )

    def test_stale_ws_bid_does_not_override_a_fresh_rest_book(self) -> None:
        client = _Exchange()
        client.order_book = lambda symbol, limit=5: {  # type: ignore[method-assign]
            "bids": [["100.70", "5"]],
            "asks": [["100.80", "5"]],
        }

        class _Stale:
            def get_ticker_price(self, symbol: str) -> dict[str, str | int]:
                return {
                    "best_bid": "100.10",
                    "best_ask": "100.20",
                    "price": "100.15",
                    "ws_age_ms": 900_000,
                }

        result = self._maker_adapter(client, _Stale()).execute(
            self._buy({"recommended_order_type": "LIMIT_MAKER", "price": "101"})
        )
        self.assertEqual(result["error"], "maker_entry_resting", result)
        self.assertEqual(Decimal(parse_qs(client.orders[0])["price"][0]), Decimal("100.70"))
        self.assertEqual(parse_qs(client.orders[0]).get("type"), ["LIMIT_MAKER"])

    def test_stale_ws_mark_uses_rest_ticker_and_does_not_read_wall_mid(self) -> None:
        os.environ["BINANCE_SPOT_WS_MAX_AGE_MS"] = "2500"
        client = _Exchange()
        client.ticker_price = lambda symbol: {"symbol": symbol, "price": "100.55"}  # type: ignore[method-assign]
        client.order_book = lambda symbol, limit=5: {  # type: ignore[method-assign]
            "bids": [["100.50", "5"]],
            "asks": [["100.60", "5"]],
        }

        class _Mark:
            def __init__(self, price: str, age: object) -> None:
                self.price = price
                self.age = age

            def get_ticker_price(self, symbol: str) -> dict[str, object]:
                payload: dict[str, object] = {"price": self.price, "source": "ws_l1"}
                if self.age is not None:
                    payload["ws_age_ms"] = self.age
                return payload

        adapter = self._maker_adapter(client, _Mark("90", 900_000))
        result = adapter.execute(
            self._buy(
                {
                    "recommended_order_type": "LIMIT_MAKER",
                    "price": "100.80",
                    "price_usd": "100.8",
                    "stop_price": "99",
                    "take_profit_price": "120",
                    "wall_mid_price": "88.88",
                }
            )
        )
        self.assertEqual(result["error"], "maker_entry_resting", result)
        form = parse_qs(client.orders[0])
        self.assertEqual(form.get("type"), ["LIMIT_MAKER"])
        self.assertEqual(Decimal(form["price"][0]), Decimal("100.50"))
        self.assertNotIn("88.88", client.orders[0])
        self.assertEqual(adapter._mark_telemetry.get("mark_source"), "rest_fallback")

        fresh = self._maker_adapter(client, _Mark("100.40", 2500))
        mark, err = fresh._read_entry_mark(client, "BNBUSDT")
        self.assertIsNone(err)
        self.assertEqual(mark, "100.40")

        stale = self._maker_adapter(client, _Mark("90", 2501))
        mark, err = stale._read_entry_mark(client, "BNBUSDT")
        self.assertIsNone(err)
        self.assertEqual(mark, "100.55")

        missing_age = self._maker_adapter(client, _Mark("100.25", None))
        mark, err = missing_age._read_entry_mark(client, "BNBUSDT")
        self.assertIsNone(err)
        self.assertEqual(mark, "100.55")

        invalid = self._maker_adapter(client, _Mark("90", "stale"))
        mark, err = invalid._read_entry_mark(client, "BNBUSDT")
        self.assertIsNone(err)
        self.assertEqual(mark, "100.55")

        def _ticker_down(symbol: str) -> dict[str, str]:
            raise TimeoutError("ticker down")

        client.ticker_price = _ticker_down  # type: ignore[method-assign]
        closed = self._maker_adapter(client, _Mark("90", 2501))
        mark, err = closed._read_entry_mark(client, "BNBUSDT")
        self.assertIsNone(mark)
        self.assertEqual(err, "mark_unavailable")
        refused = closed.execute(
            self._buy(
                {
                    "recommended_order_type": "LIMIT_MAKER",
                    "price": "100.80",
                    "stop_price": "99",
                    "take_profit_price": "120",
                },
                76,
            )
        )
        self.assertEqual(refused["error"], "mark_unavailable")
        self.assertEqual(refused["submitted"], False)
        self.assertEqual(len(client.orders), 1)

    def test_unproved_ws_age_uses_rest_for_mark_and_book(self) -> None:
        os.environ["BINANCE_SPOT_WS_MAX_AGE_MS"] = "2500"
        accepted = (0, 1, 2500, "0", "2500", "2500.0", " 1500 ")
        rejected = (
            None,
            "",
            "nan",
            "inf",
            "-inf",
            "NaN",
            "Infinity",
            "stale",
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            False,
            -1,
            10**18,
            2501,
            "-5",
            "2500.5",
        )
        for age in accepted:
            payload = {"ws_age_ms": age}
            try:
                got = BinanceSpotAdapter._ws_age_ms(payload)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"accepted age {age!r} raised {type(exc).__name__}: {exc}")
            self.assertIsNotNone(got, age)
            self.assertLessEqual(got, 2500)
        missing_key: dict[str, object] = {}
        self.assertIsNone(BinanceSpotAdapter._ws_age_ms(missing_key))
        for age in rejected:
            payload = {"ws_age_ms": age}
            try:
                got = BinanceSpotAdapter._ws_age_ms(payload)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"rejected age {age!r} raised {type(exc).__name__}: {exc}")
            self.assertIsNone(got, age)

        client = _Exchange()
        client.ticker_price = lambda symbol: {"symbol": symbol, "price": "100.55"}  # type: ignore[method-assign]
        client.order_book = lambda symbol, limit=5: {  # type: ignore[method-assign]
            "bids": [["100.50", "4"]],
            "asks": [["100.60", "4"]],
        }

        class _Book:
            def __init__(self, age: object, *, present: bool = True) -> None:
                self.age = age
                self.present = present

            def get_ticker_price(self, symbol: str) -> dict[str, object]:
                payload: dict[str, object] = {
                    "price": "100.25",
                    "best_bid": "100.20",
                    "best_ask": "100.30",
                    "source": "ws_l1",
                }
                if self.present:
                    payload["ws_age_ms"] = self.age
                return payload

        for age, present in ((None, False), (None, True), ("stale", True), (float("inf"), True), (2501, True)):
            adapter = self._maker_adapter(client, _Book(age, present=present))
            mark, err = adapter._read_entry_mark(client, "BNBUSDT")
            self.assertIsNone(err, age)
            self.assertEqual(mark, "100.55", age)
            bid, ask, book_err = adapter._maker_top_of_book(client, "BNBUSDT")
            self.assertIsNone(book_err, age)
            self.assertEqual(Decimal(bid or "0"), Decimal("100.50"))
            self.assertEqual(Decimal(ask or "0"), Decimal("100.60"))

        result = self._maker_adapter(client, _Book(None, present=False)).execute(
            self._buy(
                {
                    "recommended_order_type": "LIMIT_MAKER",
                    "price": "100.80",
                    "price_usd": "100.8",
                    "stop_price": "99",
                    "take_profit_price": "120",
                    "wall_mid_price": "88.88",
                },
                77,
            )
        )
        self.assertEqual(result["error"], "maker_entry_resting", result)
        form = parse_qs(client.orders[-1])
        self.assertEqual(form.get("type"), ["LIMIT_MAKER"])
        self.assertEqual(Decimal(form["price"][0]), Decimal("100.50"))
        self.assertNotIn("88.88", client.orders[-1])
        self.assertNotIn("100.20", client.orders[-1])

        def _down(symbol: str, limit: int = 5) -> dict[str, str]:
            raise TimeoutError("rest down")

        client.ticker_price = _down  # type: ignore[method-assign]
        client.order_book = _down  # type: ignore[method-assign]
        before = len(client.orders)
        refused = self._maker_adapter(client, _Book(None, present=False)).execute(
            self._buy(
                {
                    "recommended_order_type": "LIMIT_MAKER",
                    "order_type": "MARKET",
                    "price": "100.80",
                    "stop_price": "99",
                    "take_profit_price": "120",
                },
                78,
            )
        )
        self.assertEqual(refused["submitted"], False)
        self.assertIn(refused["error"], {"mark_unavailable", "maker_book_unavailable"})
        self.assertEqual(len(client.orders), before)
        self.assertNotEqual(refused["error"], "maker_entry_resting")

    def test_crossed_or_missing_book_does_not_invent_a_maker_price(self) -> None:
        crossed = _Exchange()
        crossed.order_book = lambda symbol, limit=5: {  # type: ignore[method-assign]
            "bids": [["100.20", "1"]],
            "asks": [["100.10", "1"]],
        }
        class _NoWs:
            pass

        refused = self._maker_adapter(crossed, _NoWs()).execute(
            self._buy({"recommended_order_type": "LIMIT_MAKER", "price": "100.79", "price_usd": "100.80"})
        )
        self.assertEqual(refused["error"], "orderbook_crossed")
        self.assertEqual(crossed.orders, [])

        missing = _Exchange()
        missing.order_book = lambda symbol, limit=5: {}  # type: ignore[method-assign]
        absent = self._maker_adapter(missing, _NoWs()).execute(
            self._buy(
                {"recommended_order_type": "LIMIT_MAKER", "price": "100.79", "price_usd": "100.80"},
                74,
            )
        )
        self.assertEqual(absent["error"], "maker_book_unavailable")
        self.assertEqual(absent["submitted"], False)
        self.assertEqual(missing.orders, [])

        market = self._maker_adapter(missing, _NoWs()).execute(
            self._buy({"order_type": "MARKET", "recommended_order_type": "NONE", "quote_order_qty": "20"}, 75, limit=False)
        )
        self.assertEqual(parse_qs(missing.orders[0]).get("type"), ["MARKET"])
        self.assertNotEqual(market["error"], "maker_book_unavailable")

    def test_unfilled_maker_reserves_its_symbol_and_not_another(self) -> None:
        resting = {
            "symbol": "BNBUSDT",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "status": "maker_resting",
            "quantity": None,
            "entry_order_id": "91001",
            "entry_order_qty": "1",
            "entry_limit_price": "99.95",
            "entry_price": "99.95",
            "protection": "maker_resting",
        }
        self.assertEqual(
            buy_exposure_block([resting], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="BNBUSDT"),
            "maker_resting_blocks_buy",
        )
        self.assertIsNone(
            buy_exposure_block([resting], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ETHUSDT")
        )
        self.assertEqual(reserved_quote_usdt([resting]), Decimal("99.95"))
        self.assertEqual(
            funding_after_reservation("120", Decimal("20"), "99.95", Decimal("100")),
            "funding_block",
        )
        naked = dict(resting, status="open", protection="held_position_exit", quantity="1")
        self.assertEqual(
            buy_exposure_block([naked], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ETHUSDT"),
            "unprotected_position_blocks_buy",
        )
        unknown = dict(resting, status="maker_cancel_unknown", quantity="0.40")
        self.assertEqual(
            buy_exposure_block([unknown], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ETHUSDT"),
            "maker_resting_blocks_buy",
        )

    def test_published_limit_maker_recommendation_covers_eligible_states(self) -> None:
        cases = {
            "state1": {},
            "state2": {"funding_z": -2.0},
            "state3": {"xs_breadth_1h": 0.80, "xs_rank_pct_1h": 0.90, "obi_top5": -0.10, "volume_z": 1.2},
            "state4": {"funding_z": 1.9},
        }
        for name, row in cases.items():
            decision = evaluate_five_state_regime(row)
            self.assertTrue(decision.allow_buy, name)
            self.assertEqual(decision.recommended_order_type, "LIMIT_MAKER", name)
        halted = evaluate_five_state_regime({}, macro_halt="macro")
        self.assertFalse(halted.allow_buy)
        self.assertEqual(halted.recommended_order_type, "NONE")
