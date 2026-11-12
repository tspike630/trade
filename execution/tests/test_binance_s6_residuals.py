"""Residual S6: reduce-only kill, spot OCO, and dust archive. No live HTTP.

    python -m unittest execution.tests.test_binance_s6_residuals
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_spot import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    BinanceRequestError,
    BinanceSpotAdapter,
    BinanceSpotHttpClient,
    OCO_PATH,
)
from execution.executors.binance_spot_protect import (  # noqa: E402
    ENTRY_MIN_NOTIONAL_USDT,
    build_spot_oco,
    classify_oco,
)
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.run import tick_live_bnb_exits  # noqa: E402

_FILTERS = [
    {
        "filterType": "LOT_SIZE",
        "minQty": "0.01",
        "maxQty": "900000",
        "stepSize": "0.01",
    },
    {
        "filterType": "PRICE_FILTER",
        "minPrice": "0.01",
        "maxPrice": "1000000",
        "tickSize": "0.01",
    },
    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
]
_INFO = {
    "symbol": "BNBUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "isSpotTradingAllowed": True,
    "filters": _FILTERS,
}


def _working_oco() -> dict[str, Any]:
    return {
        "orderListId": 42,
        "contingencyType": "OCO",
        "listStatusType": "EXEC_STARTED",
        "listOrderStatus": "EXECUTING",
        "listClientOrderId": "bnbfade-ol-11",
        "symbol": "BNBUSDT",
        "orders": [
            {"symbol": "BNBUSDT", "orderId": 11, "clientOrderId": "bnbfade-tp-11"},
            {"symbol": "BNBUSDT", "orderId": 10, "clientOrderId": "bnbfade-sl-11"},
        ],
        "orderReports": [
            {
                "orderId": 11,
                "type": "LIMIT_MAKER",
                "side": "SELL",
                "status": "NEW",
                "price": "110",
                "origQty": "1.23",
                "executedQty": "0",
            },
            {
                "orderId": 10,
                "type": "STOP_LOSS_LIMIT",
                "side": "SELL",
                "status": "NEW",
                "price": "98.99",
                "stopPrice": "99",
                "origQty": "1.23",
                "executedQty": "0",
            },
        ],
    }


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.oco_mode = "accept"
        self.list_payload: dict[str, Any] | Exception = _working_oco()
        self.legs: dict[str, dict[str, Any]] = {}

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _INFO

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        if "side=BUY" in signed_query:
            return {"orderId": "99", "status": "FILLED", "executedQty": "1.239"}
        return {"orderId": "77", "status": "FILLED", "executedQty": "1.23"}

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        if self.oco_mode == "timeout":
            self.oco_mode = "accept"
            raise TimeoutError("timed out")
        if self.oco_mode == "reject":
            raise BinanceRequestError(400)
        return _working_oco()

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order_list", signed_query, api_key))
        if isinstance(self.list_payload, Exception):
            raise self.list_payload
        return self.list_payload

    def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order", signed_query, api_key))
        form = parse_qs(signed_query)
        order_id = form.get("orderId", ["?"])[0]
        if order_id in self.legs:
            return self.legs[order_id]
        raise BinanceRequestError(400)

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": "102"}


def _env(root: Path, *, enabled: str, block_sell: str = "0") -> dict[str, str]:
    return {
        "BINANCE_SPOT_LIVE_ENABLED": enabled,
        "BINANCE_SPOT_BLOCK_SELL_ON_KILL": block_sell,
        "BINANCE_SPOT_TAKER_FEE_BPS": "10",
        "LIVE_KILL_SWITCH_PATH": str(root / "LIVE_TRADING_DISABLED"),
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "LIVE_EXCHANGE_API_KEY": "",
        "LIVE_EXCHANGE_API_SECRET": "",
        "BINANCE_SPOT_API_BASE": "https://api.binance.test",
    }


def _extras(**overrides: Any) -> dict[str, Any]:
    extras: dict[str, Any] = {
        "binance_symbol": "BNBUSDT",
        "market": "spot",
        "order_type": "MARKET",
        "quote_order_qty": "25",
        "price_usd": 102.0,
        "stop_price": 99.0,
        "take_profit_price": 110.0,
        "entry_reason": "large_trade_market",
    }
    extras.update(overrides)
    return extras


def _row(**overrides: Any) -> dict[str, Any]:
    extras = overrides.pop("extras", _extras())
    row: dict[str, Any] = {
        "id": 11,
        "ts": "2026-11-05T08:00:00Z",
        "strategy_id": "tech_boll_fade_v1",
        "action": "buy",
        "chain_id": "binance",
        "token_address": "BNBUSDT",
        "size_pct": 0.05,
        "reason": "entry-zone",
        "risk_passed": 1,
        "mode": "live_stub",
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "raw_json": json.dumps(
            {
                "pair_address": "BNB/USDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "extras": extras,
            }
        ),
    }
    row.update(overrides)
    return row


def _form(signed: str) -> dict[str, list[str]]:
    return parse_qs(signed, keep_blank_values=True)


class ResidualS6Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.client = _Client()
        self.adapter = BinanceSpotAdapter(
            client=self.client,
            kill_switch_path=self.root / "LIVE_TRADING_DISABLED",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            auto_flatten_on_oco_fail=True,
            account_snapshot={
                "accountType": "SPOT",
                "canTrade": True,
                "permissions": ["SPOT"],
                "restrictions": {
                    "enableSpotAndMarginTrading": True,
                    "enableReading": True,
                    "ipRestrict": True,
                },
                "balances": [{"asset": "USDT", "free": "1000", "locked": "0"}],
            },
            production_book="rest_api_key",
            rest_expected={"account_type": "SPOT", "usdt_free": "1000"},
        )

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _gateway(self) -> ExecutionGateway:
        return ExecutionGateway(
            decision_db=self.root / "decision.db",
            ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
            paper_enabled=False,
            pm_enabled=False,
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            binance_spot=self.adapter,
        )

    def test_dry_run_submits_nothing(self) -> None:
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                result = gw.process_one(_row(mode="dry_run"))
                exits = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "90"}
                )
        self.assertTrue(result["exec_result"]["dry_run"])
        self.assertFalse(result["exec_result"]["submitted"])
        self.assertEqual(exits, [])
        self.assertEqual(self.client.calls, [])

    def test_kill_blocks_buy_and_inbound_sell_not_the_ledger_exit(self) -> None:
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("off\n", encoding="utf-8")
        env = _env(self.root, enabled="1")
        sell = _row(
            id=12,
            action="sell",
            mode="live_stub",
            extras={
                "binance_symbol": "BNBUSDT",
                "market": "spot",
                "order_type": "MARKET",
                "quantity": "1",
            },
        )
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                bought = gw.process_one(_row())
                inbound = gw.process_one(sell)
        self.assertEqual(bought["executed"], 0)
        self.assertTrue(str(bought["error"]).startswith("kill_switch:"))
        self.assertEqual(inbound["error"], "inbound_sell_not_protective")
        self.assertFalse(str(inbound["error"]).startswith("kill_switch:"))
        self.assertEqual(self.client.calls, [])
        position = {
            "id": 1,
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "open",
            "quantity": "1.239",
            "stop_price": "99",
            "take_profit_price": "110",
            "protection": "loop_fallback",
        }
        with mock.patch.dict(os.environ, env):
            sold = self.adapter.exit_held_position(position, "98")
        self.assertTrue(sold["submitted"], sold)
        self.assertEqual(_form(self.client.calls[-1][1])["side"], ["SELL"])

    def test_block_sell_flag_halts_the_protective_exit(self) -> None:
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("off\n", encoding="utf-8")
        env = _env(self.root, enabled="1", block_sell="1")
        position = {
            "id": 1,
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "open",
            "quantity": "1.239",
            "stop_price": "99",
            "take_profit_price": "110",
            "protection": "loop_fallback",
        }
        with mock.patch.dict(os.environ, env):
            blocked = self.adapter.exit_held_position(position, "98")
        self.assertFalse(blocked["submitted"])
        self.assertTrue(str(blocked["error"]).startswith("kill_switch_block_sell:"))
        self.assertEqual(self.client.calls, [])

    def test_oco_request_matches_the_spot_order_list_endpoint(self) -> None:
        spec, error = build_spot_oco(
            symbol="BNBUSDT",
            quantity="1.239",
            stop_price="99",
            take_profit_price="110",
            decision_id=11,
            filters=_FILTERS,
            mark="100",
            timestamp_ms=1_700_000_000_000,
        )
        self.assertIsNone(error)
        assert spec is not None
        form = dict(spec["pairs"])
        self.assertEqual(form["side"], "SELL")
        self.assertEqual(form["quantity"], "1.23")
        self.assertEqual(form["aboveType"], "LIMIT_MAKER")
        self.assertEqual(form["abovePrice"], "110")
        self.assertEqual(form["belowType"], "STOP_LOSS_LIMIT")
        self.assertEqual(form["belowStopPrice"], "99")
        self.assertEqual(form["belowPrice"], "98.99")
        self.assertEqual(form["belowTimeInForce"], "GTC")
        self.assertEqual(form["listClientOrderId"], "bnbfade-ol-11")
        self.assertEqual(form["aboveClientOrderId"], "bnbfade-tp-11")
        self.assertEqual(form["belowClientOrderId"], "bnbfade-sl-11")
        self.assertEqual(form["newOrderRespType"], "FULL")
        self.assertEqual(spec["residual_qty"], "0.009")
        seen: dict[str, Any] = {}

        def opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["body"] = request.data.decode("utf-8") if request.data else ""
            return 200, json.dumps(_working_oco()).encode("utf-8")

        with mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        ):
            http = BinanceSpotHttpClient(base_url="https://api.binance.test", opener=opener)
            http.new_oco(seen.get("body") or "symbol=BNBUSDT", api_key="test-key")
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(str(seen["url"]).endswith(OCO_PATH), seen["url"])
        self.assertNotIn("api.binance.com", str(seen["url"]))

    def test_fill_places_oco_and_a_later_tick_does_not_market_sell(self) -> None:
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                opened = gw.process_one(_row())
                self.assertEqual(opened["executed"], 1, opened)
                resting = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "100"}
                )
                row = gw.ledger.binance_spot_position_by_decision(11)
                again = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "50"}
                )
        self.assertEqual(resting[0]["protection"], "exchange_oco", resting[0])
        self.assertEqual(resting[0]["position_status"], "oco_working")
        self.assertFalse(resting[0]["submitted"] and resting[0].get("order", {}).get("type") == "MARKET")
        assert row is not None
        self.assertEqual(row["status"], "oco_working")
        self.assertEqual(row["protection"], "exchange_oco")
        self.assertEqual(row["quantity"], "1.23")
        self.assertEqual(row["residual_qty"], "0.009")
        self.assertEqual(row["oco_order_list_id"], "42")
        self.assertEqual(row["oco_above_order_id"], "11")
        self.assertEqual(row["oco_below_order_id"], "10")
        oco = _form(next(call[1] for call in self.client.calls if call[0] == "new_oco"))
        self.assertEqual(oco["aboveType"], ["LIMIT_MAKER"])
        self.assertEqual(oco["abovePrice"], ["110"])
        self.assertEqual(oco["belowType"], ["STOP_LOSS_LIMIT"])
        self.assertEqual(oco["belowStopPrice"], ["99"])
        self.assertEqual(oco["belowPrice"], ["98.99"])
        self.assertEqual(oco["quantity"], ["1.23"])
        self.assertEqual(oco["side"], ["SELL"])
        self.assertNotIn("test-secret", json.dumps(oco))
        self.assertEqual(again[0]["position_status"], "oco_working")
        self.assertFalse(any(call[0] == "new_order" and "side=SELL" in call[1] for call in self.client.calls))
        self.assertEqual(sum(1 for call in self.client.calls if call[0] == "new_oco"), 1)

    def test_timeout_requeries_and_adopts_the_list(self) -> None:
        self.client.oco_mode = "timeout"
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                gw.process_one(_row())
                adopted = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "100"}
                )
                later = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "98"}
                )
                row = gw.ledger.binance_spot_position_by_decision(11)
        self.assertEqual(adopted[0]["position_status"], "oco_working", adopted[0])
        self.assertEqual(adopted[0]["protection"], "exchange_oco")
        assert row is not None
        self.assertEqual(row["status"], "oco_working")
        self.assertEqual(later[0]["position_status"], "oco_working")
        self.assertEqual(sum(1 for call in self.client.calls if call[0] == "new_oco"), 1)
        listed = next(call for call in self.client.calls if call[0] == "get_order_list")
        self.assertIn("origClientOrderId=bnbfade-ol-11", listed[1])
        self.assertFalse(any(call[0] == "new_order" and "side=SELL" in call[1] for call in self.client.calls))

    def test_oco_rejection_is_a_loud_loop_fallback(self) -> None:
        self.client.oco_mode = "reject"
        self.client.list_payload = BinanceRequestError(400)
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                opened = gw.process_one(_row())
                row = gw.ledger.binance_spot_position_by_decision(11)
        self.assertEqual(opened["executed"], 0)
        assert row is not None
        self.assertEqual(row["status"], "closed_dust")
        self.assertEqual(row["protection"], "dust_abandon")
        self.assertTrue(any(call[0] == "new_order" and "side=SELL" in call[1] for call in self.client.calls))
        self.assertEqual(opened["exec_result"]["protection_state"], "dust_residual")

    def test_exchange_fill_and_expiry_are_reconciled(self) -> None:
        env = _env(self.root, enabled="1")
        filled = {
            "orderListId": 42,
            "listOrderStatus": "ALL_DONE",
            "listStatusType": "ALL_DONE",
            "orders": [
                {"symbol": "BNBUSDT", "orderId": 11, "clientOrderId": "bnbfade-tp-11"},
                {"symbol": "BNBUSDT", "orderId": 10, "clientOrderId": "bnbfade-sl-11"},
            ],
        }
        self.client.legs = {
            "11": {
                "orderId": 11,
                "type": "LIMIT_MAKER",
                "status": "FILLED",
                "executedQty": "1.23",
                "origQty": "1.23",
                "side": "SELL",
            },
            "10": {
                "orderId": 10,
                "type": "STOP_LOSS_LIMIT",
                "status": "CANCELED",
                "executedQty": "0",
                "origQty": "1.23",
                "side": "SELL",
            },
        }
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                gw.process_one(_row())
                tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "100"}
                )
                self.client.list_payload = filled
                done = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "110"}
                )
                row = gw.ledger.binance_spot_position_by_decision(11)
        self.assertEqual(done[0]["position_status"], "oco_filled", done[0])
        assert row is not None
        self.assertEqual(row["exit_reason"], "take_profit")
        self.assertEqual(row["protection"], "exchange_oco")
        self.assertFalse(any(call[0] == "new_order" and "side=SELL" in call[1] for call in self.client.calls))

        expired = {
            "orderListId": 43,
            "listOrderStatus": "ALL_DONE",
            "listStatusType": "ALL_DONE",
            "symbol": "BNBUSDT",
            "orderReports": [
                {
                    "orderId": 12,
                    "type": "LIMIT_MAKER",
                    "status": "EXPIRED",
                    "executedQty": "0",
                    "origQty": "1.23",
                },
                {
                    "orderId": 13,
                    "type": "STOP_LOSS_LIMIT",
                    "status": "EXPIRED",
                    "executedQty": "0",
                    "origQty": "1.23",
                },
            ],
        }
        self.client.calls.clear()
        self.client.list_payload = expired
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                gw.ledger.update_binance_spot_position(
                    int(row["id"]),
                    {
                        "status": "oco_working",
                        "protection": "exchange_oco",
                        "oco_order_list_id": "43",
                        "quantity": "1.23",
                    },
                )
                # The previous gateway closed its ledger. Re-open via a new one on the same file.
        ledger_path = self.root / "execution_ledger_live_bnb_boll_fade.db"
        with mock.patch.dict(os.environ, env):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=ledger_path,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                binance_spot=self.adapter,
            ) as gw:
                # The filled row is oco_filled and is not scanned. Seed a fresh open list.
                gw.ledger.update_binance_spot_position(
                    int(row["id"]),
                    {
                        "status": "oco_working",
                        "protection": "exchange_oco",
                        "oco_order_list_id": "43",
                        "exit_reason": None,
                        "quantity": "1.23",
                    },
                )
                woke = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "100"}
                )
                woke_row = gw.ledger.binance_spot_position_by_decision(11)
        assert woke_row is not None
        self.assertEqual(woke_row["status"], "closed_dust", woke)
        self.assertEqual(woke_row["protection"], "dust_abandon")
        self.assertTrue(any(call[0] == "new_order" and "side=SELL" in call[1] for call in self.client.calls))

    def test_entry_floor_fee_and_dust_archive(self) -> None:
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, 20)
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                dry = gw.process_one(
                    _row(mode="dry_run", id=3, extras=_extras(quote_order_qty="10"))
                )
                live = gw.process_one(
                    _row(id=4, extras=_extras(quote_order_qty="19.99"))
                )
        self.assertEqual(dry["exec_result"]["error"], "entry_notional_below_min")
        self.assertTrue(dry["exec_result"]["dry_run"])
        self.assertEqual(live["exec_result"]["error"], "entry_notional_below_min")
        self.assertEqual(self.client.calls, [])

        tiny = dict(_INFO)
        tiny["filters"] = [
            {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "100", "stepSize": "0.01"},
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
        ]
        self.client.exchange_info = lambda symbol: tiny  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                refused = gw.process_one(
                    _row(id=5, extras=_extras(quote_order_qty="20", price_usd=100))
                )
        self.assertEqual(refused["exec_result"]["error"], "entry_not_viable:below_min_qty")
        self.assertFalse(any(call[0] == "new_order" for call in self.client.calls))

        self.client = _Client()
        self.adapter.client = self.client
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                opened = gw.process_one(_row(id=6))
                buy = _form(next(call[1] for call in self.client.calls if call[0] == "new_order"))
                self.assertEqual(buy["quoteOrderQty"], ["25"])
                gw.ledger.update_binance_spot_position(
                    int(gw.ledger.binance_spot_position_by_decision(6)["id"]),  # type: ignore[index]
                    {"quantity": "0.004", "status": "open", "protection": "loop_fallback"},
                )
                dusted = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "100"}
                )
                again = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "50"}
                )
                dust_row = gw.ledger.binance_spot_position_by_decision(6)
        self.assertEqual(opened["executed"], 1, opened)
        self.assertEqual(dusted[0]["error"], "dust_untradable")
        self.assertEqual(dusted[0]["position_status"], "closed_dust")
        self.assertEqual(again, [])
        assert dust_row is not None
        self.assertEqual(dust_row["status"], "closed_dust")
        self.assertEqual(dust_row["exit_reason"], "dust_abandon")
        self.assertEqual(dust_row["protection"], "dust_abandon")
        self.assertEqual(dust_row["residual_qty"], "0.004")
        self.assertEqual(dust_row["residual_quote"], "0.4")
        self.assertNotEqual(dust_row["status"], "exit_submitted")
        self.assertFalse(any(call[0] == "new_order" and "side=SELL" in call[1] for call in self.client.calls))

    def test_partial_and_canceled_lists_are_not_exchange_coverage(self) -> None:
        partial = classify_oco(
            {
                "listOrderStatus": "ALL_DONE",
                "orderListId": 7,
                "orderReports": [
                    {
                        "type": "LIMIT_MAKER",
                        "status": "FILLED",
                        "executedQty": "1.00",
                        "orderId": 1,
                    },
                    {
                        "type": "STOP_LOSS_LIMIT",
                        "status": "CANCELED",
                        "executedQty": "0",
                        "orderId": 2,
                    },
                ],
            },
            held_qty="1.23",
        )
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["exit_reason"], "take_profit")
        self.assertEqual(partial["remainder_qty"], "0.23")
        canceled = classify_oco(
            {
                "listOrderStatus": "ALL_DONE",
                "orderReports": [
                    {"type": "LIMIT_MAKER", "status": "CANCELED", "executedQty": "0"},
                    {"type": "STOP_LOSS_LIMIT", "status": "CANCELED", "executedQty": "0"},
                ],
            },
            held_qty="1.23",
        )
        self.assertEqual(canceled["state"], "unfilled")

    def test_race_peer_is_not_routed_here(self) -> None:
        self.assertEqual(
            tick_live_bnb_exits(object(), frozenset({"race_tech_boll_fade"}), {"BNBUSDT": "1"}),  # type: ignore[arg-type]
            [],
        )


if __name__ == "__main__":
    unittest.main()
