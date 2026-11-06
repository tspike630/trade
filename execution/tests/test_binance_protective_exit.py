"""Held-position protective exit for live_bnb_boll_fade. No live HTTP.

    python -m unittest execution.tests.test_binance_protective_exit
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
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
    floor_base_qty,
    protective_exit_reason,
    size_exit_quantity,
)
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.run import tick_live_bnb_exits  # noqa: E402

_INFO = {
    "symbol": "BNBUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "isSpotTradingAllowed": True,
    "filters": [
        {
            "filterType": "MARKET_LOT_SIZE",
            "minQty": "0.01",
            "maxQty": "900000",
            "stepSize": "0.01",
        },
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.001",
            "maxQty": "900000",
            "stepSize": "0.001",
        },
        {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.01",
            "maxPrice": "1000000",
            "tickSize": "0.01",
        },
    ],
}


def _working_oco() -> dict[str, Any]:
    return {
        "orderListId": 42,
        "listOrderStatus": "EXECUTING",
        "listStatusType": "EXEC_STARTED",
        "orders": [
            {"orderId": 11},
            {"orderId": 10},
        ],
        "orderReports": [
            {"orderId": 11, "type": "LIMIT_MAKER", "status": "NEW", "executedQty": "0"},
            {"orderId": 10, "type": "STOP_LOSS_LIMIT", "status": "NEW", "executedQty": "0"},
        ],
    }


class _Client:
    def __init__(self, *, info: dict[str, Any] | None = None, fail_sells: int = 0) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.info = _INFO if info is None else info
        self.fail_sells = fail_sells
        self._failed = 0

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return self.info

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        if "side=SELL" in signed_query and self._failed < self.fail_sells:
            self._failed += 1
            raise BinanceRequestError(500)
        if "side=BUY" in signed_query:
            return {
                "orderId": "99",
                "status": "FILLED",
                "executedQty": "1.239",
            }
        return {"orderId": "77", "status": "FILLED"}

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        return _working_oco()

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order_list", signed_query, api_key))
        return _working_oco()

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": "102"}


def _env(root: Path, *, enabled: str) -> dict[str, str]:
    return {
        "BINANCE_SPOT_LIVE_ENABLED": enabled,
        "LIVE_KILL_SWITCH_PATH": str(root / "LIVE_TRADING_DISABLED"),
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "LIVE_EXCHANGE_API_KEY": "",
        "LIVE_EXCHANGE_API_SECRET": "",
        "BINANCE_SPOT_API_BASE": "https://api.binance.test",
    }


def _zone_extras(**overrides: Any) -> dict[str, Any]:
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


def _decision_row(**overrides: Any) -> dict[str, Any]:
    extras = overrides.pop("extras", None) or _zone_extras()
    row = {
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
    if "extras" in overrides:
        raise AssertionError("pass extras before other overrides")
    return row


def _signed_form(signed: str) -> dict[str, list[str]]:
    return parse_qs(signed, keep_blank_values=True)


def _sell_forms(client: _Client) -> list[dict[str, list[str]]]:
    forms = []
    for call in client.calls:
        if call[0] != "new_order":
            continue
        form = _signed_form(call[1])
        if form.get("side") == ["SELL"]:
            forms.append(form)
    return forms


class ProtectiveExitTests(unittest.TestCase):
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

    def test_dry_run_submits_nothing_and_does_not_arm(self) -> None:
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                result = gw.process_one(_decision_row(mode="dry_run"))
                exits = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "90"},
                )
                armed = gw.ledger.list_binance_spot_positions_for_exit()
        self.assertEqual(result["receipt"]["status"], "dry_run_acked")
        self.assertEqual(result["executed"], 0)
        self.assertFalse(result["exec_result"]["submitted"])
        self.assertEqual(
            result["exec_result"]["exit_plan"]["protective_order"],
            "not_supported_not_sent",
        )
        self.assertNotIn("held_position", result["receipt"])
        self.assertEqual(armed, [])
        self.assertEqual(exits, [])
        self.assertEqual(self.client.calls, [])

    def test_stop_exit_is_reachable_and_sells_the_floored_position(self) -> None:
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                opened = gw.process_one(_decision_row())
                held = opened["receipt"]["held_position"]
                self.assertEqual(held["status"], "oco_working")
                self.assertEqual(held["protection"], "exchange_oco")
                self.assertEqual(held["stop_price"], "99")
                self.assertEqual(held["take_profit_price"], "110")
                self.assertEqual(opened["executed"], 1)
                buy_signed = next(
                    call[1]
                    for call in self.client.calls
                    if call[0] == "new_order" and "side=BUY" in call[1]
                )
                buy = _signed_form(buy_signed)
                self.assertEqual(buy["side"], ["BUY"])
                self.assertNotIn("stopPrice", buy)
                self.assertNotIn("STOP_LOSS", buy_signed)
                exits = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "98"},
                )
                self.assertEqual(exits[0]["position_status"], "oco_working")
                self.assertFalse(exits[0]["submitted"])
                self.assertEqual(_sell_forms(self.client), [])
                signed = next(call[1] for call in self.client.calls if call[0] == "new_oco")
                body, signature = signed.rsplit("&signature=", 1)
                expected = hmac.new(
                    b"test-secret", body.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                self.assertEqual(signature, expected)
                self.assertNotIn("test-secret", signed)
                self.assertNotIn("test-key", signed)
                oco = _signed_form(signed)
                self.assertEqual(oco["aboveType"], ["LIMIT_MAKER"])
                self.assertEqual(oco["belowType"], ["STOP_LOSS_LIMIT"])
                self.assertEqual(oco["side"], ["SELL"])
                again = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "50"},
                )
                self.assertEqual(again[0]["position_status"], "oco_working")
                self.assertEqual(_sell_forms(self.client), [])
                row = gw.ledger.binance_spot_position_by_decision(11)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "oco_working")
        self.assertEqual(row["protection"], "exchange_oco")
        self.assertEqual(row["oco_order_list_id"], "42")
        self.assertEqual(row["oco_above_order_id"], "11")
        self.assertEqual(row["oco_below_order_id"], "10")

    def test_take_profit_uses_the_decision_mid_not_a_percent(self) -> None:
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                gw.process_one(_decision_row(id=12))
                # 109 is still below the absolute mid (110) and below a 10% pop (112.2).
                quiet = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "109"},
                )
                self.assertFalse(quiet[0]["submitted"])
                self.assertEqual(quiet[0]["position_status"], "oco_working")
                hit = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "110"},
                )
        self.assertEqual(hit[0]["position_status"], "oco_working")
        self.assertFalse(hit[0]["submitted"])
        self.assertEqual(_sell_forms(self.client), [])

    def test_kill_switch_still_allows_the_ledger_protective_sell(self) -> None:
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("keep live off\n", encoding="utf-8")
        env = _env(self.root, enabled="1")
        position = {
            "id": 1,
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "open",
            "quantity": "1.239",
            "stop_price": "99",
            "take_profit_price": "110",
        }
        with mock.patch.dict(os.environ, env):
            sold = self.adapter.exit_held_position(position, "98")
        self.assertEqual(sold["reason"], "stop_loss")
        self.assertTrue(sold["submitted"], sold)
        self.assertEqual(sold["quantity"], "1.23")
        self.assertEqual(_sell_forms(self.client)[0]["side"], ["SELL"])
        self.assertNotIn("test-secret", json.dumps(sold))

    def test_live_flag_off_blocks_the_protective_sell(self) -> None:
        env = _env(self.root, enabled="0")
        position = {
            "id": 1,
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "open",
            "quantity": "1.239",
            "stop_price": "99",
            "take_profit_price": "110",
        }
        with mock.patch.dict(os.environ, env):
            blocked = self.adapter.exit_held_position(position, "110")
        self.assertEqual(blocked["reason"], "take_profit")
        self.assertFalse(blocked["submitted"])
        self.assertEqual(blocked["error"], "binance_spot_live_disabled")
        self.assertEqual(self.client.calls, [])

    def test_unconfirmed_fill_and_bad_precision_do_not_sell(self) -> None:
        self.client = _Client()
        self.client.new_order = _buy_without_qty(self.client)  # type: ignore[method-assign]
        self.adapter.client = self.client
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                opened = gw.process_one(_decision_row())
                self.assertEqual(opened["receipt"]["held_position"]["status"], "qty_unconfirmed")
                self.assertIsNone(opened["receipt"]["held_position"]["quantity"])
                pending = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "90"},
                )
                self.assertEqual(pending[0]["error"], "exit_qty_unconfirmed")
                self.assertFalse(pending[0]["submitted"])
                self.assertEqual(pending[0]["position_status"], "qty_unconfirmed")
                self.assertEqual(_sell_forms(self.client), [])

                dust = {
                    "id": 2,
                    "decision_log_id": 21,
                    "symbol": "BNBUSDT",
                    "status": "open",
                    "quantity": "0.004",
                    "stop_price": "99",
                    "take_profit_price": "110",
                    "protection": "loop_fallback",
                }
                gw.ledger.arm_binance_spot_position(
                    {
                        "decision_log_id": 21,
                        "symbol": "BNBUSDT",
                        "status": "open",
                        "quantity": "0.004",
                        "stop_price": "99",
                        "take_profit_price": "110",
                        "entry_price": "102",
                        "entry_order_id": "5",
                        "protection": "held_position_exit",
                    }
                )
                # Direct float qty is not a Binance decimal and must not be rounded via binary.
                refused = self.adapter.exit_held_position(
                    {**dust, "quantity": 1.239},
                    "98",
                )
                self.assertEqual(refused["error"], "exit_qty_unconfirmed")
                self.assertFalse(refused["submitted"])
                sized = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "100"},
                )
        dust_exit = next(item for item in sized if item["decision_log_id"] == 21)
        self.assertEqual(dust_exit["error"], "dust_untradable")
        self.assertFalse(dust_exit["submitted"])
        self.assertEqual(dust_exit["position_status"], "closed_dust")
        self.assertEqual(dust_exit["protection"], "dust_abandon")
        self.assertEqual(_sell_forms(self.client), [])
        self.assertEqual(floor_base_qty("1.239", "0.01"), "1.23")
        self.assertNotEqual(floor_base_qty("1.239", "0.01"), "1.24")

    def test_missing_fill_qty_is_read_from_the_entry_order_then_sold(self) -> None:
        self.client = _Client()

        def new_order(signed_query: str, *, api_key: str) -> dict[str, Any]:
            self.client.calls.append(("new_order", signed_query, api_key))
            if "side=SELL" in signed_query:
                return {"orderId": "77", "status": "FILLED"}
            return {"orderId": "99", "status": "NEW"}

        def get_order(signed_query: str, *, api_key: str) -> dict[str, Any]:
            self.client.calls.append(("get_order", signed_query, api_key))
            self.assertNotIn("test-secret", signed_query)
            return {"orderId": "99", "status": "FILLED", "executedQty": "1.239"}

        self.client.new_order = new_order  # type: ignore[method-assign]
        self.client.get_order = get_order  # type: ignore[method-assign]
        self.adapter.client = self.client
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                opened = gw.process_one(_decision_row())
                self.assertEqual(opened["executed"], 0)
                self.assertEqual(opened["receipt"]["held_position"]["status"], "qty_unconfirmed")
                exits = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "98"},
                )
                row = gw.ledger.binance_spot_position_by_decision(11)
        self.assertEqual(exits[0]["position_status"], "oco_failed")
        self.assertEqual(exits[0]["error"], "oco_price_restriction")
        self.assertFalse(exits[0]["submitted"])
        self.assertFalse(any(call[0] == "new_order" and "side=SELL" in call[1] for call in self.client.calls))
        assert row is not None
        self.assertEqual(row["status"], "oco_failed")
        self.assertIn("orderId=99", next(call[1] for call in self.client.calls if call[0] == "get_order"))
        self.assertEqual(_sell_forms(self.client), [])

    def test_http_failure_keeps_the_position_open_and_retries_one_client_id(self) -> None:
        self.client = _Client(fail_sells=1)
        self.adapter.client = self.client
        env = _env(self.root, enabled="1")
        position = {
            "id": 1,
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "open",
            "quantity": "1.239",
            "stop_price": "99",
            "take_profit_price": "110",
        }
        with mock.patch.dict(os.environ, env):
            failed = self.adapter.exit_held_position(position, "98")
            retried = self.adapter.exit_held_position(position, "98")
        self.assertFalse(failed["submitted"])
        self.assertEqual(failed["error"], "binance_http_error:500")
        self.assertTrue(retried["submitted"], retried)
        sells = _sell_forms(self.client)
        self.assertEqual(len(sells), 2)
        self.assertEqual(sells[0]["newClientOrderId"], ["bnbfade-x-11"])
        self.assertEqual(sells[1]["newClientOrderId"], sells[0]["newClientOrderId"])
        self.assertEqual(sells[0]["quantity"], ["1.23"])

    def test_missing_lot_filter_and_min_notional_do_not_send(self) -> None:
        naked = {
            "symbol": "BNBUSDT",
            "status": "TRADING",
            "quoteAsset": "USDT",
        }
        qty, error = size_exit_quantity(naked, "BNBUSDT", "1.239", "98")
        self.assertIsNone(qty)
        self.assertEqual(error, "missing_lot_filter")
        small, small_error = size_exit_quantity(_INFO, "BNBUSDT", "0.02", "98")
        self.assertIsNone(small)
        self.assertEqual(small_error, "exit_below_min_notional")
        self.client.info = naked
        env = _env(self.root, enabled="1")
        position = {
            "id": 1,
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "open",
            "quantity": "1.239",
            "stop_price": "99",
            "take_profit_price": "110",
        }
        with mock.patch.dict(os.environ, env):
            refused = self.adapter.exit_held_position(position, "98")
        self.assertEqual(refused["error"], "missing_lot_filter")
        self.assertFalse(refused["submitted"])
        self.assertEqual([call[0] for call in self.client.calls], ["exchange_info"])

    def test_race_peer_tick_does_not_exit(self) -> None:
        class _Boom:
            def tick_binance_protective_exits(self, marks: dict[str, Any] | None = None) -> list[dict[str, Any]]:
                raise AssertionError(f"race tick must not exit {marks}")

        self.assertEqual(
            tick_live_bnb_exits(_Boom(), frozenset({"race_tech_boll_fade"}), {"BNBUSDT": "1"}),  # type: ignore[arg-type]
            [],
        )
        self.assertEqual(tick_live_bnb_exits(_Boom(), None), [])  # type: ignore[arg-type]

    def test_market_step_zero_uses_lot_size_and_does_not_round_up(self) -> None:
        info = {
            "symbol": "BNBUSDT",
            "filters": [
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "minQty": "0.00000000",
                    "maxQty": "1000",
                    "stepSize": "0.00000000",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.01",
                    "maxQty": "9000",
                    "stepSize": "0.01",
                },
                {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": False},
            ],
        }
        qty, error = size_exit_quantity(info, "BNBUSDT", "1.239", "1")
        self.assertIsNone(error)
        self.assertEqual(qty, "1.23")


def _buy_without_qty(client: _Client):
    def new_order(signed_query: str, *, api_key: str) -> dict[str, Any]:
        client.calls.append(("new_order", signed_query, api_key))
        return {"orderId": "99", "status": "NEW"}

    return new_order


_DASH_INFO = {
    "symbol": "DASHUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "isSpotTradingAllowed": True,
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "900000", "stepSize": "0.001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "900000", "stepSize": "0.001"},
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
        {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
    ],
}


class _DashBook:
    """Ticker is the live mark. Orders are recorded and not sent to Binance."""

    def __init__(self, *, mark: str, oco_error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.mark = mark
        self.oco_error = oco_error

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _DASH_INFO

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": self.mark}

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        return {
            "orderId": "2953042815",
            "status": "FILLED",
            "executedQty": "1.082",
            "cummulativeQuoteQty": "51.957640",
            "symbol": "DASHUSDT",
        }

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        if self.oco_error is not None:
            raise self.oco_error
        raise AssertionError("OCO must not be posted for this case")


class DashUnprotectedGeometryTests(unittest.TestCase):
    """DASH id 12: signal still above the stop, live mark and fill are not."""

    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _adapter(self, client: Any) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "absent-kill",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot={
                "accountType": "SPOT",
                "canTrade": True,
                "permissions": ["SPOT"],
                "restrictions": {
                    "enableSpotAndMarginTrading": True,
                    "enableReading": True,
                    "ipRestrict": True,
                },
                "balances": [{"asset": "USDT", "free": "1000.00", "locked": "0"}],
            },
            production_book="rest_api_key",
            rest_expected={"account_type": "SPOT", "usdt_free": "1000"},
        )

    def _dash_buy(self) -> dict[str, Any]:
        return {
            "decision_log_id": 148592,
            "strategy_id": "tech_boll_fade_v1",
            "action": "buy",
            "chain_id": "binance",
            "token_address": "DASHUSDT",
            "pair_address": "DASH/USDT",
            "size_pct": 0.05,
            "mode": "live_stub",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "extras": {
                "binance_symbol": "DASHUSDT",
                "order_type": "MARKET",
                "quote_order_qty": "51.95",
                "price_usd": "48.10",
                "stop_price": "48.02605",
                "take_profit_price": "48.2895",
                "entry_reason": "large_trade_market",
            },
        }

    def test_live_mark_through_the_stop_refuses_the_buy(self) -> None:
        client = _DashBook(mark="47.79")
        adapter = self._adapter(client)
        env = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
            "LIVE_EXCHANGE_API_KEY": "",
            "LIVE_EXCHANGE_API_SECRET": "",
        }
        with mock.patch.dict(os.environ, env):
            refused = adapter.execute(self._dash_buy())
        self.assertEqual(refused["error"], "oco_price_restriction")
        self.assertEqual(refused["executed"], 0)
        self.assertFalse(any(call[0] == "new_order" for call in client.calls))
        self.assertFalse(any(call[0] == "new_oco" for call in client.calls))

    def test_requery_gap_does_not_mask_a_failed_oco_or_sell(self) -> None:
        class _NoQuery:
            calls: list[tuple[Any, ...]] = []

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _DASH_INFO

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_oco", signed_query))
                raise AssertionError("dead geometry must not post another OCO")

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_order", signed_query))
                raise AssertionError("must not market-sell the naked fill")

        client = _NoQuery()
        adapter = self._adapter(client)
        row = {
            "id": 12,
            "decision_log_id": 148592,
            "symbol": "DASHUSDT",
            "status": "oco_failed",
            "protection": "oco_pending",
            "quantity": "1.082",
            "stop_price": "48.02605",
            "take_profit_price": "48.2895",
            "entry_order_id": "2953042815",
            "last_error": "oco_rejected:binance_http_error:400",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        saved: dict[str, Any] = {}

        def persist(fields: dict[str, Any]) -> dict[str, Any]:
            saved.update(fields)
            return {**row, **saved}

        env = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
            "LIVE_EXCHANGE_API_KEY": "",
            "LIVE_EXCHANGE_API_SECRET": "",
        }
        with mock.patch.dict(os.environ, env):
            outcome = adapter.maintain_held_position(row, "47.79", persist=persist)
        self.assertNotEqual(saved.get("last_error"), "oco_requery_unsupported")
        self.assertIn("oco_rejected:binance_http_error:400", str(saved.get("last_error")))
        self.assertEqual(outcome.get("book"), "oco_failed")
        self.assertEqual(client.calls, [])
        self.assertNotIn("SELL", json.dumps(outcome))

    def test_fill_through_the_stop_does_not_post_oco(self) -> None:
        client = _DashBook(mark="48.10")
        adapter = self._adapter(client)
        env = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
            "LIVE_EXCHANGE_API_KEY": "",
            "LIVE_EXCHANGE_API_SECRET": "",
        }
        with mock.patch.dict(os.environ, env):
            filled = adapter.execute(self._dash_buy())
        self.assertEqual(filled["error"], "oco_unprotectable:oco_price_restriction")
        self.assertEqual(filled["arm_position"]["status"], "oco_failed")
        self.assertEqual(filled["executed"], 0)
        self.assertEqual(sum(1 for call in client.calls if call[0] == "new_order"), 1)
        self.assertFalse(any(call[0] == "new_oco" for call in client.calls))
        adapter.buy_blocks = [filled["arm_position"]]
        with mock.patch.dict(os.environ, env):
            blocked = adapter.execute(
                {
                    **self._dash_buy(),
                    "decision_log_id": 148593,
                    "token_address": "ETHUSDT",
                    "extras": {
                        **self._dash_buy()["extras"],
                        "binance_symbol": "ETHUSDT",
                        "price_usd": "100",
                        "stop_price": "90",
                        "take_profit_price": "120",
                    },
                }
            )
        self.assertEqual(blocked["error"], "oco_failed_blocks_buy")

    def test_absent_list_code_is_not_a_working_oco_and_does_not_repost(self) -> None:
        class _Query:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                self.calls.append("exchange_info")
                return _DASH_INFO

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("get_order_list")
                raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("new_oco")
                raise AssertionError("price through the stop must not post")

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("new_order")
                raise AssertionError("must not market-sell")

        client = _Query()
        adapter = self._adapter(client)
        row = {
            "id": 12,
            "decision_log_id": 148592,
            "symbol": "DASHUSDT",
            "status": "oco_failed",
            "protection": "oco_pending",
            "quantity": "1.082",
            "stop_price": "48.02605",
            "take_profit_price": "48.2895",
            "entry_order_id": "2953042815",
            "oco_list_client_id": "bnbfade-ol-148592",
            "last_error": "oco_rejected:binance_http_error:400",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        saved: dict[str, Any] = {}

        def persist(fields: dict[str, Any]) -> dict[str, Any]:
            saved.update(fields)
            return {**row, **saved}

        env = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
        }
        with mock.patch.dict(os.environ, env):
            outcome = adapter.maintain_held_position(row, "47.79", persist=persist)
        self.assertEqual(outcome["book"], "oco_failed")
        self.assertIn("oco_rejected:binance_http_error:400", str(saved.get("last_error")))
        self.assertNotIn("new_oco", client.calls)
        self.assertNotIn("new_order", client.calls)
        self.assertIn("get_order_list", client.calls)

    def test_unknown_submit_retries_once_when_the_list_is_absent(self) -> None:
        class _Retry:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.placed = False

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _INFO

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("get_order_list")
                if not self.placed:
                    raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")
                return _working_oco()

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("new_oco")
                self.placed = True
                return _working_oco()

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise AssertionError("retry must not buy or market-sell")

        client = _Retry()
        adapter = self._adapter(client)
        row = {
            "id": 3,
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "oco_unknown",
            "protection": "oco_pending",
            "quantity": "1.23",
            "stop_price": "99",
            "take_profit_price": "110",
            "entry_order_id": "99",
            "oco_list_client_id": "bnbfade-ol-11",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        saved: dict[str, Any] = dict(row)

        def persist(fields: dict[str, Any]) -> dict[str, Any]:
            saved.update(fields)
            return dict(saved)

        env = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
        }
        with mock.patch.dict(os.environ, env):
            first = adapter.maintain_held_position(dict(saved), "102", persist=persist)
            second = adapter.maintain_held_position(dict(saved), "102", persist=persist)
        self.assertEqual(first["book"], "oco_working", first)
        self.assertEqual(second["book"], "oco_working", second)
        self.assertEqual(client.calls.count("new_oco"), 1)
        self.assertEqual(saved.get("status"), "oco_working")
        self.assertEqual(saved.get("oco_above_order_id"), "11")
        self.assertEqual(saved.get("oco_below_order_id"), "10")


if __name__ == "__main__":
    unittest.main()
