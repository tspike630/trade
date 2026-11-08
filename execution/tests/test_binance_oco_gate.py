"""Dry/mock gates for native spot OCO. No live HTTP and no secrets.

    python -m unittest execution.tests.test_binance_oco_gate
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_mcp_broker import (  # noqa: E402
    DEFAULT_QUOTE_REDUCE_CAP,
    LiveMCPBroker,
)
from execution.executors.binance_spot import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    BinanceRequestError,
    BinanceSpotAdapter,
    BinanceSpotHttpClient,
    OCO_PATH,
    held_position_spec,
    sign_query,
)
from execution.executors.binance_spot_gate import (  # noqa: E402
    FROZEN_CLIENT_ID,
    FROZEN_DECISION_ID,
    MCP_BOOK,
    MCP_POSITION_UID,
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    REST_BOOK,
    VERIFIED_FET_EXIT,
    acceptance_report,
    account_book_block,
    bind_rest_book,
    buy_exposure_block,
    classify_sub_account_list,
    native_oco_verified,
    replay_block,
    replay_scope,
)
from execution.executors.binance_spot_protect import (  # noqa: E402
    ENTRY_MIN_NOTIONAL_USDT,
    build_spot_oco,
    reference_protection_prices,
)
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.ledger import ExecutionLedger  # noqa: E402
from execution.live_bnb_dispatch import VERIFIED_FET_FILL, dispatch_live_bnb, import_verified_fet_fill  # noqa: E402

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
# Fixture REST book. No uid. USDT free is not the MCP account's 437.63809903.
_BOUND = {
    "accountType": "SPOT",
    "canTrade": True,
    "permissions": ["SPOT"],
    "restrictions": {
        "enableSpotAndMarginTrading": True,
        "enableReading": True,
        "ipRestrict": True,
    },
    "balances": [
        {"asset": "USDT", "free": "1000.00", "locked": "0"},
        {"asset": "FET", "free": "0", "locked": "0"},
    ],
}
_EXPECTED = {"account_type": "SPOT", "usdt_free": "1000"}


def _working(*, list_id: object = 42, above: object = 11, below: object = 10) -> dict[str, Any]:
    return {
        "orderListId": list_id,
        "listOrderStatus": "EXECUTING",
        "listStatusType": "EXEC_STARTED",
        "orderReports": [
            {"orderId": above, "type": "LIMIT_MAKER", "status": "NEW", "executedQty": "0"},
            {
                "orderId": below,
                "type": "STOP_LOSS_LIMIT",
                "status": "NEW",
                "stopPrice": "99",
                "executedQty": "0",
            },
        ],
    }


class _Client:
    def __init__(self, oco: dict[str, Any] | Exception | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.oco: dict[str, Any] | Exception = _working() if oco is None else oco

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _INFO

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        if "side=SELL" in signed_query:
            return {"orderId": "77", "status": "FILLED", "executedQty": "1.23"}
        return {"orderId": "99", "status": "FILLED", "executedQty": "1.239", "symbol": "BNBUSDT"}

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        if isinstance(self.oco, Exception):
            raise self.oco
        return self.oco

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order_list", signed_query, api_key))
        if isinstance(self.oco, Exception):
            raise self.oco
        return self.oco

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": "102"}


def _buy(decision_id: int = 147841) -> dict[str, Any]:
    return {
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "action": "buy",
        "mode": "live_stub",
        "decision_log_id": decision_id,
        "token_address": "BNBUSDT",
        "extras": {
            "binance_symbol": "BNBUSDT",
            "order_type": "MARKET",
            "quote_order_qty": "25",
            "price_usd": "102",
            "stop_price": "99",
            "take_profit_price": "110",
            "entry_reason": "large_trade_market",
        },
    }


def _env(root: Path) -> dict[str, str]:
    return {
        "BINANCE_SPOT_LIVE_ENABLED": "1",
        "LIVE_KILL_SWITCH_PATH": str(root / "absent-kill"),
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "BINANCE_SPOT_ACCOUNT_UID": "",
        "BINANCE_SPOT_PRODUCTION_BOOK": "",
    }


class NativeOcoReceiptTests(unittest.TestCase):
    def test_signed_oco_posts_both_legs_and_accepts_order_list_id_zero(self) -> None:
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, Decimal("20"))
        self.assertEqual(DEFAULT_QUOTE_REDUCE_CAP, Decimal("0.05"))
        spec, error = build_spot_oco(
            symbol="BNBUSDT",
            quantity="1.239",
            stop_price="99",
            take_profit_price="110",
            decision_id=147841,
            filters=_FILTERS,
            mark="102",
            timestamp_ms=1_700_000_000_000,
        )
        self.assertIsNone(error)
        assert spec is not None
        signed = sign_query(spec["pairs"], "test-secret")
        form = parse_qs(signed)
        self.assertEqual(form["symbol"], ["BNBUSDT"])
        self.assertEqual(form["side"], ["SELL"])
        self.assertEqual(form["quantity"], ["1.23"])
        self.assertEqual(form["aboveType"], ["LIMIT_MAKER"])
        self.assertEqual(form["abovePrice"], ["110"])
        self.assertEqual(form["aboveClientOrderId"], ["bnbfade-tp-147841"])
        self.assertEqual(form["belowType"], ["STOP_LOSS_LIMIT"])
        self.assertEqual(form["belowStopPrice"], ["99"])
        self.assertEqual(form["belowPrice"], ["98.99"])
        self.assertEqual(form["belowTimeInForce"], ["GTC"])
        self.assertEqual(form["belowClientOrderId"], ["bnbfade-sl-147841"])
        self.assertEqual(form["listClientOrderId"], ["bnbfade-ol-147841"])
        self.assertEqual(form["newOrderRespType"], ["FULL"])
        self.assertIn("signature", form)
        self.assertNotIn("test-secret", signed)
        seen: dict[str, Any] = {}

        def opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["body"] = request.data
            return 200, json.dumps(_working(list_id=0, above=8, below=9)).encode("utf-8")

        with mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled"),
        ):
            http = BinanceSpotHttpClient(base_url="https://api.binance.test", opener=opener)
            payload = http.new_oco(signed, api_key="test-key")
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(str(seen["url"]).endswith(OCO_PATH))
        self.assertNotIn("api.binance.com", str(seen["url"]))
        self.assertEqual(seen["body"], signed.encode("utf-8"))
        verified, verify_error = native_oco_verified(payload)
        self.assertIsNone(verify_error)
        assert verified is not None
        self.assertEqual(verified["order_list_id"], 0)
        self.assertEqual(verified["above_order_id"], 8)
        self.assertEqual(verified["below_order_id"], 9)

    def test_single_leg_and_bad_list_id_are_not_protection(self) -> None:
        for order_type in ("MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"):
            verified, reason = native_oco_verified(
                {"symbol": "BNBUSDT", "side": "SELL", "type": order_type, "orderId": 5, "status": "NEW"}
            )
            self.assertIsNone(verified)
            self.assertEqual(reason, "mcp_order_not_oco")
        verified, reason = native_oco_verified(_working(list_id=-1))
        self.assertIsNone(verified)
        self.assertEqual(reason, "native_oco_unverified")
        verified, reason = native_oco_verified(_working(above=11, below=11))
        self.assertIsNone(verified)
        self.assertEqual(reason, "native_oco_unverified")
        broker = LiveMCPBroker()
        broker._catalog = frozenset({"spot.newOrder"})  # catalog read, no invoke
        self.assertFalse(broker.availability("protection_tool"))
        self.assertFalse(broker.availability("oco"))


class ConsumerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _adapter(self, client: _Client, **extra: Any) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "absent-kill",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected=_EXPECTED,
            **extra,
        )

    def test_verified_oco_allows_the_consumer_and_a_failure_blocks_the_next_buy(self) -> None:
        report = acceptance_report()
        self.assertFalse(report["live_ready"])
        self.assertTrue(report["rest_book_bindable_without_uid"])
        self.assertNotIn("rest_account_uid_not_proven", report["blockers"])
        self.assertNotIn("canonical_fet_close_is_worker_owned", report["blockers"])
        self.assertIn("mcp_connector_has_no_order_list_oco", report["blockers"])
        client = _Client()
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, _env(self.root)):
            covered = adapter.execute(_buy())
        self.assertTrue(covered["consumer_allowed"], covered)
        self.assertEqual(covered["executed"], 1)
        self.assertEqual(covered["arm_position"]["status"], "oco_working")
        self.assertEqual(covered["oco_fields"]["oco_order_list_id"], "42")
        self.assertEqual(covered["oco_fields"]["oco_above_order_id"], "11")
        self.assertEqual(covered["oco_fields"]["oco_below_order_id"], "10")
        oco = parse_qs(next(call[1] for call in client.calls if call[0] == "new_oco"))
        self.assertEqual(oco["belowType"], ["STOP_LOSS_LIMIT"])
        self.assertEqual(oco["aboveType"], ["LIMIT_MAKER"])
        self.assertEqual(len([call for call in client.calls if call[0] == "new_order"]), 1)

        failed_client = _Client(BinanceRequestError(400))
        failed = self._adapter(failed_client)
        with mock.patch.dict(os.environ, _env(self.root)):
            naked = failed.execute(_buy(147842))
        self.assertFalse(naked["consumer_allowed"])
        self.assertEqual(naked["executed"], 0)
        self.assertEqual(naked["arm_position"]["status"], "oco_failed")
        self.assertTrue(str(naked["error"]).startswith("oco_rejected:"))
        failed.buy_blocks = [naked["arm_position"]]
        with mock.patch.dict(os.environ, _env(self.root)):
            again = failed.execute(_buy(147843))
        self.assertEqual(again["error"], "oco_failed_blocks_buy")
        self.assertEqual(
            [call[0] for call in failed_client.calls if call[0] == "new_order"],
            ["new_order"],
        )

    def test_fill_average_is_the_ledger_entry_and_signal_stays_on_the_plan(self) -> None:
        """Signal 48.08, exchange average 48.02. The new row stores 48.02.

        An older row is not rewritten. The signal remains on the exit plan.
        """
        class _AverageFill(_Client):
            def ticker_price(self, symbol: str) -> dict[str, Any]:
                self.calls.append(("ticker_price", symbol))
                return {"symbol": symbol, "price": "48.05"}

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_order", signed_query, api_key))
                return {
                    "orderId": "9001",
                    "status": "FILLED",
                    "executedQty": "1",
                    "cummulativeQuoteQty": "48.02",
                    "symbol": "BNBUSDT",
                }

        client = _AverageFill()
        sig = _buy(9002)
        sig["extras"] = dict(sig["extras"])
        sig["extras"]["price_usd"] = "48.08"
        sig["extras"]["stop_price"] = "47"
        sig["extras"]["take_profit_price"] = "49.5"
        sig["extras"]["quote_order_qty"] = "48.02"
        adapter = self._adapter(client)
        ledger = ExecutionLedger(self.root / "book.db")
        prior = ledger.arm_binance_spot_position(
            {
                "decision_log_id": 1,
                "symbol": "DASHUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "oco_filled",
                "quantity": "1",
                "stop_price": "47",
                "take_profit_price": "49.5",
                "entry_price": "48.08",
                "entry_order_id": "1",
                "protection": "exchange_oco",
            }
        )
        with mock.patch.dict(os.environ, _env(self.root)):
            result = adapter.execute(sig)
        self.assertEqual(result["executed"], 1, result)
        self.assertEqual(result["exit_plan"]["entry_price"], "48.08")
        self.assertEqual(result["arm_position"]["signal_entry_price"], "48.08")
        self.assertEqual(result["arm_position"]["entry_price"], "48.02")
        spec = held_position_spec(sig, result)
        self.assertIsNotNone(spec)
        assert spec is not None
        armed = ledger.arm_binance_spot_position(spec)
        self.assertEqual(armed["entry_price"], "48.02")
        self.assertEqual(armed["decision_log_id"], 9002)
        kept = ledger.binance_spot_position_by_decision(int(prior["decision_log_id"]))
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual(kept["entry_price"], "48.08")
        self.assertEqual(kept["status"], "oco_filled")

    def test_real_fill_keeps_signal_band_and_current_oco_names(self) -> None:
        """executedQty 1 and cumQuote 100 average to 100. Signal 110/95 is not rewritten.

        The 0.3% reference stop limit is 94.70. The live list uses the existing
        one-tick buffer, 94.99 on a 0.01 tick, and does not copy the reference.
        Legacy price/stopPrice/stopLimitPrice names are not the OCO contract.
        """
        reference = reference_protection_prices("100")
        self.assertEqual(reference["take_profit_price"], "110")
        self.assertEqual(reference["stop_price"], "95")
        self.assertEqual(Decimal(reference["stop_limit_price"]), Decimal("94.70"))

        class _Fill(_Client):
            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_order", signed_query, api_key))
                return {
                    "orderId": "77",
                    "status": "FILLED",
                    "executedQty": "1",
                    "cummulativeQuoteQty": "100",
                    "price": "90",
                    "symbol": "BNBUSDT",
                }

        client = _Fill()
        sig = _buy(9100)
        sig["extras"] = dict(sig["extras"])
        sig["extras"]["price_usd"] = "100"
        sig["extras"]["stop_price"] = "95"
        sig["extras"]["take_profit_price"] = "110"
        sig["extras"]["quote_order_qty"] = "100"
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, _env(self.root)):
            covered = adapter.execute(sig)
        self.assertEqual(covered["executed"], 1, covered)
        self.assertTrue(covered["consumer_allowed"])
        self.assertEqual(covered["arm_position"]["entry_price"], "100")
        self.assertEqual(covered["exit_plan"]["stop_price"], "95")
        self.assertEqual(covered["exit_plan"]["take_profit_price"], "110")
        oco = parse_qs(next(call[1] for call in client.calls if call[0] == "new_oco"))
        self.assertEqual(oco["aboveType"], ["LIMIT_MAKER"])
        self.assertEqual(oco["abovePrice"], ["110"])
        self.assertEqual(oco["belowType"], ["STOP_LOSS_LIMIT"])
        self.assertEqual(oco["belowStopPrice"], ["95"])
        self.assertEqual(oco["belowPrice"], ["94.99"])
        self.assertNotEqual(oco["belowPrice"], [reference["stop_limit_price"]])
        self.assertEqual(oco["belowTimeInForce"], ["GTC"])
        self.assertNotIn("aboveTimeInForce", oco)
        for legacy in ("price", "stopPrice", "stopLimitPrice"):
            self.assertNotIn(legacy, oco)
        self.assertEqual(oco["orderListId"] if "orderListId" in oco else ["sent-by-response"], ["sent-by-response"])
        self.assertEqual(covered["oco_fields"]["oco_order_list_id"], "42")

        class _Naked(_Fill):
            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("new_oco", signed_query, api_key))
                raise BinanceRequestError(400, code=-1013, message="Filter failure")

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append(("get_order_list", signed_query, api_key))
                raise BinanceRequestError(500, message="ocorequery")

        naked_client = _Naked()
        naked = self._adapter(naked_client)
        failed_sig = _buy(9101)
        failed_sig["extras"] = dict(sig["extras"])
        with mock.patch.dict(os.environ, _env(self.root)):
            missed = naked.execute(failed_sig)
        self.assertEqual(missed["executed"], 0)
        self.assertFalse(missed["consumer_allowed"])
        self.assertEqual(missed["filled_qty"], "1")
        self.assertEqual(missed["arm_position"]["entry_price"], "100")
        self.assertEqual(missed["arm_position"]["status"], "oco_failed")
        self.assertTrue(str(missed["error"]).startswith("oco_rejected:"))
        self.assertEqual(len([call for call in naked_client.calls if call[0] == "new_order"]), 1)
        self.assertEqual(len([call for call in naked_client.calls if call[0] == "new_oco"]), 1)

    def test_kill_blocks_only_the_new_buy(self) -> None:
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
            blocked = adapter.execute(_buy())
            sold = adapter.exit_held_position(
                {
                    "id": 1,
                    "decision_log_id": 147841,
                    "symbol": "BNBUSDT",
                    "status": "open",
                    "quantity": "1.239",
                    "stop_price": "99",
                    "take_profit_price": "110",
                },
                "98",
            )
        self.assertTrue(str(blocked["error"]).startswith("kill_switch:"))
        self.assertEqual(blocked["executed"], 0)
        self.assertTrue(sold["submitted"], sold)
        self.assertEqual(parse_qs(client.calls[-1][1])["side"], ["SELL"])
        env["BINANCE_SPOT_BLOCK_SELL_ON_KILL"] = "1"
        with mock.patch.dict(os.environ, env):
            halted = adapter.exit_held_position(
                {
                    "id": 1,
                    "decision_log_id": 147841,
                    "symbol": "BNBUSDT",
                    "status": "open",
                    "quantity": "1.239",
                    "stop_price": "99",
                    "take_profit_price": "110",
                },
                "98",
            )
        self.assertTrue(str(halted["error"]).startswith("kill_switch_block_sell:"))
        self.assertFalse(halted["submitted"])

    def test_accounts_stay_split_and_cantrade_is_not_a_uid(self) -> None:
        self.assertEqual(account_book_block(None, None), "account_book_unselected")
        self.assertEqual(
            account_book_block(
                {"canTrade": True, "balances": [{"asset": "FET", "free": "0"}]},
                REST_BOOK,
                _EXPECTED,
            ),
            "rest_identity_incomplete",
        )
        mcp_shaped = dict(_BOUND)
        mcp_shaped["uid"] = MCP_POSITION_UID
        self.assertEqual(
            account_book_block(mcp_shaped, REST_BOOK, _EXPECTED),
            "account_book_mismatch",
        )
        self.assertEqual(
            account_book_block({"uid": "9000000001"}, MCP_BOOK),
            "account_book_mismatch",
        )
        self.assertEqual(
            account_book_block({"uid": MCP_POSITION_UID}, MCP_BOOK),
            "mcp_has_no_native_oco",
        )
        bound, error = bind_rest_book(_BOUND, _EXPECTED)
        self.assertIsNone(error)
        assert bound is not None
        self.assertIsNone(bound["uid"])
        self.assertFalse(bound["same_as_mcp_uid"])
        self.assertEqual(bound["usdt_free"], "1000")
        self.assertIsNone(account_book_block(_BOUND, REST_BOOK, _EXPECTED))
        listed = classify_sub_account_list(
            {"subAccounts": [{"subUserId": int(MCP_POSITION_UID), "email": "agentic"}]}
        )
        self.assertEqual(listed["role"], "master")
        self.assertTrue(listed["lists_mcp_uid"])
        self.assertEqual(
            account_book_block(listed, REST_BOOK, _EXPECTED),
            "rest_identity_incomplete",
        )
        client = _Client()
        adapter = BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "absent-kill",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            rest_expected=_EXPECTED,
        )
        env = _env(self.root)
        env["BINANCE_SPOT_ACCOUNT_UID"] = MCP_POSITION_UID
        env["BINANCE_SPOT_PRODUCTION_BOOK"] = REST_BOOK
        env["BINANCE_SPOT_REST_USDT_FREE"] = "1000"
        with mock.patch.dict(os.environ, env):
            refused = adapter.execute(_buy())
        self.assertEqual(refused["error"], "account_snapshot_required")
        self.assertEqual(client.calls, [])
        ready = BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "absent-kill",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected=_EXPECTED,
        )
        with mock.patch.dict(os.environ, env):
            opened = ready.execute(_buy(147850))
        self.assertTrue(opened["consumer_allowed"], opened)
        self.assertIsNone(opened.get("account_uid"))
        self.assertEqual(replay_block(FROZEN_DECISION_ID, None), "historical_client_replay")
        self.assertEqual(replay_block(147999, FROZEN_CLIENT_ID), "historical_client_replay")
        self.assertEqual(
            buy_exposure_block(
                [],
                [{"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "status": "held_naked", "symbol": "FETUSDT"}],
            ),
            "naked_holds_new_buys",
        )
        self.assertIsNone(
            buy_exposure_block(
                [],
                [
                    {
                        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                        "status": "closed",
                        "symbol": "FETUSDT",
                        "entry_order_id": "3735698106",
                        "decision_log_id": FROZEN_DECISION_ID,
                    }
                ],
            )
        )
        scope = replay_scope()
        self.assertEqual(scope["decisions_above_floor_in_repo"], [])
        self.assertIsNone(scope["execution_log_rows_in_repo"])
        self.assertEqual(scope["operator_reported"]["execution_log_rows"], 20)
        self.assertEqual(scope["operator_reported"]["cursor"], 147840)
        self.assertEqual(scope["operator_reported"]["canonical_status"], "closed")

    def test_startup_does_not_replay_the_historical_cursor(self) -> None:
        db = self.root / "decision.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE decision_log (
                id INTEGER PRIMARY KEY,
                ts TEXT, strategy_id TEXT, action TEXT, chain_id TEXT,
                token_address TEXT, size_pct REAL, reason TEXT, risk_passed INTEGER,
                mode TEXT, raw_json TEXT, purpose TEXT
            )
            """
        )
        # 147000 is a synthetic id below the floor, not execution_log row 20.
        for decision_id in (FROZEN_DECISION_ID, 147000, 147840, 147841):
            conn.execute(
                """
                INSERT INTO decision_log
                    (id, ts, strategy_id, action, chain_id, token_address, size_pct,
                     reason, risk_passed, mode, purpose, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    "2026-11-05T00:00:00Z",
                    "tech_boll_fade_v1",
                    "buy",
                    "binance",
                    "BNBUSDT",
                    0.05,
                    "old",
                    1,
                    "live_stub",
                    PURPOSE_LIVE_BNB_BOLL_FADE,
                    json.dumps({"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "pair_address": "BNB/USDT"}),
                ),
            )
        conn.commit()
        conn.close()
        client = _Client()
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, _env(self.root)):
            with ExecutionGateway(
                decision_db=db,
                ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                binance_spot=adapter,
            ) as gw:
                stats = gw.process_pending(since_id=0)
                frozen = adapter.execute(_buy(FROZEN_DECISION_ID))
        seen = [item["decision_log_id"] for item in stats.receipts]
        self.assertEqual(seen, [147841])
        self.assertNotIn(FROZEN_DECISION_ID, seen)
        self.assertNotIn(147000, seen)
        self.assertNotIn(147840, seen)
        self.assertEqual(frozen["error"], "historical_client_replay")
        self.assertFalse(any("bnbfade-147831" in call[1] for call in client.calls if len(call) > 1))
        gated = dispatch_live_bnb(
            _buy(FROZEN_DECISION_ID),
            {"canonical_positions": [], "spent_orders": [], "open_orders": []},
            None,
        )
        self.assertEqual(gated["reason"], "historical_client_replay")
        self.assertFalse(gated["oco_placed"])
        self.assertTrue(all(item.get("release") is False for item in gated["parent_queue"]))

    def test_closed_fet_exit_does_not_move_the_log_or_block_a_new_buy(self) -> None:
        ledger = ExecutionLedger(self.root / "execution_ledger_live_bnb_boll_fade.db")
        ledger.set_last_processed_id(147840)
        import_verified_fet_fill(ledger)
        self.assertEqual(ledger.count(), 0)
        book = ledger.close_canonical_exit(dict(VERIFIED_FET_EXIT))
        again = ledger.close_canonical_exit(dict(VERIFIED_FET_EXIT))
        self.assertEqual(ledger.get_last_processed_id(), 147840)
        self.assertEqual(ledger.count(), 0)
        self.assertEqual(
            int(ledger.conn.execute("SELECT COUNT(*) AS n FROM order_events").fetchone()["n"]),
            0,
        )
        self.assertEqual(book["canonical_positions"][0]["status"], "closed")
        self.assertEqual(again["canonical_positions"][0]["status"], "closed")
        self.assertEqual(
            int(
                ledger.conn.execute(
                    "SELECT COUNT(*) AS n FROM canonical_position_exits"
                ).fetchone()["n"]
            ),
            1,
        )
        self.assertIsNone(buy_exposure_block([], book["canonical_positions"]))
        self.assertEqual(str(VERIFIED_FET_FILL["client_order_id"]), FROZEN_CLIENT_ID)
        ledger.close()


def _protected(status: str, symbol: str, **extra: Any) -> dict[str, Any]:
    row = {
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "symbol": symbol,
        "status": status,
        "protection": "exchange_oco",
        "entry_order_id": "9",
        "oco_order_list_id": "3",
        "oco_above_order_id": "4",
        "oco_below_order_id": "5",
    }
    row.update(extra)
    return row


class MultiSymbolArmTests(unittest.TestCase):
    """一臂多仓: several symbols on one arm. Not 一币多仓 (same-symbol adds)."""

    def test_other_symbol_is_allowed_and_same_symbol_still_blocks(self) -> None:
        held = _protected("oco_working", "CFGUSDT")
        fade = PURPOSE_LIVE_BNB_BOLL_FADE
        self.assertIsNone(buy_exposure_block([held], [], purpose=fade, symbol="BNBUSDT"))
        self.assertIsNone(buy_exposure_block([held], [], purpose=fade, symbol="ETHUSDT"))
        self.assertEqual(
            buy_exposure_block([held], [], purpose=fade, symbol="CFGUSDT"),
            "position_open_blocks_buy",
        )
        self.assertEqual(
            buy_exposure_block([held], [], purpose=fade, symbol="cfgusdt"),
            "position_open_blocks_buy",
        )
        # Callers that omit the buy symbol stay fail-closed.
        self.assertEqual(
            buy_exposure_block([held], [], purpose=fade),
            "position_open_blocks_buy",
        )
        self.assertEqual(
            buy_exposure_block([held], [], purpose=fade, symbol="  "),
            "position_open_blocks_buy",
        )
        # A held row with no symbol cannot be proved to be a different coin.
        blank = _protected("oco_working", "CFGUSDT")
        blank["symbol"] = ""
        self.assertEqual(
            buy_exposure_block([blank], [], purpose=fade, symbol="BNBUSDT"),
            "position_open_blocks_buy",
        )
        for status in ("open", "exit_submitted"):
            row = _protected(status, "CFGUSDT")
            self.assertIsNone(
                buy_exposure_block([row], [], purpose=fade, symbol="BNBUSDT"),
                status,
            )
            self.assertEqual(
                buy_exposure_block([row], [], purpose=fade, symbol="CFGUSDT"),
                "position_open_blocks_buy",
                status,
            )
        # One matching symbol among other healthy opens still blocks.
        self.assertEqual(
            buy_exposure_block(
                [_protected("oco_working", "ETHUSDT"), _protected("oco_working", "BNBUSDT")],
                [],
                purpose=fade,
                symbol="BNBUSDT",
            ),
            "position_open_blocks_buy",
        )
        self.assertIsNone(
            buy_exposure_block(
                [_protected("oco_working", "ETHUSDT"), _protected("oco_working", "CFGUSDT")],
                [],
                purpose=fade,
                symbol="BNBUSDT",
            )
        )
        # Same-arm helper still ignores the other arm. The cross-arm gate does not.
        self.assertIsNone(
            buy_exposure_block(
                [held],
                [],
                purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC,
                symbol="CFGUSDT",
            )
        )

    def test_flag_off_restores_one_open_position_per_arm(self) -> None:
        held = _protected("oco_working", "CFGUSDT")
        with mock.patch.dict(os.environ, {"BINANCE_SPOT_ALLOW_MULTI_SYMBOL": "0"}):
            self.assertEqual(
                buy_exposure_block(
                    [held],
                    [],
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    symbol="BNBUSDT",
                ),
                "position_open_blocks_buy",
            )

    def test_unsafe_rows_still_block_another_symbol(self) -> None:
        fade = PURPOSE_LIVE_BNB_BOLL_FADE
        naked = buy_exposure_block(
            [],
            [{"purpose": fade, "status": "held_naked", "symbol": "CFGUSDT"}],
            purpose=fade,
            symbol="BNBUSDT",
        )
        self.assertEqual(naked, "naked_holds_new_buys")
        unbound = buy_exposure_block(
            [],
            [{"purpose": fade, "status": "open", "symbol": "CFGUSDT"}],
            purpose=fade,
            symbol="BNBUSDT",
        )
        self.assertEqual(unbound, "unbound_position")
        cases = [
            (_protected("qty_unconfirmed", "CFGUSDT"), "parent_fill_incomplete"),
            (_protected("oco_failed", "CFGUSDT"), "oco_failed_blocks_buy"),
            (_protected("oco_unknown", "CFGUSDT"), "oco_failed_blocks_buy"),
            (
                _protected("open", "CFGUSDT", protection="pending"),
                "unprotected_position_blocks_buy",
            ),
            (
                _protected("open", "CFGUSDT", oco_below_order_id=None),
                "unprotected_position_blocks_buy",
            ),
            (_protected("pending", "CFGUSDT"), "unprotected_position_blocks_buy"),
        ]
        for row, error in cases:
            self.assertEqual(
                buy_exposure_block([row], [], purpose=fade, symbol="BNBUSDT"),
                error,
                row["status"],
            )
            self.assertEqual(
                buy_exposure_block([row], [], purpose=fade, symbol="CFGUSDT"),
                error,
                row["status"],
            )


if __name__ == "__main__":
    unittest.main()
