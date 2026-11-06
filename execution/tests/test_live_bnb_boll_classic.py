"""Classic live arm: purpose gate, native OCO, per-purpose buy blocks. No live HTTP.

    python -m unittest execution.tests.test_live_bnb_boll_classic
"""

from __future__ import annotations

import json
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

from execution.executors.binance_mcp_broker import LiveMCPBroker  # noqa: E402
from execution.executors.binance_spot import (  # noqa: E402
    CLASSIC_EXIT_SHELL,
    BinanceSpotAdapter,
)
from execution.executors.binance_spot_gate import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    REST_BOOK,
    buy_exposure_block,
)
from execution.executors.binance_spot_protect import ENTRY_MIN_NOTIONAL_USDT  # noqa: E402
from execution.gateway import BINANCE_SPOT_EXECUTOR, ExecutionGateway  # noqa: E402
from execution.ledger import ExecutionLedger  # noqa: E402
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


def _adapter(root: Path, client: _Client | None = None) -> BinanceSpotAdapter:
    return BinanceSpotAdapter(
        client=client if client is not None else _Client(),
        kill_switch_path=root / "absent-kill",
        api_key="test-key",
        api_secret="test-secret",
        clock_ms=lambda: 1_700_000_000_000,
        account_snapshot=_BOUND,
        production_book=REST_BOOK,
        rest_expected=_EXPECTED,
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
        adapter = _adapter(self.root, client)
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
        # The other arm is unchanged, including a buy of the symbol fade holds.
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
        adapter = _adapter(self.root, client)
        adapter.buy_blocks = [fade_open]
        with mock.patch.dict(os.environ, _env(self.root)):
            opened = adapter.execute(_sig(mode="live_stub"))
        self.assertTrue(opened["consumer_allowed"], opened)
        self.assertEqual(opened["arm_position"]["symbol"], "BNBUSDT")
        self.assertEqual(_oco_form(client)["aboveType"], ["LIMIT_MAKER"])
        self.assertEqual(_oco_form(client)["belowType"], ["STOP_LOSS_LIMIT"])

        # Classic already holds BNBUSDT. A second BNB buy is still one-symbol-one-position.
        adapter.buy_blocks = [fade_open, opened["arm_position"]]
        with mock.patch.dict(os.environ, _env(self.root)):
            again = adapter.execute(_sig(mode="live_stub", decision_log_id=147903))
        self.assertEqual(again["error"], "position_open_blocks_buy")
        self.assertEqual([call[0] for call in client.calls].count("new_order"), 1)

        # Fade holds CFGUSDT. A fade buy of BNBUSDT is another symbol on that arm.
        fade_client = _Client()
        fade = _adapter(self.root, fade_client)
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
        with mock.patch.dict(os.environ, _env(self.root)):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=ledger_path,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(self.root, client),
            ) as gw:
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
