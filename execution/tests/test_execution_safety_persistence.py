"""Production-path safety for ratchet, time-stop, unverified OCO, and maker rests.

Uses ``ExecutionLedger`` persistence, not an in-memory dict that keeps fields
the ledger drops. No live HTTP.
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

from execution.executors.binance_spot import (  # noqa: E402
    BinanceRequestError,
    BinanceSpotAdapter,
)
from execution.executors.binance_spot_gate import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    REST_BOOK,
    buy_exposure_block,
    row_quote_usdt,
)
from execution.executors.binance_spot_protect import PROTECTION_EXCHANGE  # noqa: E402
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
        self.ocos: list[str] = []
        self.orders: list[str] = []
        self.sells: list[str] = []
        self.oco_mode = "ok"
        self.sell_mode = "ok"
        self.list_mode = "working"
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
        if self.list_mode == "absent":
            raise BinanceRequestError(400, code=-2018, message="Order list does not exist.")
        if self.list_mode == "timeout":
            raise TimeoutError("list timed out")
        if self.list_mode == "working_unverified_ack":
            return _working(5002, 11, 12, "bnbfade-ol-44")
        return _working(5001, 6001, 6002, "bnbfade-ol-42")

    def cancel_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.cancels.append(signed_query)
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
            self.exit_order = {
                "orderId": 77,
                "status": "FILLED",
                "executedQty": form.get("quantity", ["1"])[0],
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
        self.entry = dict(self.entry, status="CANCELED", executedQty=self.entry.get("executedQty") or "0")
        return self.entry


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
            buy_exposure_block([armed], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ETHUSDT"),
            "maker_resting_blocks_buy",
        )

        client.entry = dict(
            client.entry,
            status="PARTIALLY_FILLED",
            executedQty="0.40",
            cummulativeQuoteQty="39.98",
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
        gateway.tick_binance_protective_exits({"BNBUSDT": "100"})
        partial = ledger.binance_spot_position_by_id(pid)
        assert partial is not None
        self.assertEqual(partial["status"], "maker_resting")
        self.assertEqual(Decimal(partial["quantity"]), Decimal("0.40"))
        self.assertEqual(client.ocos, [])
        reserved = row_quote_usdt(partial)
        assert reserved is not None
        self.assertGreater(reserved, 0)

        adapter.clock_ms = lambda: 1_700_000_200_000
        client.entry = dict(client.entry, status="NEW", executedQty="0.00", cummulativeQuoteQty="0")
        # Quantity already 0.40 from the partial. TTL cancel of a later zero read
        # must not OCO the unfilled remainder. Force a fresh zero-fill rest first.
        ledger.update_binance_spot_position(pid, {"quantity": None, "status": "maker_resting"})
        gateway.tick_binance_protective_exits({"BNBUSDT": "100"})
        closed = ledger.binance_spot_position_by_id(pid)
        assert closed is not None
        self.assertEqual(closed["status"], "closed_canceled")
        self.assertEqual(client.ocos, [])
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
