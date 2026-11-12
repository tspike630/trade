"""Resting LIMIT preflight versus the live-mark MARKET band. No live HTTP.

Signal 148630 is the geometry: a neutral bid below the target while the fresh
mark is still above that target. The mark used here is a fixture. The
13:40:28 production mark was not persisted and is not reconstructed.

    python3 -m unittest execution.tests.test_resting_limit_oco_preflight
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

from execution.executors.binance_spot import (  # noqa: E402
    CLASSIC_EXIT_SHELL,
    BinanceRequestError,
    BinanceSpotAdapter,
)
from execution.executors.binance_spot_gate import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    REST_BOOK,
)
from execution.executors.binance_spot_protect import (  # noqa: E402
    PROTECTION_AUTO_FLATTEN,
    buy_entry_oco_preflight,
)
from execution.ledger import (  # noqa: E402
    ExecutionLedger,
    bind_registry_membership,
    initialize_spot_ledger_binding,
)
from execution.gateway import (  # noqa: E402
    _binance_public_result,
    ExecutionGateway,
)

# Fixture mark. Not the missing 13:40:28 production last.
_MARK_ABOVE_TP = "0.14400"
_LIMIT = "0.1320624"
_STOP = "0.1306188"
_TP = "0.135912"
_PRICE_USD = "0.13898"
_INIT_FILTERS = [
    {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "90000000", "stepSize": "1"},
    {"filterType": "MARKET_LOT_SIZE", "minQty": "1", "maxQty": "90000000", "stepSize": "1"},
    {"filterType": "PRICE_FILTER", "minPrice": "0.00001000", "maxPrice": "100000", "tickSize": "0.00001000"},
    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
]
_BNB_FILTERS = [
    {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "900000", "stepSize": "0.001"},
    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "900000", "stepSize": "0.001"},
    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
]
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
_API_KEY = "unit-test-spot-key"
_API_SECRET = "unit-test-spot-secret"


def _info(symbol: str, filters: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "filters": filters,
    }


def _working_oco() -> dict[str, Any]:
    return {
        "orderListId": 42,
        "listOrderStatus": "EXECUTING",
        "listStatusType": "EXEC_STARTED",
        "orders": [{"orderId": 11}, {"orderId": 10}],
        "orderReports": [
            {"orderId": 11, "type": "LIMIT_MAKER", "status": "NEW", "executedQty": "0"},
            {"orderId": 10, "type": "STOP_LOSS_LIMIT", "status": "NEW", "executedQty": "0"},
        ],
    }


class _Book:
    """Scripted public market. ``marks`` is consumed one ticker read at a time."""

    def __init__(
        self,
        *,
        symbol: str,
        filters: list[dict[str, Any]],
        marks: list[str],
        ack: dict[str, Any] | None = None,
        oco_error: Exception | None = None,
        ticker_error: Exception | None = None,
        asks: list[list[str]] | None = None,
        book_error: Exception | None = None,
    ) -> None:
        self.symbol = symbol
        self.filters = filters
        self.marks = list(marks)
        self.ack = ack or {
            "orderId": "810",
            "status": "NEW",
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
            "symbol": symbol,
        }
        self.oco_error = oco_error
        self.ticker_error = ticker_error
        self.asks = asks if asks is not None else [["0.15000", "1000"]]
        self.book_error = book_error
        self.calls: list[tuple[Any, ...]] = []
        self.lookup_count = 0
        self.canceled = False
        self.partial_qty = "100"
        self.partial_quote = "13.206"

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _info(symbol, self.filters)

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        if self.ticker_error is not None and len(self.marks) == 0:
            raise self.ticker_error
        if not self.marks:
            raise AssertionError("ticker_price called with no scripted mark")
        price = self.marks.pop(0) if len(self.marks) > 1 else self.marks[0]
        if self.ticker_error is not None and self.calls.count(("ticker_price", symbol)) > 1:
            raise self.ticker_error
        return {"symbol": symbol, "price": price}

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        self.calls.append(("order_book", symbol))
        if self.book_error is not None:
            raise self.book_error
        ask_px = Decimal(self.asks[0][0])
        bid_px = ask_px - Decimal("0.00001")
        return {"bids": [[format(bid_px, "f"), "1000"]], "asks": self.asks}

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        form = parse_qs(signed_query)
        if form.get("side") == ["SELL"]:
            qty = form.get("quantity", ["0"])[0]
            return {"orderId": "7701", "status": "FILLED", "executedQty": qty, "symbol": self.symbol}
        return dict(self.ack, symbol=self.symbol)

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        if self.oco_error is not None:
            raise self.oco_error
        return _working_oco()

    def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order", signed_query, api_key))
        self.lookup_count += 1
        if "origClientOrderId" in parse_qs(signed_query):
            raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")
        if not self.canceled:
            return {
                "orderId": "810",
                "symbol": self.symbol,
                "status": "PARTIALLY_FILLED",
                "executedQty": self.partial_qty,
                "cummulativeQuoteQty": self.partial_quote,
                "price": "0.13206",
            }
        return {
            "orderId": "810",
            "symbol": self.symbol,
            "status": "CANCELED",
            "executedQty": self.partial_qty,
            "cummulativeQuoteQty": self.partial_quote,
            "price": "0.13206",
        }

    def cancel_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("cancel_order", signed_query, api_key))
        self.canceled = True
        return {"orderId": "810", "status": "CANCELED", "executedQty": self.partial_qty}

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order_list", signed_query, api_key))
        if self.oco_error is not None:
            raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")
        return _working_oco()


def _names(client: _Book, kind: str) -> list[str]:
    return [call[0] for call in client.calls if call[0] == kind]


def _form(client: _Book, kind: str, *, side: str | None = None) -> dict[str, list[str]]:
    for call in client.calls:
        if call[0] != kind:
            continue
        form = parse_qs(call[1])
        if side is None or form.get("side") == [side]:
            return form
    raise AssertionError(f"no {kind} side={side}")


class PreflightGeometryTests(unittest.TestCase):
    def test_exact_signal_geometry_uses_the_planned_entry(self) -> None:
        diag, err = buy_entry_oco_preflight(
            order_type="LIMIT",
            limit_price=_LIMIT,
            stop_price=_STOP,
            take_profit_price=_TP,
            mark=_MARK_ABOVE_TP,
            mark_source="rest_fallback",
            filters=_INIT_FILTERS,
            resting_ceiling="0.15000",
            ceiling_source="rest_book",
        )
        self.assertIsNone(err)
        self.assertEqual(diag["reference"], "planned_entry")
        self.assertEqual(diag["ceiling_price"], "0.15")
        self.assertEqual(diag["ceiling_source"], "rest_book")
        self.assertEqual(diag["reference_price"], "0.13206")
        self.assertEqual(diag["aligned_limit"], "0.13206")
        self.assertEqual(diag["aligned_stop"], "0.13061")
        self.assertEqual(diag["aligned_take_profit"], "0.13591")
        self.assertEqual(diag["mark"], "0.144")
        self.assertFalse(diag["mark_inside_band"])
        self.assertEqual(diag["planned_limit"], "0.1320624")

    def test_market_at_and_beyond_the_band_refuses_without_moving_prices(self) -> None:
        inside, inside_err = buy_entry_oco_preflight(
            order_type="MARKET",
            limit_price=None,
            stop_price="95",
            take_profit_price="110",
            mark="102",
            mark_source="rest_fallback",
            filters=_BNB_FILTERS,
        )
        self.assertIsNone(inside_err)
        self.assertEqual(inside["reference"], "live_mark")
        self.assertTrue(inside["mark_inside_band"])
        for mark in ("95", "110", "94.99", "110.01"):
            diag, err = buy_entry_oco_preflight(
                order_type="MARKET",
                limit_price=None,
                stop_price="95",
                take_profit_price="110",
                mark=mark,
                mark_source="rest_fallback",
                filters=_BNB_FILTERS,
            )
            self.assertEqual(err, "oco_price_restriction", mark)
            self.assertEqual(diag["aligned_stop"], "95")
            self.assertEqual(diag["aligned_take_profit"], "110")
            self.assertEqual(diag["reference"], "live_mark")

    def test_missing_mark_and_marketable_limit_do_not_use_the_order_type(self) -> None:
        missing, missing_err = buy_entry_oco_preflight(
            order_type="LIMIT",
            limit_price=_LIMIT,
            stop_price=_STOP,
            take_profit_price=_TP,
            mark=None,
            mark_source=None,
            filters=_INIT_FILTERS,
        )
        self.assertEqual(missing_err, "mark_unavailable")
        self.assertIsNone(missing["reference"])
        no_ask, no_ask_err = buy_entry_oco_preflight(
            order_type="LIMIT",
            limit_price=_LIMIT,
            stop_price=_STOP,
            take_profit_price=_TP,
            mark=_MARK_ABOVE_TP,
            mark_source="rest_fallback",
            filters=_INIT_FILTERS,
        )
        self.assertEqual(no_ask_err, "ask_unavailable")
        crossed, crossed_err = buy_entry_oco_preflight(
            order_type="LIMIT",
            limit_price=_LIMIT,
            stop_price=_STOP,
            take_profit_price=_TP,
            mark=_MARK_ABOVE_TP,
            mark_source="rest_fallback",
            filters=_INIT_FILTERS,
            resting_ceiling="0.13206",
            ceiling_source="rest_book",
        )
        self.assertEqual(crossed_err, "limit_would_cross")
        self.assertEqual(crossed["reference"], "planned_entry")
        self.assertEqual(crossed["ceiling_price"], "0.13206")
        self.assertGreater(Decimal(crossed["mark"]), Decimal(crossed["aligned_limit"]))

    def test_tick_alignment_that_leaves_the_band_does_not_rewrite_the_target(self) -> None:
        diag, err = buy_entry_oco_preflight(
            order_type="LIMIT",
            limit_price="0.1359109",
            stop_price=_STOP,
            take_profit_price=_TP,
            mark=_MARK_ABOVE_TP,
            mark_source="rest_fallback",
            filters=_INIT_FILTERS,
            resting_ceiling="0.15000",
            ceiling_source="rest_book",
        )
        self.assertEqual(err, "oco_price_restriction")
        self.assertEqual(diag["aligned_limit"], "0.13591")
        self.assertEqual(diag["aligned_take_profit"], "0.13591")


class RestingLimitExecutionTests(unittest.TestCase):
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

    def _env(self) -> dict[str, str]:
        return {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
            "LIVE_EXCHANGE_API_KEY": "",
            "LIVE_EXCHANGE_API_SECRET": "",
            "BINANCE_SPOT_POST_ONLY_LIMIT": "0",
            "BINANCE_SPOT_REANCHOR_OCO_ON_FILL": "0",
            "BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL": "0",
        }

    def _adapter(self, client: _Book, **kwargs: Any) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "absent-kill",
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected=_EXPECTED,
            post_only_limit=False,
            **kwargs,
        )

    def _limit_signal(self, **extra: Any) -> dict[str, Any]:
        extras = {
            "binance_symbol": "INITUSDT",
            "order_type": "LIMIT",
            "limit_price": _LIMIT,
            "quantity": "295",
            "price_usd": _PRICE_USD,
            "stop_price": _STOP,
            "take_profit_price": _TP,
            "entry_reason": "neutral_limit",
        }
        extras.update(extra)
        return {
            "decision_log_id": 148630,
            "strategy_id": "tech_boll_fade_v1",
            "action": "buy",
            "chain_id": "binance",
            "token_address": "INITUSDT",
            "pair_address": "INIT/USDT",
            "size_pct": 0.066,
            "mode": "live_stub",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "extras": extras,
        }

    def test_above_band_rest_is_refused_while_flatten_is_off(self) -> None:
        client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertEqual(result["error"], "resting_protection_unconfigured", result)
        self.assertFalse(result["submitted"])
        self.assertIsNone(result.get("arm_position"))
        self.assertEqual(_names(client, "new_order"), [])
        self.assertEqual(_names(client, "new_oco"), [])
        pre = result["oco_preflight"]
        self.assertEqual(pre["reference"], "planned_entry")
        self.assertEqual(pre["aligned_limit"], "0.13206")
        self.assertEqual(pre["ceiling_source"], "rest_book")
        self.assertEqual(pre["ceiling_price"], "0.15")
        self.assertFalse(pre["mark_inside_band"])
        self.assertEqual(result["exit_plan"]["take_profit_price"], "0.135912")
        public = _binance_public_result(result)
        self.assertEqual(public["oco_preflight"]["aligned_stop"], "0.13061")
        self.assertNotIn(_API_KEY, json.dumps(result))

    def test_reanchor_alone_does_not_admit_an_above_band_rest(self) -> None:
        client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        adapter = self._adapter(client, reanchor_oco_on_fill=True, auto_flatten_on_oco_fail=False)
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertEqual(result["error"], "resting_protection_unconfigured")
        self.assertEqual(_names(client, "new_order"), [])

    def test_configured_flatten_rests_the_limit_without_an_oco(self) -> None:
        client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        adapter = self._adapter(client, auto_flatten_on_oco_fail=True)
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertEqual(result["error"], "maker_entry_resting", result)
        buy = _form(client, "new_order", side="BUY")
        self.assertEqual(buy["type"], ["LIMIT"])
        self.assertEqual(buy["price"], ["0.13206"])
        self.assertEqual(_names(client, "new_oco"), [])
        self.assertEqual(result["oco_preflight"]["ceiling_source"], "rest_book")

    def test_reviewer_in_band_rest_is_not_submitted_under_deployed_defaults(self) -> None:
        """f88a645 admitted this bid, then reconcile at 0.15000 was oco_failed.

        That receipt had no new_oco and no SELL. Deployed defaults must not
        submit the order. oco_failed is not the success condition.
        """
        cases = (
            ("in_band_then_through_tp", "0.13300", "0.13350"),
            ("in_band_then_through_sl", "0.13300", "0.13350"),
            ("partial_fill", "0.13300", "0.13350"),
            ("full_fill", "0.13300", "0.13350"),
            ("immediate_fill", "0.13300", "0.13350"),
            ("missing_postfill_mark", "0.13300", "0.13350"),
            ("oco_rejection", "0.13300", "0.13350"),
            ("above_band", _MARK_ABOVE_TP, "0.15000"),
        )
        for name, mark, ask in cases:
            client = _Book(
                symbol="INITUSDT",
                filters=_INIT_FILTERS,
                marks=[mark],
                asks=[[ask, "1000"]],
                ack={
                    "orderId": "810",
                    "status": "FILLED",
                    "executedQty": "295",
                    "cummulativeQuoteQty": "38.9577",
                    "symbol": "INITUSDT",
                },
            )
            adapter = self._adapter(client, reanchor_oco_on_fill=False, auto_flatten_on_oco_fail=False)
            with mock.patch.dict(os.environ, self._env()):
                result = adapter.execute(self._limit_signal())
            self.assertEqual(result["error"], "resting_protection_unconfigured", name)
            self.assertFalse(result["submitted"], name)
            self.assertIsNone(result.get("arm_position"), name)
            self.assertEqual(_names(client, "new_order"), [], name)
            self.assertEqual(_names(client, "new_oco"), [], name)
            self.assertNotIn("oco_failed", str(result.get("error")), name)
            if name == "above_band":
                self.assertFalse(result["oco_preflight"]["mark_inside_band"])
            else:
                self.assertTrue(result["oco_preflight"]["mark_inside_band"], name)
        maker = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=["0.13300"],
            asks=[["0.13350", "1000"]],
        )
        maker_adapter = self._adapter(maker)
        with mock.patch.dict(os.environ, self._env()):
            maker_result = maker_adapter.execute(self._limit_signal(order_type="LIMIT_MAKER"))
        self.assertEqual(maker_result["error"], "resting_protection_unconfigured")
        self.assertEqual(_names(maker, "new_order"), [])

    def test_configured_flatten_sells_when_an_in_band_fill_later_leaves_the_band(self) -> None:
        """The same 0.15000 reconcile sells only after the operator enables auto-flatten."""
        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=["0.13300"],
            asks=[["0.13350", "1000"]],
        )
        adapter = self._adapter(client, auto_flatten_on_oco_fail=True, reanchor_oco_on_fill=False)
        with mock.patch.dict(os.environ, self._env()):
            rested = adapter.execute(self._limit_signal())
        self.assertEqual(rested["error"], "maker_entry_resting", rested)
        arm = dict(rested["arm_position"])
        arm["id"] = 1

        def _filled(signed_query: str, *, api_key: str) -> dict[str, Any]:
            client.calls.append(("get_order", signed_query, api_key))
            if "origClientOrderId" in parse_qs(signed_query):
                raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")
            return {
                "orderId": "810",
                "symbol": "INITUSDT",
                "status": "FILLED",
                "executedQty": "295",
                "cummulativeQuoteQty": "38.9577",
            }

        client.get_order = _filled  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, self._env()):
            through_tp = adapter.reconcile_resting_entry(arm, mark="0.15000", now_ms=1_700_000_010_000)
        self.assertEqual(_names(client, "new_oco"), [])
        self.assertEqual(_form(client, "new_order", side="SELL")["side"], ["SELL"])
        self.assertEqual(_form(client, "new_order", side="SELL")["type"], ["MARKET"])
        self.assertNotEqual(through_tp.get("book"), "oco_failed")
        self.assertIn("auto_flatten", str(through_tp.get("error")))

    def test_last_above_the_limit_does_not_hide_an_ask_at_or_through_it(self) -> None:
        for ask in ("0.13206", "0.13200"):
            client = _Book(
                symbol="INITUSDT",
                filters=_INIT_FILTERS,
                marks=[_MARK_ABOVE_TP],
                asks=[[ask, "1000"]],
            )
            adapter = self._adapter(client)
            with mock.patch.dict(os.environ, self._env()):
                result = adapter.execute(self._limit_signal())
            self.assertEqual(result["error"], "limit_would_cross", ask)
            self.assertEqual(_names(client, "new_order"), [], ask)
            self.assertGreater(Decimal(result["oco_preflight"]["mark"]), Decimal("0.13206"))

    def test_stale_ws_ask_is_not_the_executable_ask(self) -> None:
        class _Stale:
            def get_ticker_price(self, symbol: str) -> dict[str, Any]:
                return {
                    "price": _MARK_ABOVE_TP,
                    "best_bid": "0.14900",
                    "best_ask": "0.15000",
                    "source": "ws_l1",
                    "ws_age_ms": 900_000,
                }

        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=[_MARK_ABOVE_TP],
            asks=[["0.13200", "1000"]],
        )
        adapter = self._adapter(client, ws_cache=_Stale())
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertEqual(result["error"], "limit_would_cross", result)
        self.assertEqual(result["oco_preflight"]["ceiling_source"], "rest_book")
        self.assertEqual(result["oco_preflight"]["ceiling_price"], "0.132")
        self.assertEqual(_names(client, "new_order"), [])

    def test_missing_ask_fails_closed(self) -> None:
        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=[_MARK_ABOVE_TP],
            book_error=TimeoutError("book down"),
        )
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertEqual(result["error"], "maker_book_unavailable")
        self.assertEqual(_names(client, "new_order"), [])
        self.assertEqual(_names(client, "new_oco"), [])

    def test_fresh_ws_ask_is_the_ceiling(self) -> None:
        class _Fresh:
            def get_ticker_price(self, symbol: str) -> dict[str, Any]:
                return {
                    "price": _MARK_ABOVE_TP,
                    "best_bid": "0.14300",
                    "best_ask": "0.14500",
                    "source": "ws_l1",
                    "ws_age_ms": 50,
                }

        client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[])
        client.order_book = lambda symbol, limit=5: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("fresh ws ask must not fall through to REST")
        )
        client.ticker_price = lambda symbol: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("fresh ws mark must not fall through to REST")
        )
        adapter = self._adapter(client, ws_cache=_Fresh())
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertEqual(result["error"], "resting_protection_unconfigured", result)
        self.assertEqual(result["oco_preflight"]["mark_source"], "ws_l1")
        self.assertEqual(result["oco_preflight"]["ceiling_source"], "ws_bbo")
        self.assertEqual(result["oco_preflight"]["ceiling_price"], "0.145")
        self.assertEqual(result["oco_preflight"]["ask_age_ms"], 50)
        self.assertEqual(_names(client, "new_order"), [])

    def test_missing_mark_refuses_a_limit_even_when_price_usd_is_present(self) -> None:
        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=[],
            ticker_error=TimeoutError("timed out"),
        )
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertEqual(result["error"], "mark_unavailable")
        self.assertFalse(result["submitted"])
        self.assertEqual(_names(client, "new_order"), [])
        self.assertEqual(_names(client, "new_oco"), [])
        self.assertIsNone(result["oco_preflight"]["mark"])

    def test_marketable_limit_is_not_sent_and_is_not_rewritten_to_market(self) -> None:
        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=["0.13400"],
            asks=[["0.13350", "1000"]],
        )
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal(limit_price="0.13350"))
        self.assertEqual(result["error"], "limit_would_cross")
        self.assertFalse(result["submitted"])
        self.assertEqual(_names(client, "new_order"), [])
        self.assertEqual(_names(client, "new_oco"), [])
        self.assertEqual(result["exit_plan"]["take_profit_price"], "0.135912")
        self.assertEqual(result["oco_preflight"]["aligned_take_profit"], "0.13591")

    def test_market_order_with_the_same_prices_still_refuses_on_the_live_mark(self) -> None:
        client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        adapter = self._adapter(client)
        signal = self._limit_signal()
        signal["extras"] = {
            "binance_symbol": "INITUSDT",
            "order_type": "MARKET",
            "quote_order_qty": "39",
            "price_usd": _LIMIT,
            "stop_price": _STOP,
            "take_profit_price": _TP,
            "entry_reason": "neutral_market",
        }
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(signal)
        self.assertEqual(result["error"], "oco_price_restriction")
        self.assertEqual(_names(client, "new_order"), [])
        self.assertEqual(_names(client, "new_oco"), [])
        self.assertEqual(result["oco_preflight"]["reference"], "live_mark")

    def test_immediate_fill_keeps_the_original_band_when_the_fresh_mark_returns(self) -> None:
        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=[_MARK_ABOVE_TP, "0.13250"],
            ack={
                "orderId": "810",
                "status": "FILLED",
                "executedQty": "295",
                "cummulativeQuoteQty": "38.9577",
                "symbol": "INITUSDT",
            },
        )
        adapter = self._adapter(client, auto_flatten_on_oco_fail=True)
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["executed"], 1)
        oco = _form(client, "new_oco")
        self.assertEqual(oco["abovePrice"], ["0.13591"])
        self.assertEqual(oco["belowStopPrice"], ["0.13061"])
        self.assertEqual(_names(client, "new_order"), ["new_order"])
        self.assertEqual(result["oco_preflight"]["fill_mark"], "0.13250")
        self.assertEqual(result["oco_preflight"]["mark"], "0.144")

    def test_immediate_fill_reanchors_when_the_fresh_mark_is_still_outside(self) -> None:
        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=[_MARK_ABOVE_TP, _MARK_ABOVE_TP],
            ack={
                "orderId": "810",
                "status": "FILLED",
                "executedQty": "295",
                "cummulativeQuoteQty": "38.9577",
                "symbol": "INITUSDT",
            },
        )
        adapter = self._adapter(
            client,
            reanchor_oco_on_fill=True,
            auto_flatten_on_oco_fail=True,
        )
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertTrue(result["ok"], result)
        oco = _form(client, "new_oco")
        self.assertGreater(Decimal(oco["abovePrice"][0]), Decimal(_MARK_ABOVE_TP))
        self.assertNotEqual(oco["abovePrice"], ["0.13591"])
        self.assertTrue(result["arm_position"]["oco_reanchored"])
        self.assertEqual(_names(client, "new_order"), ["new_order"])

    def test_missing_fill_mark_flattens_and_does_not_post_oco(self) -> None:
        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=[_MARK_ABOVE_TP],
            ticker_error=TimeoutError("timed out"),
            ack={
                "orderId": "810",
                "status": "FILLED",
                "executedQty": "295",
                "cummulativeQuoteQty": "38.9577",
                "symbol": "INITUSDT",
            },
        )
        adapter = self._adapter(client, auto_flatten_on_oco_fail=True)
        with mock.patch.dict(os.environ, self._env()):
            result = adapter.execute(self._limit_signal())
        self.assertEqual(_names(client, "new_oco"), [])
        self.assertEqual(_form(client, "new_order", side="SELL")["type"], ["MARKET"])
        self.assertIn("mark_unavailable", str(result["error"]))
        self.assertEqual(result["arm_position"]["protection"], PROTECTION_AUTO_FLATTEN)

    def test_partial_fill_cancels_the_remainder_and_protects_only_the_fill(self) -> None:
        client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        adapter = self._adapter(client, auto_flatten_on_oco_fail=True)
        with mock.patch.dict(os.environ, self._env()):
            rested = adapter.execute(self._limit_signal(quantity="200"))
        self.assertEqual(rested["error"], "maker_entry_resting")
        arm = dict(rested["arm_position"])
        arm["id"] = 1
        arm["status"] = "maker_resting"
        with mock.patch.dict(os.environ, self._env()):
            adopted = adapter.reconcile_resting_entry(
                arm,
                mark="0.13210",
                now_ms=1_700_000_010_000,
            )
        self.assertEqual(adopted["book"], "oco_working", adopted)
        self.assertEqual(_names(client, "cancel_order"), ["cancel_order"])
        oco = _form(client, "new_oco")
        self.assertEqual(oco["quantity"], ["100"])
        self.assertEqual(oco["abovePrice"], ["0.13591"])
        self.assertNotEqual(oco["quantity"], ["200"])

    def test_partial_fill_oco_failure_flattens_the_filled_qty(self) -> None:
        client = _Book(
            symbol="INITUSDT",
            filters=_INIT_FILTERS,
            marks=[_MARK_ABOVE_TP],
            oco_error=BinanceRequestError(400, code=-2010, msg="Account has insufficient balance"),
        )
        adapter = self._adapter(client, auto_flatten_on_oco_fail=True)
        with mock.patch.dict(os.environ, self._env()):
            rested = adapter.execute(self._limit_signal(quantity="200"))
        arm = dict(rested["arm_position"])
        arm["id"] = 1
        with mock.patch.dict(os.environ, self._env()):
            failed = adapter.reconcile_resting_entry(arm, mark="0.13210", now_ms=1_700_000_010_000)
        self.assertEqual(_names(client, "cancel_order"), ["cancel_order"])
        sell = _form(client, "new_order", side="SELL")
        self.assertEqual(sell["type"], ["MARKET"])
        self.assertEqual(sell["quantity"], ["100"])
        self.assertIn("oco_failed_auto_flattened", str(failed.get("error")))

    def test_post_fill_mark_through_the_stop_reanchors_or_flattens(self) -> None:
        def _arm(client: _Book) -> dict[str, Any]:
            adapter = self._adapter(client, reanchor_oco_on_fill=True, auto_flatten_on_oco_fail=True)
            with mock.patch.dict(os.environ, self._env()):
                rested = adapter.execute(self._limit_signal())
            arm = dict(rested["arm_position"])
            arm["id"] = 1
            return adapter, arm

        re_client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        re_adapter, re_arm = _arm(re_client)
        def _filled_entry(signed_query: str, *, api_key: str) -> dict[str, Any]:
            re_client.calls.append(("get_order", signed_query, api_key))
            if "origClientOrderId" in parse_qs(signed_query):
                raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")
            return {
                "orderId": "810",
                "symbol": "INITUSDT",
                "status": "FILLED",
                "executedQty": "295",
                "cummulativeQuoteQty": "38.9577",
            }

        re_client.get_order = _filled_entry  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, self._env()):
            reanchored = re_adapter.reconcile_resting_entry(
                re_arm,
                mark="0.12000",
                now_ms=1_700_000_010_000,
            )
        self.assertEqual(reanchored["book"], "oco_working", reanchored)
        oco = _form(re_client, "new_oco")
        self.assertEqual(oco["abovePrice"], ["0.13591"])
        self.assertLess(Decimal(oco["belowStopPrice"][0]), Decimal("0.12000"))

        flat_client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        flat_adapter = self._adapter(flat_client, reanchor_oco_on_fill=False, auto_flatten_on_oco_fail=True)
        with mock.patch.dict(os.environ, self._env()):
            rested = flat_adapter.execute(self._limit_signal())
        flat_arm = dict(rested["arm_position"])
        flat_arm["id"] = 1
        def _filled_entry(signed_query: str, *, api_key: str) -> dict[str, Any]:
            flat_client.calls.append(("get_order", signed_query, api_key))
            if "origClientOrderId" in parse_qs(signed_query):
                raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")
            return {
                "orderId": "810",
                "symbol": "INITUSDT",
                "status": "FILLED",
                "executedQty": "295",
                "cummulativeQuoteQty": "38.9577",
            }

        flat_client.get_order = _filled_entry  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, self._env()):
            flattened = flat_adapter.reconcile_resting_entry(
                flat_arm,
                mark="0.12000",
                now_ms=1_700_000_010_000,
            )
        self.assertEqual(_names(flat_client, "new_oco"), [])
        self.assertEqual(_form(flat_client, "new_order", side="SELL")["type"], ["MARKET"])
        self.assertIn("oco_unprotectable", str(flattened.get("error")))

        tp_client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        tp_adapter = self._adapter(tp_client, auto_flatten_on_oco_fail=True)
        with mock.patch.dict(os.environ, self._env()):
            rested = tp_adapter.execute(self._limit_signal())
        tp_arm = dict(rested["arm_position"])
        tp_arm["id"] = 1

        def _filled_tp(signed_query: str, *, api_key: str) -> dict[str, Any]:
            tp_client.calls.append(("get_order", signed_query, api_key))
            if "origClientOrderId" in parse_qs(signed_query):
                raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")
            return {
                "orderId": "810",
                "symbol": "INITUSDT",
                "status": "FILLED",
                "executedQty": "295",
                "cummulativeQuoteQty": "38.9577",
            }

        tp_client.get_order = _filled_tp  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, self._env()):
            through_tp = tp_adapter.reconcile_resting_entry(
                tp_arm,
                mark="0.15000",
                now_ms=1_700_000_010_000,
            )
        self.assertEqual(_names(tp_client, "new_oco"), [])
        self.assertEqual(_form(tp_client, "new_order", side="SELL")["type"], ["MARKET"])
        self.assertIn("oco_unprotectable", str(through_tp.get("error")))

    def test_receipt_persists_the_preflight_without_credentials(self) -> None:
        client = _Book(symbol="INITUSDT", filters=_INIT_FILTERS, marks=[_MARK_ABOVE_TP])
        adapter = self._adapter(client)
        snap = dict(adapter.account_snapshot)
        snap["uid"] = 424242
        adapter.account_snapshot = snap
        row = {
            "id": 148630,
            "ts": "2026-11-12T05:40:22Z",
            "strategy_id": "tech_boll_fade_v1",
            "action": "buy",
            "chain_id": "binance",
            "token_address": "INITUSDT",
            "size_pct": 0.066,
            "reason": "neutral_limit",
            "risk_passed": 1,
            "mode": "live_stub",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "raw_json": json.dumps(
                {
                    "pair_address": "INIT/USDT",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                    "extras": self._limit_signal()["extras"],
                }
            ),
        }
        env = self._env()
        with mock.patch.dict(os.environ, env):
            ledger_path = self.root / "execution_ledger_live_bnb_boll_fade.db"
            registry = self.root / "registry.json"
            classic = self.root / "execution_ledger_live_bnb_boll_classic.db"
            if not classic.is_file():
                ExecutionLedger(classic).close()
            initialize_spot_ledger_binding(classic, "424242")
            registry.write_text(
                json.dumps(
                    {
                        "required_purposes": [
                            "live_bnb_boll_fade",
                            "live_bnb_boll_classic",
                        ],
                        "purpose_ledgers": {
                            "live_bnb_boll_fade": str(ledger_path.resolve()),
                            "live_bnb_boll_classic": str(classic.resolve()),
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.environ["BINANCE_SPOT_LEDGER_REGISTRY"] = str(registry)
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=ledger_path,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                binance_spot=adapter,
            ) as gw:
                initialize_spot_ledger_binding(ledger_path, "424242")
                bind_registry_membership("424242")
                processed = gw.process_one(row)
                stored = gw.ledger.conn.execute(
                    "SELECT receipt_json, executed FROM execution_log WHERE decision_log_id = ?",
                    (148630,),
                ).fetchone()
        self.assertEqual(processed["executed"], 0)
        self.assertEqual(processed["error"], "resting_protection_unconfigured")
        self.assertEqual(_names(client, "new_order"), [])
        assert stored is not None
        receipt = json.loads(stored["receipt_json"])
        pre = receipt["exec_result"]["oco_preflight"]
        self.assertEqual(pre["reference"], "planned_entry")
        self.assertEqual(pre["ceiling_source"], "rest_book")
        self.assertEqual(pre["mark_source"], "rest_fallback")
        self.assertEqual(pre["aligned_stop"], "0.13061")
        self.assertEqual(pre["aligned_take_profit"], "0.13591")
        self.assertEqual(pre["aligned_limit"], "0.13206")
        blob = stored["receipt_json"]
        self.assertNotIn(_API_KEY, blob)
        self.assertNotIn(_API_SECRET, blob)
        self.assertNotIn("signature", blob)


class ClassicMarkBandTests(unittest.TestCase):
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

    def _run(
        self,
        mark: str | None,
        *,
        ticker_error: Exception | None = None,
        auto_flatten: bool = False,
    ) -> tuple[dict[str, Any], _Book]:
        marks = [] if mark is None else [mark]
        client = _Book(
            symbol="BNBUSDT",
            filters=_BNB_FILTERS,
            marks=marks,
            ticker_error=ticker_error,
            ack={
                "orderId": "99",
                "status": "FILLED",
                "executedQty": "0.25",
                "cummulativeQuoteQty": "25",
                "symbol": "BNBUSDT",
            },
        )
        adapter = BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "absent-kill",
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected=_EXPECTED,
            auto_flatten_on_oco_fail=auto_flatten,
        )
        signal = {
            "decision_log_id": 148631,
            "strategy_id": "tech_boll_fade_v1",
            "action": "buy",
            "chain_id": "binance",
            "token_address": "BNBUSDT",
            "pair_address": "BNB/USDT",
            "size_pct": 0.05,
            "mode": "live_stub",
            "purpose": PURPOSE_LIVE_BNB_BOLL_CLASSIC,
            "extras": {
                "binance_symbol": "BNBUSDT",
                "order_type": "MARKET",
                "quote_order_qty": "25",
                "price_usd": "100",
                "exit_shell": CLASSIC_EXIT_SHELL,
                "entry_reason": "classic_market",
            },
        }
        env = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
        }
        with mock.patch.dict(os.environ, env):
            return adapter.execute(signal), client

    def test_classic_market_inside_the_band_is_not_sent_under_defaults(self) -> None:
        result, client = self._run("102")
        self.assertEqual(result["error"], "buy_protection_unconfigured", result)
        self.assertTrue(result["oco_preflight"]["mark_inside_band"])
        self.assertEqual(result["oco_preflight"]["reference"], "live_mark")
        self.assertEqual(_names(client, "new_order"), [])
        self.assertEqual(_names(client, "new_oco"), [])
        self.assertEqual(result["exit_plan"]["price_source"], CLASSIC_EXIT_SHELL)

    def test_classic_market_inside_the_band_posts_oco_when_flatten_is_configured(self) -> None:
        result, client = self._run("102", auto_flatten=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["protection_state"], "oco_working")
        self.assertEqual(_form(client, "new_order", side="BUY")["type"], ["MARKET"])
        self.assertEqual(_names(client, "new_oco"), ["new_oco"])

    def test_classic_market_at_or_beyond_stop_or_target_refuses(self) -> None:
        for mark in ("95", "110", "94.99"):
            result, client = self._run(mark)
            self.assertEqual(result["error"], "oco_price_restriction", mark)
            self.assertEqual(_names(client, "new_order"), [], mark)
            self.assertEqual(_names(client, "new_oco"), [], mark)
            self.assertEqual(result["oco_preflight"]["reference"], "live_mark")

    def test_classic_missing_mark_refuses_before_the_order(self) -> None:
        result, client = self._run(None, ticker_error=TimeoutError("timed out"))
        self.assertEqual(result["error"], "mark_unavailable")
        self.assertEqual(_names(client, "new_order"), [])
        self.assertEqual(_names(client, "new_oco"), [])


if __name__ == "__main__":
    unittest.main()
