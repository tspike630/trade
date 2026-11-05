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

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        raise AssertionError(f"ticker must not be fetched when a mark was given ({symbol})")


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
                self.assertEqual(held["status"], "open")
                self.assertEqual(held["quantity"], "1.239")
                self.assertEqual(held["protection"], "held_position_exit")
                self.assertEqual(held["stop_price"], "99")
                self.assertEqual(held["take_profit_price"], "110")
                buy = _signed_form(self.client.calls[1][1])
                self.assertEqual(buy["side"], ["BUY"])
                self.assertNotIn("stopPrice", buy)
                self.assertNotIn("STOP_LOSS", self.client.calls[1][1])
                with mock.patch(
                    "execution.executors.binance_spot.protective_exit_reason",
                    wraps=protective_exit_reason,
                ) as spy:
                    # 98 is above a 5% stop from 102 (96.9) and at/under the plan stop.
                    exits = tick_live_bnb_exits(
                        gw,
                        frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                        {"BNBUSDT": "98"},
                    )
                self.assertGreaterEqual(spy.call_count, 1)
                self.assertEqual(exits[0]["reason"], "stop_loss")
                self.assertTrue(exits[0]["submitted"], exits[0])
                self.assertEqual(exits[0]["quantity"], "1.23")
                self.assertEqual(exits[0]["position_status"], "exit_submitted")
                sells = _sell_forms(self.client)
                self.assertEqual(len(sells), 1)
                self.assertEqual(sells[0]["type"], ["MARKET"])
                self.assertEqual(sells[0]["quantity"], ["1.23"])
                self.assertEqual(sells[0]["symbol"], ["BNBUSDT"])
                self.assertEqual(sells[0]["newClientOrderId"], ["bnbfade-x-11"])
                self.assertNotIn("quoteOrderQty", sells[0])
                signed = next(
                    call[1] for call in self.client.calls if call[0] == "new_order" and "side=SELL" in call[1]
                )
                body, signature = signed.rsplit("&signature=", 1)
                expected = hmac.new(
                    b"test-secret", body.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                self.assertEqual(signature, expected)
                self.assertNotIn("test-secret", signed)
                self.assertNotIn("test-key", signed)
                again = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "50"},
                )
                self.assertEqual(again, [])
                self.assertEqual(len(_sell_forms(self.client)), 1)
                row = gw.ledger.binance_spot_position_by_decision(11)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "exit_submitted")
        self.assertEqual(row["exit_reason"], "stop_loss")
        self.assertEqual(row["quantity"], "1.23")
        self.assertEqual(row["exit_order_id"], "77")

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
                self.assertEqual(quiet[0]["reason"], None)
                self.assertFalse(quiet[0]["submitted"])
                self.assertEqual(quiet[0]["position_status"], "open")
                hit = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "110"},
                )
        self.assertEqual(hit[0]["reason"], "take_profit")
        self.assertTrue(hit[0]["submitted"])
        self.assertEqual(_sell_forms(self.client)[-1]["quantity"], ["1.23"])

    def test_kill_switch_blocks_the_protective_sell(self) -> None:
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("keep live off\n", encoding="utf-8")
        env = _env(self.root, enabled="1")
        position = {
            "decision_log_id": 11,
            "symbol": "BNBUSDT",
            "status": "open",
            "quantity": "1.239",
            "stop_price": "99",
            "take_profit_price": "110",
        }
        def _boom_get(signed_query: str, *, api_key: str) -> dict[str, Any]:
            raise AssertionError(f"kill switch must not query orders {signed_query} {api_key}")

        self.client.get_order = _boom_get  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(
                self.adapter.refresh_filled_qty(
                    {**position, "status": "qty_unconfirmed", "entry_order_id": "99"}
                )
            )
            blocked = self.adapter.exit_held_position(position, "98")
        self.assertEqual(blocked["reason"], "stop_loss")
        self.assertFalse(blocked["submitted"])
        self.assertTrue(str(blocked["error"]).startswith("kill_switch:"))
        self.assertEqual(self.client.calls, [])
        self.assertNotIn("test-secret", json.dumps(blocked))

    def test_live_flag_off_blocks_the_protective_sell(self) -> None:
        env = _env(self.root, enabled="0")
        position = {
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
                    {"BNBUSDT": "98"},
                )
        dust_exit = next(item for item in sized if item["decision_log_id"] == 21)
        self.assertEqual(dust_exit["error"], "exit_qty_below_min")
        self.assertFalse(dust_exit["submitted"])
        self.assertEqual(dust_exit["position_status"], "open")
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
                self.assertEqual(opened["receipt"]["held_position"]["status"], "qty_unconfirmed")
                exits = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "98"},
                )
                row = gw.ledger.binance_spot_position_by_decision(11)
        self.assertEqual(exits[0]["reason"], "stop_loss")
        self.assertTrue(exits[0]["submitted"], exits[0])
        self.assertEqual(exits[0]["quantity"], "1.23")
        self.assertEqual(exits[0]["position_status"], "exit_submitted")
        assert row is not None
        self.assertEqual(row["status"], "exit_submitted")
        kinds = [call[0] for call in self.client.calls]
        self.assertEqual(kinds, ["exchange_info", "new_order", "get_order", "exchange_info", "new_order"])
        self.assertIn("orderId=99", self.client.calls[2][1])
        self.assertEqual(len(_sell_forms(self.client)), 1)

    def test_http_failure_keeps_the_position_open_and_retries_one_client_id(self) -> None:
        self.client = _Client(fail_sells=1)
        self.adapter.client = self.client
        env = _env(self.root, enabled="1")
        with mock.patch.dict(os.environ, env):
            with self._gateway() as gw:
                gw.process_one(_decision_row())
                failed = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "98"},
                )
                self.assertFalse(failed[0]["submitted"])
                self.assertEqual(failed[0]["error"], "binance_http_error:500")
                self.assertEqual(failed[0]["position_status"], "open")
                row = gw.ledger.binance_spot_position_by_decision(11)
                assert row is not None
                self.assertEqual(row["status"], "open")
                self.assertEqual(row["last_error"], "binance_http_error:500")
                retried = tick_live_bnb_exits(
                    gw,
                    frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                    {"BNBUSDT": "98"},
                )
                self.assertTrue(retried[0]["submitted"], retried[0])
                self.assertEqual(retried[0]["position_status"], "exit_submitted")
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


if __name__ == "__main__":
    unittest.main()
