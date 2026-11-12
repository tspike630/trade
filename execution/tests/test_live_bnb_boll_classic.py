"""Classic live arm: purpose gate, native OCO, per-purpose buy blocks. No live HTTP.

    python -m unittest execution.tests.test_live_bnb_boll_classic
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_mcp_broker import LiveMCPBroker  # noqa: E402
from execution.executors.binance_spot import (  # noqa: E402
    CLASSIC_EXIT_SHELL,
    BinanceRequestError,
    BinanceSpotAdapter,
)
from execution.executors.binance_spot_gate import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    REST_BOOK,
    buy_exposure_block,
)
from execution.executors.binance_spot_protect import (  # noqa: E402
    ENTRY_MIN_NOTIONAL_USDT,
    sellable_base_qty,
)
from execution.gateway import BINANCE_SPOT_EXECUTOR, ExecutionGateway  # noqa: E402
from execution.ledger import (  # noqa: E402
    ExecutionLedger,
    LEDGER_REGISTRY_ENV,
    initialize_spot_ledger_binding,
)
from execution.run import _peer_isolation_guard, tick_live_bnb_exits  # noqa: E402

_FILTERS = [
    {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "900000", "stepSize": "0.01"},
    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
]
_INFO = {
    "symbol": "BNBUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "isSpotTradingAllowed": True,
    "filters": _FILTERS,
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
_EXPECTED = {"account_type": "SPOT", "usdt_free": "1000"}


def _working() -> dict[str, Any]:
    return {
        "orderListId": 42,
        "listOrderStatus": "EXECUTING",
        "listStatusType": "EXEC_STARTED",
        "orderReports": [
            {"orderId": 11, "type": "LIMIT_MAKER", "status": "NEW", "executedQty": "0"},
            {
                "orderId": 10,
                "type": "STOP_LOSS_LIMIT",
                "status": "NEW",
                "stopPrice": "95",
                "executedQty": "0",
            },
        ],
    }


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _INFO

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        return {
            "orderId": "99",
            "status": "FILLED",
            "executedQty": "1.239",
            "symbol": "BNBUSDT",
        }

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        return _working()

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order_list", signed_query, api_key))
        return _working()

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": "102"}


class _Boom:
    def execute(self, sig: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"paper must not run for {sig.get('purpose')}")


def _env(root: Path) -> dict[str, str]:
    return {
        "BINANCE_SPOT_LIVE_ENABLED": "1",
        "LIVE_KILL_SWITCH_PATH": str(root / "absent-kill"),
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "LIVE_EXCHANGE_API_KEY": "",
        "LIVE_EXCHANGE_API_SECRET": "",
        "BINANCE_SPOT_ACCOUNT_UID": "",
        "BINANCE_SPOT_PRODUCTION_BOOK": "",
    }


def _classic_extras(**overrides: Any) -> dict[str, Any]:
    extras: dict[str, Any] = {
        "binance_symbol": "BNBUSDT",
        "order_type": "MARKET",
        "quote_order_qty": "25",
        "price_usd": "100",
        "stop_price": "95",
        "take_profit_price": "110",
        "tp_pct": 10,
        "sl_pct": 5,
        "max_hold_min": 60,
        "exit_shell": CLASSIC_EXIT_SHELL,
        "and_mode": "tech_fade&boll_lower",
    }
    extras.update(overrides)
    return extras


def _sig(**overrides: Any) -> dict[str, Any]:
    extras = overrides.pop("extras", None)
    if extras is None:
        extras = _classic_extras()
    base: dict[str, Any] = {
        "decision_log_id": 147901,
        "strategy_id": "tech_boll_fade_v1",
        "action": "buy",
        "chain_id": "binance",
        "token_address": "BNBUSDT",
        "pair_address": "BNB/USDT",
        "size_pct": 0.24,
        "mode": "dry_run",
        "purpose": PURPOSE_LIVE_BNB_BOLL_CLASSIC,
        "extras": extras,
    }
    base.update(overrides)
    return base


def _decision_row(**overrides: Any) -> dict[str, Any]:
    sig = _sig(**overrides)
    return {
        "id": int(sig["decision_log_id"]),
        "ts": "2026-11-06T08:00:00Z",
        "strategy_id": sig["strategy_id"],
        "action": sig["action"],
        "chain_id": sig["chain_id"],
        "token_address": sig["token_address"],
        "size_pct": sig["size_pct"],
        "reason": "classic",
        "risk_passed": 1,
        "mode": sig["mode"],
        "purpose": sig["purpose"],
        "raw_json": json.dumps(
            {
                "pair_address": sig["pair_address"],
                "purpose": sig["purpose"],
                "extras": sig["extras"],
            }
        ),
    }


def _adapter(
    root: Path,
    client: _Client | None = None,
    account_snapshot: dict[str, Any] | None = None,
    **kwargs: Any,
) -> BinanceSpotAdapter:
    return BinanceSpotAdapter(
        client=client if client is not None else _Client(),
        kill_switch_path=root / "absent-kill",
        api_key="test-key",
        api_secret="test-secret",
        clock_ms=lambda: 1_700_000_000_000,
        account_snapshot=_BOUND if account_snapshot is None else account_snapshot,
        production_book=REST_BOOK,
        rest_expected=_EXPECTED,
        **kwargs,
    )


def _oco_form(client: _Client) -> dict[str, list[str]]:
    signed = next(call[1] for call in client.calls if call[0] == "new_oco")
    return parse_qs(signed, keep_blank_values=True)


class ClassicPurposeTests(unittest.TestCase):
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

    def test_entry_floor_stays_at_20(self) -> None:
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, Decimal("20"))

    def test_unknown_purpose_stays_blocked_and_classic_dry_uses_signal_prices(self) -> None:
        client = _Client()
        adapter = _adapter(self.root, client)
        with mock.patch.dict(os.environ, _env(self.root)):
            race = adapter.execute(_sig(purpose="race_tech_boll_fade", mode="live_stub"))
            frozen = adapter.execute(_sig(purpose="live_tech_boll_fade", mode="live_stub"))
            dry = adapter.execute(_sig())
            under = adapter.execute(
                _sig(
                    decision_log_id=147902,
                    extras=_classic_extras(quote_order_qty="19"),
                )
            )
        self.assertEqual(race["error"], "purpose_not_live_bnb_boll_fade")
        self.assertEqual(frozen["error"], "purpose_not_live_bnb_boll_fade")
        self.assertEqual(client.calls, [])
        self.assertTrue(dry["ok"], dry)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(dry["submitted"])
        self.assertEqual(dry["exit_plan"]["stop_price"], "95")
        self.assertEqual(dry["exit_plan"]["take_profit_price"], "110")
        self.assertEqual(dry["exit_plan"]["price_source"], "signal_absolute")
        self.assertEqual(dry["exit_plan"]["protective_order"], "not_supported_not_sent")
        self.assertEqual(under["error"], "entry_notional_below_min")

    def test_shell_fills_tp10_sl5_when_absolute_prices_are_absent(self) -> None:
        adapter = _adapter(self.root)
        extras = _classic_extras()
        extras.pop("stop_price")
        extras.pop("take_profit_price")
        with mock.patch.dict(os.environ, _env(self.root)):
            dry = adapter.execute(_sig(extras=extras))
        self.assertTrue(dry["ok"], dry)
        self.assertEqual(dry["exit_plan"]["stop_price"], "95")
        self.assertEqual(dry["exit_plan"]["take_profit_price"], "110")
        self.assertEqual(dry["exit_plan"]["price_source"], CLASSIC_EXIT_SHELL)
        self.assertEqual(dry["exit_plan"]["entry_price"], "100")

    def test_live_buy_posts_native_oco_both_legs_from_signal_prices(self) -> None:
        client = _Client()
        adapter = _adapter(self.root, client, auto_flatten_on_oco_fail=True)
        with mock.patch.dict(os.environ, _env(self.root)):
            covered = adapter.execute(
                _sig(
                    mode="live_stub",
                    extras=_classic_extras(stop_price="90", take_profit_price="120"),
                )
            )
        self.assertTrue(covered["consumer_allowed"], covered)
        self.assertEqual(covered["executed"], 1)
        self.assertEqual(covered["arm_position"]["status"], "oco_working")
        self.assertEqual(covered["arm_position"]["purpose"], PURPOSE_LIVE_BNB_BOLL_CLASSIC)
        self.assertEqual(covered["arm_position"]["stop_price"], "90")
        self.assertEqual(covered["arm_position"]["take_profit_price"], "120")
        self.assertEqual(covered["exit_plan"]["protective_order"], "exchange_oco")
        kinds = [call[0] for call in client.calls]
        self.assertEqual(kinds.count("new_order"), 1)
        self.assertEqual(kinds.count("new_oco"), 1)
        oco = _oco_form(client)
        self.assertEqual(oco["side"], ["SELL"])
        self.assertEqual(oco["aboveType"], ["LIMIT_MAKER"])
        self.assertEqual(oco["belowType"], ["STOP_LOSS_LIMIT"])
        self.assertEqual(oco["abovePrice"], ["120"])
        self.assertEqual(oco["belowStopPrice"], ["90"])
        self.assertNotIn("api.binance.com", json.dumps(client.calls))

    def test_kill_switch_still_blocks_a_classic_buy(self) -> None:
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("off\n", encoding="utf-8")
        client = _Client()
        adapter = BinanceSpotAdapter(
            client=client,
            kill_switch_path=kill,
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected=_EXPECTED,
        )
        env = _env(self.root)
        env["LIVE_KILL_SWITCH_PATH"] = str(kill)
        with mock.patch.dict(os.environ, env):
            blocked = adapter.execute(_sig(mode="live_stub"))
        self.assertTrue(str(blocked["error"]).startswith("kill_switch:"))
        self.assertEqual(blocked["executed"], 0)
        self.assertEqual(client.calls, [])

    def test_one_arm_may_hold_another_symbol_but_not_a_second_of_the_same(self) -> None:
        fade_open = {
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "symbol": "CFGUSDT",
            "status": "open",
            "protection": "exchange_oco",
            "entry_order_id": "9",
            "oco_order_list_id": "3",
            "oco_above_order_id": "4",
            "oco_below_order_id": "5",
            "quantity": "1",
        }
        # No buy symbol: fail closed, same as the old one-open arm block.
        self.assertEqual(
            buy_exposure_block([fade_open], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE),
            "position_open_blocks_buy",
        )
        self.assertEqual(
            buy_exposure_block(
                [fade_open],
                [],
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                symbol="CFGUSDT",
            ),
            "position_open_blocks_buy",
        )
        self.assertIsNone(
            buy_exposure_block(
                [fade_open],
                [],
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                symbol="BNBUSDT",
            )
        )
        # Same-arm helper does not apply fade's row to classic. Cross-arm does.
        self.assertIsNone(
            buy_exposure_block([fade_open], [], purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC)
        )
        self.assertIsNone(
            buy_exposure_block(
                [fade_open],
                [],
                purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC,
                symbol="CFGUSDT",
            )
        )
        self.assertEqual(
            buy_exposure_block(
                [],
                [{"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "status": "held_naked", "symbol": "CFGUSDT"}],
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                symbol="BNBUSDT",
            ),
            "naked_holds_new_buys",
        )
        self.assertIsNone(
            buy_exposure_block(
                [],
                [{"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "status": "held_naked", "symbol": "CFGUSDT"}],
                purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC,
            )
        )
        # Untagged rows stay on the fade arm.
        self.assertEqual(
            buy_exposure_block(
                [{"status": "oco_working", "symbol": "CFGUSDT", "protection": "exchange_oco"}],
                [],
            ),
            "position_open_blocks_buy",
        )
        self.assertIsNone(
            buy_exposure_block(
                [{"status": "oco_working", "symbol": "CFGUSDT", "protection": "exchange_oco"}],
                [],
                purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC,
            )
        )

        client = _Client()
        adapter = _adapter(self.root, client, auto_flatten_on_oco_fail=True)
        adapter.buy_blocks = [fade_open]
        with mock.patch.dict(os.environ, _env(self.root)):
            opened = adapter.execute(_sig(mode="live_stub"))
        self.assertTrue(opened["consumer_allowed"], opened)
        self.assertEqual(opened["arm_position"]["symbol"], "BNBUSDT")
        self.assertEqual(_oco_form(client)["aboveType"], ["LIMIT_MAKER"])
        self.assertEqual(_oco_form(client)["belowType"], ["STOP_LOSS_LIMIT"])

        # Classic already holds BNBUSDT. A second BNB buy is still one-symbol-one-position.
        held = dict(opened["arm_position"])
        held.update(opened.get("oco_fields") or {})
        adapter.buy_blocks = [fade_open, held]
        with mock.patch.dict(os.environ, _env(self.root)):
            again = adapter.execute(_sig(mode="live_stub", decision_log_id=147903))
        self.assertEqual(again["error"], "unprotected_position_blocks_buy")
        self.assertEqual([call[0] for call in client.calls].count("new_order"), 1)

        # Fade holds CFGUSDT. A fade buy of BNBUSDT is another symbol on that arm.
        fade_client = _Client()
        fade = _adapter(self.root, fade_client, auto_flatten_on_oco_fail=True)
        fade.buy_blocks = [fade_open]
        with mock.patch.dict(os.environ, _env(self.root)):
            fade_bnb = fade.execute(
                _sig(
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    mode="live_stub",
                    decision_log_id=147904,
                    extras=_classic_extras(entry_reason="large_trade_market"),
                )
            )
        self.assertTrue(fade_bnb["consumer_allowed"], fade_bnb)
        self.assertEqual(fade_bnb["symbol"], "BNBUSDT")
        self.assertEqual([call[0] for call in fade_client.calls].count("new_order"), 1)

        same_client = _Client()
        same = _adapter(self.root, same_client)
        same.buy_blocks = [fade_open]
        with mock.patch.dict(os.environ, _env(self.root)):
            blocked = same.execute(
                _sig(
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    mode="live_stub",
                    decision_log_id=147905,
                    token_address="CFGUSDT",
                    pair_address="CFG/USDT",
                    extras=_classic_extras(
                        binance_symbol="CFGUSDT",
                        entry_reason="large_trade_market",
                    ),
                )
            )
        self.assertEqual(blocked["error"], "position_open_blocks_buy")
        self.assertEqual(same_client.calls, [])

        off_client = _Client()
        off = _adapter(self.root, off_client)
        off.buy_blocks = [fade_open]
        off_env = _env(self.root)
        off_env["BINANCE_SPOT_ALLOW_MULTI_SYMBOL"] = "0"
        with mock.patch.dict(os.environ, off_env):
            refused = off.execute(
                _sig(
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    mode="live_stub",
                    decision_log_id=147906,
                    extras=_classic_extras(entry_reason="large_trade_market"),
                )
            )
        self.assertEqual(refused["error"], "position_open_blocks_buy")
        self.assertEqual(off_client.calls, [])


class ClassicMarketProtectionTests(unittest.TestCase):
    """Deployed switches leave a rejected OCO unsold. Do not send the buy."""

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

    def test_defaults_do_not_submit_the_classic_market_repro(self) -> None:
        client = _Client()

        def _reject_oco(signed_query: str, *, api_key: str) -> dict[str, Any]:
            client.calls.append(("new_oco", signed_query, api_key))
            raise BinanceRequestError(400, code=-2010, msg="Account has insufficient balance")

        client.new_oco = _reject_oco  # type: ignore[method-assign]
        adapter = _adapter(self.root, client)
        with mock.patch.dict(os.environ, _env(self.root)):
            result = adapter.execute(
                _sig(
                    mode="live_stub",
                    extras=_classic_extras(price_usd="100", stop_price="95", take_profit_price="110"),
                )
            )
        self.assertEqual(result["error"], "buy_protection_unconfigured", result)
        self.assertFalse(result["submitted"])
        self.assertIsNone(result.get("arm_position"))
        self.assertEqual(result["oco_preflight"]["reference"], "live_mark")
        self.assertEqual(result["oco_preflight"]["protection_disposition"], "unconfigured")
        kinds = [call[0] for call in client.calls]
        self.assertNotIn("new_order", kinds)
        self.assertNotIn("new_oco", kinds)
        self.assertNotIn("oco_failed", str(result["error"]))

    def test_configured_flatten_sells_the_net_fill_after_oco_rejection(self) -> None:
        class _Reject:
            def __init__(self) -> None:
                self.calls: list[tuple[Any, ...]] = []

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                self.calls.append(("exchange_info", symbol))
                return _INFO

            def ticker_price(self, symbol: str) -> dict[str, Any]:
                self.calls.append(("ticker_price", symbol))
                return {"symbol": symbol, "price": "102"}

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_order", signed_query, api_key))
                form = parse_qs(signed_query)
                if form.get("side") == ["SELL"]:
                    return {
                        "orderId": "7701",
                        "status": "FILLED",
                        "executedQty": form.get("quantity", ["0"])[0],
                        "origQty": form.get("quantity", ["0"])[0],
                        "symbol": "BNBUSDT",
                    }
                return {
                    "orderId": "99",
                    "status": "FILLED",
                    "executedQty": "0.25",
                    "cummulativeQuoteQty": "25.5",
                    "fills": [
                        {
                            "commission": "0.001",
                            "commissionAsset": "BNB",
                            "qty": "0.25",
                        }
                    ],
                    "symbol": "BNBUSDT",
                }

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_oco", signed_query, api_key))
                raise BinanceRequestError(400, code=-2010, msg="Account has insufficient balance")

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("get_order_list", signed_query, api_key))
                raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")

            def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("get_order", signed_query, api_key))
                raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")

        client = _Reject()
        adapter = _adapter(self.root, client, auto_flatten_on_oco_fail=True)  # type: ignore[arg-type]
        with mock.patch.dict(os.environ, _env(self.root)):
            result = adapter.execute(
                _sig(
                    mode="live_stub",
                    extras=_classic_extras(
                        quote_order_qty="25",
                        price_usd="100",
                        stop_price="95",
                        take_profit_price="110",
                    ),
                )
            )
        kinds = [call[0] for call in client.calls]
        self.assertEqual(kinds.count("new_oco"), 1, kinds)
        sells = [parse_qs(call[1]) for call in client.calls if call[0] == "new_order" and parse_qs(call[1]).get("side") == ["SELL"]]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["type"], ["MARKET"])
        self.assertEqual(sells[0]["quantity"], ["0.24"])
        self.assertEqual(result["protection_state"], "dust_residual", result)
        self.assertEqual(result["arm_position"]["status"], "closed_dust")
        self.assertEqual(result["arm_position"]["protection"], "dust_abandon")
        self.assertEqual(result["arm_position"]["residual_qty"], "0.009")
        self.assertEqual(result["arm_position"]["exit_filled_qty"], "0.24")
        self.assertNotEqual(result["protection_state"], "exited")
        self.assertFalse(result["protection_incident"]["delivered"])

    def test_oco_timeout_does_not_sell_or_repost_before_reconciliation(self) -> None:
        class _Timeout:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _INFO

            def ticker_price(self, symbol: str) -> dict[str, Any]:
                return {"symbol": symbol, "price": "102"}

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("new_order")
                return {
                    "orderId": "99",
                    "status": "FILLED",
                    "executedQty": "0.25",
                    "cummulativeQuoteQty": "25.5",
                    "symbol": "BNBUSDT",
                }

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("new_oco")
                raise TimeoutError("timed out")

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("get_order_list")
                raise TimeoutError("list timed out")

        client = _Timeout()
        adapter = _adapter(self.root, client, auto_flatten_on_oco_fail=True)  # type: ignore[arg-type]
        with mock.patch.dict(os.environ, _env(self.root)):
            result = adapter.execute(_sig(mode="live_stub"))
        self.assertEqual(client.calls.count("new_oco"), 1)
        self.assertEqual(client.calls.count("new_order"), 1)
        self.assertEqual(result["protection_state"], "unprotected", result)
        self.assertEqual(result["manual_action"], "halt_new_buys_and_manually_close_or_protect_the_fill")
        self.assertIn("oco_submit_timeout", str(result["error"]))
        self.assertFalse(result["consumer_allowed"])

    def test_rejected_flatten_is_unprotected_not_a_blocked_buy(self) -> None:
        class _SellReject:
            def __init__(self) -> None:
                self.sells = 0

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _INFO

            def ticker_price(self, symbol: str) -> dict[str, Any]:
                return {"symbol": symbol, "price": "102"}

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                form = parse_qs(signed_query)
                if form.get("side") == ["SELL"]:
                    self.sells += 1
                    raise BinanceRequestError(400, code=-2010, msg="Account has insufficient balance")
                return {
                    "orderId": "99",
                    "status": "FILLED",
                    "executedQty": "0.25",
                    "cummulativeQuoteQty": "25.5",
                    "symbol": "BNBUSDT",
                }

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise BinanceRequestError(400, code=-2010, msg="Account has insufficient balance")

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")

            def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")

        client = _SellReject()
        adapter = _adapter(self.root, client, auto_flatten_on_oco_fail=True)  # type: ignore[arg-type]
        with mock.patch.dict(os.environ, _env(self.root)):
            result = adapter.execute(_sig(mode="live_stub"))
        self.assertEqual(client.sells, 1)
        self.assertEqual(result["protection_state"], "unprotected", result)
        self.assertEqual(result["manual_action"], "halt_new_buys_and_manually_close_or_protect_the_fill")
        self.assertFalse(result["consumer_allowed"])
        self.assertNotEqual(result["arm_position"]["status"], "closed")


def _trading_info(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "filters": _FILTERS,
    }


class FillNetTests(unittest.TestCase):
    """Commission is subtracted only when it is the traded base. Do not change that."""

    def test_third_asset_quote_and_two_base_fills(self) -> None:
        eth = {
            "executedQty": "1.5",
            "fills": [
                {"qty": "1.5", "commission": "0.0001", "commissionAsset": "BNB"},
            ],
        }
        self.assertEqual(sellable_base_qty(eth, "ETHUSDT"), "1.5")
        quote_fee = {
            "executedQty": "0.25",
            "fills": [
                {"qty": "0.25", "commission": "0.01", "commissionAsset": "USDT"},
            ],
        }
        self.assertEqual(sellable_base_qty(quote_fee, "BNBUSDT"), "0.25")
        two = {
            "executedQty": "0.25",
            "fills": [
                {"qty": "0.10", "commission": "0.0004", "commissionAsset": "BNB"},
                {"qty": "0.15", "commission": "0.0006", "commissionAsset": "BNB"},
            ],
        }
        self.assertEqual(sellable_base_qty(two, "BNBUSDT"), "0.249")
        missing = {"executedQty": "0.25"}
        self.assertEqual(sellable_base_qty(missing, "BNBUSDT"), "0.25")


class ClassicGatewayProtectionTests(unittest.TestCase):
    """The adapter result is not the book. These go through the gateway ledger."""

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

    @contextmanager
    def _gateway(self, client: Any, purposes: frozenset[str]) -> Any:
        ledger_path = self.root / "ledger.db"
        registry = self.root / "registry.json"
        registry.write_text(
            json.dumps({"ledgers": [str(ledger_path.resolve())]}),
            encoding="utf-8",
        )
        os.environ[LEDGER_REGISTRY_ENV] = str(registry)
        snapshot = dict(_BOUND)
        snapshot["uid"] = 424242
        with ExecutionGateway(
            decision_db=self.root / "decision.db",
            ledger_path=ledger_path,
            paper_enabled=False,
            pm_enabled=False,
            accept_purposes=purposes,
            binance_spot=_adapter(
                self.root,
                client,
                auto_flatten_on_oco_fail=True,
                account_snapshot=snapshot,
            ),
        ) as gateway:
            bound = initialize_spot_ledger_binding(ledger_path, "424242")
            if bound:
                raise RuntimeError(bound)
            yield gateway

    def test_dust_remainder_is_persisted_and_blocks_the_next_same_purpose_buy(self) -> None:
        class _Book:
            def __init__(self) -> None:
                self.calls: list[tuple[Any, ...]] = []

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _trading_info(symbol)

            def ticker_price(self, symbol: str) -> dict[str, Any]:
                return {"symbol": symbol, "price": "102"}

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_order", signed_query))
                form = parse_qs(signed_query)
                if form.get("side") == ["SELL"]:
                    qty = form.get("quantity", ["0"])[0]
                    return {
                        "orderId": "7701",
                        "status": "FILLED",
                        "executedQty": qty,
                        "origQty": qty,
                        "symbol": "BNBUSDT",
                    }
                return {
                    "orderId": "99",
                    "status": "FILLED",
                    "executedQty": "0.25",
                    "cummulativeQuoteQty": "25.5",
                    "fills": [
                        {"commission": "0.001", "commissionAsset": "BNB", "qty": "0.25"}
                    ],
                    "symbol": "BNBUSDT",
                }

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_oco", signed_query))
                raise BinanceRequestError(400, code=-2010, msg="Account has insufficient balance")

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")

            def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")

        client = _Book()
        purposes = frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC, PURPOSE_LIVE_BNB_BOLL_FADE})
        with mock.patch.dict(os.environ, _env(self.root)):
            with self._gateway(client, purposes) as gw:
                opened = gw.process_one(_decision_row(mode="live_stub"))
                row = gw.ledger.binance_spot_position_by_decision(147901)
                events = gw.ledger.conn.execute(
                    "SELECT event, detail_json FROM order_events WHERE event = 'rejected'"
                ).fetchall()
        self.assertEqual(opened["exec_result"]["protection_state"], "dust_residual", opened)
        self.assertNotEqual(opened["exec_result"]["protection_state"], "exited")
        self.assertFalse(opened["exec_result"]["protection_incident"]["delivered"])
        assert row is not None
        self.assertEqual(row["status"], "closed_dust")
        self.assertEqual(row["protection"], "dust_abandon")
        self.assertNotEqual(row["protection"], "auto_flatten")
        self.assertEqual(row["residual_qty"], "0.009")
        self.assertEqual(row["exit_filled_qty"], "0.24")
        self.assertTrue(events)
        detail = json.loads(events[-1][1])
        self.assertEqual(detail["delivered"], False)
        self.assertEqual(detail["transport"], "none")
        self.assertEqual(detail["protection_state"], "dust_residual")
        buys_before = sum(
            1
            for call in client.calls
            if call[0] == "new_order" and parse_qs(call[1]).get("side") == ["BUY"]
        )
        with mock.patch.dict(os.environ, _env(self.root)):
            with self._gateway(client, purposes) as gw:
                again = gw.process_one(
                    _decision_row(mode="live_stub", decision_log_id=147902)
                )
        self.assertEqual(again["error"], "unprotected_position_blocks_buy", again)
        buys_after = sum(
            1
            for call in client.calls
            if call[0] == "new_order" and parse_qs(call[1]).get("side") == ["BUY"]
        )
        self.assertEqual(buys_after, buys_before)
        with mock.patch.dict(os.environ, _env(self.root)):
            with self._gateway(client, purposes) as gw:
                other = gw.process_one(
                    _decision_row(
                        mode="live_stub",
                        decision_log_id=147903,
                        purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                        token_address="ETHUSDT",
                        pair_address="ETH/USDT",
                        extras=_classic_extras(binance_symbol="ETHUSDT"),
                    )
                )
                kept = gw.ledger.binance_spot_position_by_decision(147901)
        self.assertEqual(other["error"], "unprotected_position_blocks_buy", other)
        assert kept is not None
        self.assertEqual(kept["status"], "closed_dust")
        self.assertEqual(kept["residual_qty"], "0.009")
        kept_id = int(kept["id"])
        with mock.patch.dict(os.environ, _env(self.root)):
            with self._gateway(client, purposes) as gw:
                before = gw.ledger.conn.execute(
                    "SELECT COUNT(*) AS n FROM order_events"
                ).fetchone()["n"]
                gw.ledger.update_binance_spot_position(kept_id, {"status": "closed_canceled"})
                after_update = gw.ledger.conn.execute(
                    "SELECT COUNT(*) AS n FROM order_events"
                ).fetchone()["n"]
                self.assertEqual(after_update, before)
                cleared = gw.process_one(
                    _decision_row(
                        mode="live_stub",
                        decision_log_id=147904,
                        purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                        token_address="ETHUSDT",
                        pair_address="ETH/USDT",
                        extras=_classic_extras(binance_symbol="ETHUSDT"),
                    )
                )
                still = gw.ledger.binance_spot_position_by_id(kept_id)
        self.assertEqual(cleared["error"], "unprotected_position_blocks_buy", cleared)
        self.assertEqual(
            [call for call in client.calls if call[0] == "new_order" and parse_qs(call[1]).get("symbol") == ["ETHUSDT"]],
            [],
        )
        assert still is not None
        self.assertEqual(still["id"], kept_id)
        self.assertEqual(still["residual_qty"], "0.009")

    def test_peer_file_unresolved_row_blocks_a_trading_symbol(self) -> None:
        fade = self.root / "execution_ledger_live_bnb_boll_fade.db"
        classic = self.root / "execution_ledger_live_bnb_boll_classic.db"
        snapshot = dict(_BOUND)
        snapshot["uid"] = 424242
        registry = self.root / "registry-peer.json"
        registry.write_text(
            json.dumps({"ledgers": [str(fade.resolve()), str(classic.resolve())]}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, _env(self.root)):
            os.environ[LEDGER_REGISTRY_ENV] = str(registry)
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=fade,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                binance_spot=_adapter(
                    self.root, _Client(), auto_flatten_on_oco_fail=True, account_snapshot=snapshot
                ),
            ) as gw:
                initialize_spot_ledger_binding(fade, "424242")
                gw.ledger.arm_binance_spot_position(
                    {
                        "decision_log_id": 147880,
                        "symbol": "BNBUSDT",
                        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                        "status": "oco_unknown",
                        "quantity": "0.25",
                        "stop_price": "95",
                        "take_profit_price": "110",
                        "entry_price": "102",
                        "entry_order_id": "99",
                        "protection": "oco_pending",
                    }
                )
            with ExecutionGateway(
                decision_db=self.root / "decision-classic.db",
                ledger_path=classic,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(
                    self.root, _Client(), auto_flatten_on_oco_fail=True, account_snapshot=snapshot
                ),
            ) as gw:
                initialize_spot_ledger_binding(classic, "424242")
            client = _Client()
            with ExecutionGateway(
                decision_db=self.root / "decision-classic.db",
                ledger_path=classic,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(
                    self.root, client, auto_flatten_on_oco_fail=True, account_snapshot=snapshot
                ),
            ) as gw:
                refused = gw.process_one(
                    _decision_row(
                        mode="live_stub",
                        decision_log_id=147881,
                        token_address="ETHUSDT",
                        pair_address="ETH/USDT",
                        extras=_classic_extras(binance_symbol="ETHUSDT"),
                    )
                )
        self.assertEqual(refused["error"], "oco_failed_blocks_buy", refused)
        self.assertEqual([call for call in client.calls if call[0] == "new_order"], [])

    def test_missing_required_peer_and_unmapped_ledger_do_not_buy(self) -> None:
        snapshot = dict(_BOUND)
        snapshot["uid"] = 424242
        client = _Client()

        def _run(ledger: Path, registry: dict[str, Any] | None) -> dict[str, Any]:
            if registry is None:
                os.environ.pop(LEDGER_REGISTRY_ENV, None)
            else:
                path = self.root / "registry-missing.json"
                path.write_text(json.dumps(registry), encoding="utf-8")
                os.environ[LEDGER_REGISTRY_ENV] = str(path)
            with ExecutionGateway(
                decision_db=self.root / "decision-missing.db",
                ledger_path=ledger,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(
                    self.root,
                    client,
                    auto_flatten_on_oco_fail=True,
                    account_snapshot=snapshot,
                ),
            ) as gw:
                if registry is not None and ledger.is_file():
                    initialize_spot_ledger_binding(ledger, "424242")
                return gw.process_one(
                    _decision_row(
                        mode="live_stub",
                        token_address="ETHUSDT",
                        pair_address="ETH/USDT",
                        extras=_classic_extras(binance_symbol="ETHUSDT"),
                    )
                )

        with mock.patch.dict(os.environ, _env(self.root)):
            unmapped = _run(self.root / "custom.db", None)
        self.assertEqual(unmapped["error"], "ledger_registry_unmapped", unmapped)
        self.assertEqual([call for call in client.calls if call[0] == "new_order"], [])

        classic = self.root / "execution_ledger_live_bnb_boll_classic.db"
        fade = self.root / "execution_ledger_live_bnb_boll_fade.db"
        with mock.patch.dict(os.environ, _env(self.root)):
            with ExecutionGateway(
                decision_db=self.root / "decision-init.db",
                ledger_path=classic,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(self.root, _Client(), auto_flatten_on_oco_fail=True),
            ):
                pass
            absent = _run(
                classic,
                {"ledgers": [str(classic.resolve()), str(fade.resolve())]},
            )
        self.assertEqual(absent["error"], "peer_ledger_absent", absent)
        self.assertEqual([call for call in client.calls if call[0] == "new_order"], [])

    def test_unreadable_peer_ledger_blocks_the_buy(self) -> None:
        classic = self.root / "execution_ledger_live_bnb_boll_classic.db"
        fade = self.root / "execution_ledger_live_bnb_boll_fade.db"
        fade.write_bytes(b"not a sqlite ledger")
        snapshot = dict(_BOUND)
        snapshot["uid"] = 424242
        registry = self.root / "registry-bad.json"
        registry.write_text(
            json.dumps({"ledgers": [str(classic.resolve()), str(fade.resolve())]}),
            encoding="utf-8",
        )
        client = _Client()
        with mock.patch.dict(os.environ, _env(self.root)):
            os.environ[LEDGER_REGISTRY_ENV] = str(registry)
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=classic,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(
                    self.root, client, auto_flatten_on_oco_fail=True, account_snapshot=snapshot
                ),
            ) as gw:
                initialize_spot_ledger_binding(classic, "424242")
                refused = gw.process_one(_decision_row(mode="live_stub"))
        self.assertEqual(refused["error"], "peer_ledger_unreadable", refused)
        self.assertEqual([call for call in client.calls if call[0] == "new_order"], [])

    def test_unknown_oco_blocks_known_predicates_and_a_valid_other_symbol_is_not_those(self) -> None:
        class _Timeout:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []
                self.list_payload: dict[str, Any] | None = None

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _trading_info(symbol)

            def ticker_price(self, symbol: str) -> dict[str, Any]:
                return {"symbol": symbol, "price": "102"}

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_order", signed_query))
                return {
                    "orderId": "99",
                    "status": "FILLED",
                    "executedQty": "0.25",
                    "cummulativeQuoteQty": "25.5",
                    "fills": [],
                    "symbol": parse_qs(signed_query).get("symbol", ["BNBUSDT"])[0],
                }

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_oco", signed_query))
                raise TimeoutError("timed out")

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("get_order_list", signed_query))
                if self.list_payload is not None:
                    return self.list_payload
                raise TimeoutError("list timed out")

        client = _Timeout()
        purposes = frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC, PURPOSE_LIVE_BNB_BOLL_FADE})
        with mock.patch.dict(os.environ, _env(self.root)):
            with self._gateway(client, purposes) as gw:
                opened = gw.process_one(_decision_row(mode="live_stub"))
                self.assertEqual(opened["exec_result"]["protection_state"], "unprotected", opened)
                list(gw.tick_binance_protective_exits({"BNBUSDT": "102"}))
                row = gw.ledger.binance_spot_position_by_decision(147901)
        assert row is not None
        self.assertEqual(row["status"], "oco_unknown")
        self.assertEqual(sum(1 for call in client.calls if call[0] == "new_oco"), 1)
        self.assertFalse(any(parse_qs(call[1]).get("side") == ["SELL"] for call in client.calls if call[0] == "new_order"))

        def _eth(purpose: str, decision_id: int) -> dict[str, Any]:
            return _decision_row(
                mode="live_stub",
                decision_log_id=decision_id,
                purpose=purpose,
                token_address="ETHUSDT",
                pair_address="ETH/USDT",
                extras=_classic_extras(binance_symbol="ETHUSDT"),
            )

        with mock.patch.dict(os.environ, _env(self.root)):
            with self._gateway(client, purposes) as gw:
                same_arm = gw.process_one(_eth(PURPOSE_LIVE_BNB_BOLL_CLASSIC, 147911))
                same_symbol = gw.process_one(
                    _decision_row(
                        mode="live_stub",
                        decision_log_id=147912,
                        purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    )
                )
                other = gw.process_one(_eth(PURPOSE_LIVE_BNB_BOLL_FADE, 147913))
        self.assertEqual(same_arm["error"], "oco_failed_blocks_buy", same_arm)
        self.assertEqual(same_symbol["error"], "oco_failed_blocks_buy", same_symbol)
        self.assertEqual(other["error"], "oco_failed_blocks_buy", other)
        self.assertNotEqual(other["error"], "symbol_not_trading")
        eth_buys = [
            call
            for call in client.calls
            if call[0] == "new_order" and parse_qs(call[1]).get("symbol") == ["ETHUSDT"]
        ]
        self.assertEqual(eth_buys, [])
        self.assertEqual(sum(1 for call in client.calls if call[0] == "new_oco"), 1)
        kept_id = int(row["id"])
        with mock.patch.dict(os.environ, _env(self.root)):
            with self._gateway(client, purposes) as gw:
                current = gw.ledger.binance_spot_position_by_id(kept_id)
                assert current is not None
                gw.ledger.update_binance_spot_position(kept_id, {"status": "oco_working"})
                forged = gw.process_one(_eth(PURPOSE_LIVE_BNB_BOLL_FADE, 147914))
                bare = gw.ledger.binance_spot_position_by_id(kept_id)
        self.assertEqual(forged["error"], "oco_failed_blocks_buy", forged)
        assert bare is not None
        self.assertEqual(bare["id"], kept_id)
        self.assertEqual(
            [call for call in client.calls if call[0] == "new_order" and parse_qs(call[1]).get("symbol") == ["ETHUSDT"]],
            [],
        )
        client.list_payload = {
            "orderListId": 42,
            "listOrderStatus": "EXECUTING",
            "listStatusType": "EXEC_STARTED",
            "orderReports": [
                {"orderId": 11, "type": "LIMIT_MAKER", "status": "NEW", "executedQty": "0"},
                {"orderId": 10, "type": "STOP_LOSS_LIMIT", "status": "NEW", "executedQty": "0"},
            ],
        }
        with mock.patch.dict(os.environ, _env(self.root)):
            with self._gateway(client, frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC})) as gw:
                list(gw.tick_binance_protective_exits({"BNBUSDT": "102"}))
                read_back = gw.ledger.binance_spot_position_by_id(kept_id)
            with self._gateway(client, purposes) as gw:
                resolved = gw.process_one(_eth(PURPOSE_LIVE_BNB_BOLL_FADE, 147915))
        assert read_back is not None
        self.assertEqual(read_back["id"], kept_id)
        self.assertEqual(read_back["status"], "oco_working", read_back)
        self.assertEqual(str(read_back.get("oco_order_list_id")), "42", read_back)
        self.assertNotEqual(resolved["error"], "oco_failed_blocks_buy", resolved)
        eth_buys = [
            parse_qs(call[1])
            for call in client.calls
            if call[0] == "new_order" and parse_qs(call[1]).get("symbol") == ["ETHUSDT"]
        ]
        self.assertEqual(len(eth_buys), 1)
        self.assertEqual(eth_buys[0]["side"], ["BUY"])


class ClassicGatewayTests(unittest.TestCase):
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

    def test_accept_purpose_routes_classic_to_the_adapter_not_paper(self) -> None:
        client = _Client()
        with mock.patch.dict(os.environ, _env(self.root)):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=self.root / "execution_ledger_live_bnb_boll_classic.db",
                paper_enabled=True,
                paper_db=self.root / "paper_eq_unused.db",
                paper_executor=_Boom(),
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(self.root, client),
            ) as gw:
                dry = gw.process_one(_decision_row())
                other = gw.process_one(
                    _decision_row(
                        decision_log_id=147910,
                        purpose="race_tech_boll_fade",
                        mode="dry_run",
                    )
                )
        self.assertEqual(dry["executor"], BINANCE_SPOT_EXECUTOR)
        self.assertEqual(dry["receipt"]["status"], "dry_run_acked")
        self.assertEqual(dry["executed"], 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(other["executed"], 0)
        self.assertIn("purpose_invalid", other["validation_errors"])

    def test_shared_ledger_lets_classic_open_beside_a_fade_position(self) -> None:
        client = _Client()
        ledger_path = self.root / "execution_ledger_shared_arms.db"
        ledger = ExecutionLedger(ledger_path)
        armed = ledger.arm_binance_spot_position(
            {
                "decision_log_id": 147800,
                "symbol": "CFGUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "open",
                "quantity": "10",
                "stop_price": "1",
                "take_profit_price": "2",
                "entry_price": "1.5",
                "entry_order_id": "9",
                "protection": "exchange_oco",
            }
        )
        ledger.update_binance_spot_position(
            int(armed["id"]),
            {
                "oco_order_list_id": "3",
                "oco_above_order_id": "4",
                "oco_below_order_id": "5",
                "protection": "exchange_oco",
                "status": "open",
            },
        )
        self.assertEqual(
            ledger.list_binance_spot_buy_blocks(purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC),
            [],
        )
        self.assertEqual(
            len(ledger.list_binance_spot_buy_blocks(purpose=PURPOSE_LIVE_BNB_BOLL_FADE)),
            1,
        )
        ledger.close()
        registry = self.root / "registry.json"
        registry.write_text(
            json.dumps({"ledgers": [str(ledger_path.resolve())]}),
            encoding="utf-8",
        )
        env = _env(self.root)
        env[LEDGER_REGISTRY_ENV] = str(registry)
        snapshot = dict(_BOUND)
        snapshot["uid"] = 424242
        with mock.patch.dict(os.environ, env):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=ledger_path,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(
                    self.root,
                    client,
                    auto_flatten_on_oco_fail=True,
                    account_snapshot=snapshot,
                ),
            ) as gw:
                initialize_spot_ledger_binding(ledger_path, "424242")
                opened = gw.process_one(_decision_row(mode="live_stub"))
                self.assertEqual(opened["executed"], 1, opened)
                self.assertEqual(opened["receipt"]["held_position"]["purpose"], PURPOSE_LIVE_BNB_BOLL_CLASSIC)
                self.assertEqual(opened["receipt"]["status"], "binance_spot_submitted")
                exits = tick_live_bnb_exits(gw, frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}))
                fade_rows = gw.ledger.list_binance_spot_positions_for_exit(
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE
                )
                classic_rows = gw.ledger.list_binance_spot_positions_for_exit(
                    purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC
                )
        self.assertEqual(_oco_form(client)["belowType"], ["STOP_LOSS_LIMIT"])
        self.assertEqual(_oco_form(client)["aboveType"], ["LIMIT_MAKER"])
        self.assertEqual(len(fade_rows), 1)
        self.assertEqual(fade_rows[0]["symbol"], "CFGUSDT")
        self.assertEqual(len(classic_rows), 1)
        self.assertEqual(classic_rows[0]["symbol"], "BNBUSDT")
        self.assertTrue(exits)
        self.assertEqual(exits[0]["symbol"], "BNBUSDT")
        self.assertNotEqual(exits[0]["symbol"], "CFGUSDT")


class ClassicIsolationTests(unittest.TestCase):
    def test_classic_is_its_own_consumer_and_fade_guard_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "execution_ledger.db"
            fade = root / "execution_ledger_live_bnb_boll_fade.db"
            classic = root / "execution_ledger_live_bnb_boll_classic.db"
            with mock.patch("execution.run._default_main_ledger_path", return_value=main):
                self.assertIsNone(
                    _peer_isolation_guard(
                        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                        ledger_path=classic,
                        paper_db=None,
                        paper_enabled=False,
                    )
                )
                self.assertIsNone(
                    _peer_isolation_guard(
                        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                        ledger_path=fade,
                        paper_db=None,
                        paper_enabled=False,
                    )
                )
                self.assertIn(
                    "own --ledger",
                    _peer_isolation_guard(
                        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                        ledger_path=fade,
                        paper_db=None,
                        paper_enabled=False,
                    )
                    or "",
                )
                self.assertIn(
                    "only",
                    _peer_isolation_guard(
                        accept_purposes=frozenset(
                            {PURPOSE_LIVE_BNB_BOLL_FADE, PURPOSE_LIVE_BNB_BOLL_CLASSIC}
                        ),
                        ledger_path=classic,
                        paper_db=None,
                        paper_enabled=False,
                    )
                    or "",
                )
                self.assertIn(
                    "paper",
                    _peer_isolation_guard(
                        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                        ledger_path=classic,
                        paper_db=None,
                        paper_enabled=True,
                    )
                    or "",
                )

    def test_mcp_gate_accepts_classic_and_still_blocks_unknown(self) -> None:
        broker = LiveMCPBroker(transport=None)
        classic = broker.evaluate(
            {
                "purpose": PURPOSE_LIVE_BNB_BOLL_CLASSIC,
                "action": "buy",
                "decision_log_id": 147901,
                "extras": {"binance_symbol": "BNBUSDT"},
            },
            {"canonical_positions": [], "spent_orders": [], "open_orders": []},
        )
        self.assertNotEqual(classic["reason"], "purpose_not_live_bnb_boll_fade")
        self.assertEqual(classic["reason"], "protection_tool_absent")
        self.assertFalse(classic["oco_placed"])
        beside_fade = broker.evaluate(
            {
                "purpose": PURPOSE_LIVE_BNB_BOLL_CLASSIC,
                "action": "buy",
                "decision_log_id": 147902,
                "extras": {"binance_symbol": "BNBUSDT"},
            },
            {
                "canonical_positions": [
                    {
                        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                        "status": "held_naked",
                        "symbol": "CFGUSDT",
                    }
                ],
                "spent_orders": [],
                "open_orders": [],
            },
        )
        self.assertNotEqual(beside_fade["reason"], "naked_holds_new_buys")
        unknown = broker.evaluate(
            {"purpose": "race_tech_boll_fade", "action": "buy", "decision_log_id": 3, "extras": {}},
            {},
        )
        self.assertEqual(unknown["reason"], "purpose_not_live_bnb_boll_fade")


if __name__ == "__main__":
    unittest.main()
