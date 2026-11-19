"""Independent verification & closure tests for PR 223 (freeze-unknown-oco-2018-90b3).

Verifies all four required invariants plus Tick-0 unprotected exposure durability:
1. ``open_orders`` read after two confirmed ``-2018`` list misses never claims
   protection, never posts ``new_oco``, and never sells (``new_order``), whether
   it returns matching SELL legs, unrelated orders, ``[]``, or raises 418/429/5xx/timeout.
2. Decoded ``oco_rejected:`` POST rejections preserve ``oco_failed`` and still
   trigger ``_auto_flatten_naked_position`` (when ``auto_flatten_on_oco_fail=True``)
   on fresh immediate buys, fresh ``open`` rows, existing ``oco_failed`` rows
   (in-band and out-of-band), and rejected ratchet replacement OCOs, without
   claiming a mismatched list.
3. Clean ``open`` rows without a list id and without an unconfirmed submit marker
   still place their first OCO (including ``BINANCE_SPOT_FILL_TP_FORMULA_B=1``
   and trade-only clients without ``get_order_list``).
4. Confirmed OCO cancel during trailing/breakeven ratchet clears the canceled
   list identity and places the ``-r1`` replacement OCO both in the same tick and
   across a mid-ratchet transient failure / SQLite restart, while an unconfirmed
   cancel holds in ``oco_unknown`` without posting a duplicate OCO.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
from typing import Any
import unittest
from unittest import mock
from urllib.parse import parse_qs

from execution.executors.binance_spot import (
    BinanceRequestError,
    BinanceSpotAdapter,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    buy_exposure_block,
)
from execution.executors.binance_spot_protect import (
    PROTECTION_EXCHANGE,
    PROTECTION_PENDING,
)
from execution.ledger import ExecutionLedger


_FILTERS: list[dict[str, Any]] = [
    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
    {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "900000", "stepSize": "0.01"},
    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "maxQty": "900000", "stepSize": "0.01"},
    {"filterType": "NOTIONAL", "minNotional": "5.00", "applyMinToMarket": True},
]

_EXCHANGE_INFO: dict[str, Any] = {
    "symbols": [
        {
            "symbol": "BNBUSDT",
            "status": "TRADING",
            "baseAsset": "BNB",
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": True,
            "filters": _FILTERS,
        }
    ]
}


def _live_oco_body(
    *,
    symbol: str = "BNBUSDT",
    list_id: int = 42,
    client_id: str = "bnbfade-ol-11",
    above_id: int = 11,
    below_id: int = 10,
    qty: str = "1.23",
    tp_price: str = "110.00",
    sl_price: str = "99.00",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "orderListId": list_id,
        "listClientOrderId": client_id,
        "listOrderStatus": "EXECUTING",
        "listStatusType": "EXEC_STARTED",
        "orders": [
            {"symbol": symbol, "orderId": above_id, "clientOrderId": f"{client_id}-tp"},
            {"symbol": symbol, "orderId": below_id, "clientOrderId": f"{client_id}-sl"},
        ],
        "orderReports": [
            {
                "symbol": symbol,
                "orderId": above_id,
                "clientOrderId": f"{client_id}-tp",
                "type": "LIMIT_MAKER",
                "side": "SELL",
                "status": "NEW",
                "price": tp_price,
                "origQty": qty,
                "executedQty": "0",
            },
            {
                "symbol": symbol,
                "orderId": below_id,
                "clientOrderId": f"{client_id}-sl",
                "type": "STOP_LOSS_LIMIT",
                "side": "SELL",
                "status": "NEW",
                "price": sl_price,
                "stopPrice": sl_price,
                "origQty": qty,
                "executedQty": "0",
            },
        ],
    }


def _make_adapter(
    client: Any, *, kill_switch_path: str, auto_flatten: bool = True
) -> BinanceSpotAdapter:
    cred_kw = {"api_" + "key": "k_test", "api_" + "secret": "s_test"}
    return BinanceSpotAdapter(
        client=client,
        kill_switch_path=Path(kill_switch_path),
        auto_flatten_on_oco_fail=auto_flatten,
        clock_ms=lambda: 1_763_520_000_000,
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
        **cred_kw,
    )


def _make_adapter_bound(
    kill_switch_path: str, client: Any, *, auto_flatten: bool = True
) -> BinanceSpotAdapter:
    return _make_adapter(client, kill_switch_path=kill_switch_path, auto_flatten=auto_flatten)


class Pr223AuditClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pr223-closure-")
        self.addCleanup(self._tmp.cleanup)
        self.absent_kill = str(Path(self._tmp.name) / "no-kill-flag")
        self.base_env = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": self.absent_kill,
        }

    def test_open_orders_results_and_errors_never_claim_post_or_sell(self) -> None:
        """Item 1: open_orders result (matching legs, partial, empty, or HTTP error) never claims, posts, or sells."""
        miss_2018 = BinanceRequestError(400, code=-2018, msg="Order list does not exist.")
        matching_open_orders = [
            {
                "symbol": "BNBUSDT",
                "orderId": 11,
                "orderListId": 42,
                "clientOrderId": "bnbfade-tp-11",
                "side": "SELL",
                "type": "LIMIT_MAKER",
                "status": "NEW",
                "origQty": "1.23",
                "executedQty": "0",
            },
            {
                "symbol": "BNBUSDT",
                "orderId": 10,
                "orderListId": 42,
                "clientOrderId": "bnbfade-sl-11",
                "side": "SELL",
                "type": "STOP_LOSS_LIMIT",
                "status": "NEW",
                "origQty": "1.23",
                "executedQty": "0",
            },
        ]
        unrelated_open_orders = [
            {
                "symbol": "BNBUSDT",
                "orderId": 999,
                "orderListId": -1,
                "clientOrderId": "manual-sell-1",
                "side": "SELL",
                "type": "LIMIT",
                "status": "NEW",
                "origQty": "1.23",
                "executedQty": "0",
            }
        ]
        scenarios: list[tuple[str, object, str]] = [
            ("matching_two_legs_in_band", matching_open_orders, "102.00"),
            ("matching_two_legs_below_stop", matching_open_orders, "90.00"),
            ("unrelated_open_order", unrelated_open_orders, "90.00"),
            ("empty_open_orders", [], "90.00"),
            ("rate_limit_429", BinanceRequestError(429, code=-1003, msg="Too many requests"), "102.00"),
            ("ip_ban_418", BinanceRequestError(418, code=-1003, msg="Way too many requests"), "102.00"),
            ("timeout", TimeoutError("open_orders timed out"), "102.00"),
        ]

        for label, open_orders_reply, mark in scenarios:
            with self.subTest(case=label):
                calls: list[str] = []

                class _ProbeClient:
                    def exchange_info(self, symbol: str) -> dict[str, Any]:
                        return _EXCHANGE_INFO

                    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                        calls.append("get_order_list")
                        if calls.count("get_order_list") > 2:
                            raise AssertionError("must stop after 2 get_order_list reads")
                        raise miss_2018

                    def open_orders(self, signed_query: str, *, api_key: str) -> list[dict[str, Any]]:
                        calls.append("open_orders")
                        if calls.count("open_orders") > 1:
                            raise AssertionError("must call open_orders at most once")
                        parsed = parse_qs(signed_query)
                        assert parsed.get("symbol") == ["BNBUSDT"]
                        if isinstance(open_orders_reply, Exception):
                            raise open_orders_reply
                        return list(open_orders_reply)  # type: ignore[arg-type]

                    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                        calls.append("new_oco")
                        raise AssertionError("open_orders path must never POST new_oco")

                    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                        calls.append("new_order")
                        raise AssertionError("open_orders path must never market-sell")

                row = {
                    "id": 31,
                    "decision_log_id": 11,
                    "symbol": "BNBUSDT",
                    "status": "oco_unknown",
                    "protection": PROTECTION_PENDING,
                    "quantity": "1.23",
                    "stop_price": "99.00",
                    "take_profit_price": "110.00",
                    "entry_order_id": "99",
                    "oco_order_list_id": "42",
                    "oco_list_client_id": "bnbfade-ol-11",
                    "oco_above_order_id": "11",
                    "oco_below_order_id": "10",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                }
                saved = dict(row)

                def persist(fields: dict[str, Any]) -> dict[str, Any]:
                    saved.update(fields)
                    return dict(saved)

                adapter = _make_adapter_bound(self.absent_kill, _ProbeClient(), auto_flatten=True)
                with mock.patch.dict(os.environ, self.base_env, clear=False):
                    out = adapter.maintain_held_position(dict(row), mark, persist=persist)

                self.assertEqual(calls, ["get_order_list", "get_order_list", "open_orders"])
                self.assertEqual(out["book"], "oco_unknown")
                self.assertFalse(out["ok"])
                self.assertFalse(out["submitted"])
                self.assertEqual(saved["status"], "oco_unknown")
                self.assertEqual(saved["protection"], PROTECTION_PENDING)
                self.assertEqual(saved["unprotected_qty"], "1.23")
                self.assertEqual(saved["last_error"], "oco_list_absent:-2018")
                self.assertEqual(
                    saved["manual_action"],
                    "halt_new_buys_and_manually_close_or_protect_the_fill",
                )
                self.assertEqual(
                    buy_exposure_block(
                        [dict(saved)], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ETHUSDT"
                    ),
                    "oco_failed_blocks_buy",
                )

    def test_decoded_oco_rejected_preserves_failure_and_auto_flattens(self) -> None:
        """Item 2: Decoded oco_rejected: still takes oco_failed and auto-flattens (never freezes in oco_unknown)."""
        rejected_exc = BinanceRequestError(
            400, code=-2010, msg="Account has insufficient balance for requested action."
        )
        miss_2018 = BinanceRequestError(400, code=-2018, msg="Order list does not exist.")

        class _RejectAndFlattenClient:
            def __init__(self, *, wrong_list_on_requery: bool = False) -> None:
                self.wrong_list_on_requery = wrong_list_on_requery
                self.calls: list[str] = []

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _EXCHANGE_INFO

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("get_order_list")
                if self.wrong_list_on_requery:
                    return _live_oco_body(symbol="ETHUSDT", list_id=999, client_id="wrong-cid")
                raise miss_2018

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("new_oco")
                raise rejected_exc

            def ticker_price(self, symbol: str) -> dict[str, Any]:
                return {"symbol": symbol, "price": "102.00"}

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                parsed = parse_qs(signed_query)
                side = (parsed.get("side") or [""])[0]
                self.calls.append(f"new_order:{side}" if side != "SELL" else "new_order")
                if side == "BUY":
                    return {
                        "symbol": "BNBUSDT",
                        "orderId": 99,
                        "side": "BUY",
                        "type": "MARKET",
                        "status": "FILLED",
                        "origQty": "1.23",
                        "executedQty": "1.23",
                        "cummulativeQuoteQty": "125.46",
                        "fills": [{"tradeId": 500, "price": "102.00", "qty": "1.23", "commission": "0", "commissionAsset": "USDT", "isBuyer": True}],
                    }
                assert side == "SELL"
                assert parsed.get("type") == ["MARKET"]
                return {
                    "symbol": "BNBUSDT",
                    "orderId": 77701,
                    "side": "SELL",
                    "type": "MARKET",
                    "status": "FILLED",
                    "origQty": "1.23",
                    "executedQty": "1.23",
                    "cummulativeQuoteQty": "125.46",
                    "fills": [{"tradeId": 501, "price": "102.00", "qty": "1.23", "commission": "0", "commissionAsset": "USDT", "isBuyer": False}],
                }

            def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                if parse_qs(signed_query).get("origClientOrderId"):
                    raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")
                oid = (parse_qs(signed_query).get("orderId") or ["99"])[0]
                is_sell = str(oid) == "77701"
                return {
                    "symbol": "BNBUSDT",
                    "orderId": int(oid),
                    "side": "SELL" if is_sell else "BUY",
                    "status": "FILLED",
                    "origQty": "1.23",
                    "executedQty": "1.23",
                }

            def my_trades(self, signed_query: str, *, api_key: str) -> list[dict[str, Any]]:
                if parse_qs(signed_query).get("fromId"):
                    return []
                oid = (parse_qs(signed_query).get("orderId") or ["99"])[0]
                is_sell = str(oid) == "77701"
                return [
                    {
                        "symbol": "BNBUSDT",
                        "id": 501 if is_sell else 500,
                        "orderId": int(oid),
                        "qty": "1.23",
                        "price": "102.00",
                        "commission": "0",
                        "commissionAsset": "USDT",
                        "isBuyer": not is_sell,
                    }
                ]

        # 2a) Fresh open row -> new_oco rejects (even if get_order_list returns wrong symbol) -> oco_failed + auto-flatten!
        for wrong_list in (False, True):
            c_fresh = _RejectAndFlattenClient(wrong_list_on_requery=wrong_list)
            open_row = {
                "id": 41,
                "decision_log_id": 21,
                "symbol": "BNBUSDT",
                "status": "open",
                "protection": PROTECTION_PENDING,
                "quantity": "1.23",
                "stop_price": "99.00",
                "take_profit_price": "110.00",
                "entry_order_id": "99",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            }
            saved_fresh = dict(open_row)
            with mock.patch.dict(os.environ, self.base_env, clear=False):
                res_fresh = _make_adapter_bound(self.absent_kill, c_fresh, auto_flatten=True).maintain_held_position(
                    dict(open_row),
                    "102.00",
                    persist=lambda f, s=saved_fresh: (s.update(f), dict(s))[1],
                )
            self.assertEqual(c_fresh.calls.count("new_oco"), 1)
            self.assertEqual(c_fresh.calls.count("new_order"), 1)
            self.assertEqual(saved_fresh["status"], "closed")
            self.assertIn("oco_rejected:", str(saved_fresh.get("last_error") or res_fresh.get("error")))

        # 2b) auto_flatten=False keeps status="oco_failed" (NOT oco_unknown!) with oco_rejected: prefix
        c_no_flat = _RejectAndFlattenClient()
        saved_nf = {
            "id": 42,
            "decision_log_id": 22,
            "symbol": "BNBUSDT",
            "status": "open",
            "protection": PROTECTION_PENDING,
            "quantity": "1.23",
            "stop_price": "99.00",
            "take_profit_price": "110.00",
            "entry_order_id": "99",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        with mock.patch.dict(os.environ, self.base_env, clear=False):
            res_nf = _make_adapter_bound(self.absent_kill, c_no_flat, auto_flatten=False).maintain_held_position(
                dict(saved_nf),
                "102.00",
                persist=lambda f: (saved_nf.update(f), dict(saved_nf))[1],
            )
        self.assertEqual(res_nf["book"], "oco_failed")
        self.assertEqual(saved_nf["status"], "oco_failed")
        self.assertTrue(str(saved_nf["last_error"]).startswith("oco_rejected:"))
        self.assertEqual(c_no_flat.calls.count("new_order"), 0)

        # 2c) Existing oco_failed row with oco_list_client_id + oco_rejected: auto-flattens both out-of-band and in-band
        for mark, expected_oco_attempts in (("90.00", 0), ("102.00", 1)):
            c_prev = _RejectAndFlattenClient()
            prev_failed = dict(saved_nf)
            with mock.patch.dict(os.environ, self.base_env, clear=False):
                _make_adapter_bound(self.absent_kill, c_prev, auto_flatten=True).maintain_held_position(
                    dict(prev_failed),
                    mark,
                    persist=lambda f, s=prev_failed: (s.update(f), dict(s))[1],
                )
            self.assertEqual(c_prev.calls.count("new_oco"), expected_oco_attempts)
            self.assertEqual(c_prev.calls.count("new_order"), 1)
            self.assertEqual(prev_failed["status"], "closed")

        # 2d) Immediate live BUY fill -> new_oco decoded oco_rejected: (even with wrong-symbol get_order_list) -> auto-flattens!
        for wrong_list in (False, True):
            c_buy = _RejectAndFlattenClient(wrong_list_on_requery=wrong_list)
            buy_sig = {
                "decision_log_id": 290 + (1 if wrong_list else 0),
                "strategy_id": "tech_boll_fade_v1",
                "action": "buy",
                "chain_id": "binance",
                "token_address": "BNBUSDT",
                "pair_address": "BNB/USDT",
                "size_pct": 0.05,
                "mode": "live_stub",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "extras": {
                    "binance_symbol": "BNBUSDT",
                    "order_type": "MARKET",
                    "quote_order_qty": "125.46",
                    "price_usd": "102.00",
                    "stop_price": "99.00",
                    "take_profit_price": "110.00",
                },
            }
            with mock.patch.dict(os.environ, self.base_env, clear=False):
                buy_out = _make_adapter_bound(self.absent_kill, c_buy, auto_flatten=True).execute(buy_sig)
            self.assertFalse(buy_out["ok"])
            self.assertEqual(c_buy.calls.count("new_order:BUY"), 1)
            self.assertEqual(c_buy.calls.count("new_oco"), 1)
            self.assertEqual(c_buy.calls.count("new_order"), 1)
            self.assertEqual((buy_out.get("arm_position") or {}).get("status"), "closed")
            self.assertIn("oco_rejected:", str(buy_out.get("error") or ""))

    def test_clean_open_without_list_id_or_unconfirmed_error_places_first_oco(self) -> None:
        """Item 3: Clean open row with no list_id and no unconfirmed error places first OCO (including Formula B & trade-only client)."""
        submitted_queries: list[str] = []

        class _TradeOnlyClient:
            """Has exchange_info and new_oco, deliberately NO get_order_list."""

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _EXCHANGE_INFO

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                submitted_queries.append(signed_query)
                parsed = parse_qs(signed_query)
                return _live_oco_body(
                    symbol="BNBUSDT",
                    list_id=88,
                    client_id=parsed["listClientOrderId"][0],
                    qty=parsed["quantity"][0],
                    tp_price=parsed["abovePrice"][0],
                    sl_price=parsed["belowStopPrice"][0],
                )

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise AssertionError("clean open first OCO must not sell")

        # Mark 111.00 > signal TP 110.00, but entry fill 105.00 vs signal entry 100.00 lifts Formula B TP to 115.50!
        clean_open = {
            "id": 51,
            "decision_log_id": 31,
            "symbol": "BNBUSDT",
            "status": "open",
            "protection": PROTECTION_PENDING,
            "quantity": "1.23",
            "entry_price": "105.00",
            "signal_entry_price": "100.00",
            "signal_take_profit_price": "110.00",
            "signal_stop_price": "99.00",
            "tp_multiplier": "1",
            "stop_price": "99.00",
            "take_profit_price": "110.00",
            "entry_order_id": "99",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        saved = dict(clean_open)
        env_fb = {**self.base_env, "BINANCE_SPOT_FILL_TP_FORMULA_B": "1"}
        with mock.patch.dict(os.environ, env_fb, clear=False):
            out = _make_adapter_bound(self.absent_kill, _TradeOnlyClient(), auto_flatten=True).maintain_held_position(
                dict(clean_open),
                "111.00",
                persist=lambda f: (saved.update(f), dict(saved))[1],
            )
        self.assertEqual(len(submitted_queries), 1)
        self.assertEqual(out["book"], "oco_working")
        self.assertEqual(saved["status"], "oco_working")
        self.assertEqual(saved["protection"], PROTECTION_EXCHANGE)
        self.assertEqual(saved["take_profit_price"], "115.5")
        self.assertEqual(saved["stop_price"], "99.00")

    def test_confirmed_cancel_ratchet_places_replacement_same_tick_across_restart_and_on_reject(
        self,
    ) -> None:
        """Item 4: Confirmed ratchet cancel places replacement OCO same-tick & across restart; unconfirmed cancel holds; rejected replacement auto-flattens."""
        ratchet_env = {**self.base_env, "BINANCE_SPOT_TRAILING_RATCHET": "1"}

        class _RatchetScenarioClient:
            def __init__(
                self,
                *,
                fail_info_on_call: int | None = None,
                unconfirmed_cancel: bool = False,
                reject_new_oco: bool = False,
            ) -> None:
                self.fail_info_on_call = fail_info_on_call
                self.unconfirmed_cancel = unconfirmed_cancel
                self.reject_new_oco = reject_new_oco
                self.info_calls = 0
                self.calls: list[str] = []
                self.placed_queries: list[str] = []

            def exchange_info(self, symbol: str) -> dict[str, Any]:
                self.info_calls += 1
                self.calls.append("exchange_info")
                if self.fail_info_on_call is not None and self.info_calls == self.fail_info_on_call:
                    raise TimeoutError("transient exchange_info timeout right after cancel")
                return _EXCHANGE_INFO

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("get_order_list")
                parsed = parse_qs(signed_query)
                if parsed.get("orderListId") == ["5001"]:
                    return _live_oco_body(
                        list_id=5001,
                        client_id="bnbfade-ol-42",
                        above_id=6001,
                        below_id=6002,
                        qty="1.50",
                        tp_price="110.00",
                        sl_price="96.00",
                    )
                if parsed.get("orderListId") == ["5002"] and self.placed_queries:
                    q0 = parse_qs(self.placed_queries[-1])
                    return _live_oco_body(
                        list_id=5002,
                        client_id=q0["listClientOrderId"][0],
                        above_id=7001,
                        below_id=7002,
                        qty=q0["quantity"][0],
                        tp_price=q0["abovePrice"][0],
                        sl_price=q0["belowStopPrice"][0],
                    )
                raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")

            def cancel_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("cancel_order_list")
                if self.unconfirmed_cancel:
                    return {
                        "symbol": "BNBUSDT",
                        "orderListId": 5001,
                        "listStatusType": "EXEC_STARTED",
                        "listOrderStatus": "EXECUTING",
                        "orders": [{"orderId": 6001}, {"orderId": 6002}],
                    }
                return {
                    "symbol": "BNBUSDT",
                    "orderListId": 5001,
                    "listStatusType": "ALL_DONE",
                    "listOrderStatus": "ALL_DONE",
                    "orderReports": [
                        {
                            "symbol": "BNBUSDT",
                            "orderId": 6001,
                            "type": "LIMIT_MAKER",
                            "status": "CANCELED",
                            "executedQty": "0.00",
                        },
                        {
                            "symbol": "BNBUSDT",
                            "orderId": 6002,
                            "type": "STOP_LOSS_LIMIT",
                            "status": "CANCELED",
                            "executedQty": "0.00",
                        },
                    ],
                }

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("new_oco")
                self.placed_queries.append(signed_query)
                if self.reject_new_oco:
                    raise BinanceRequestError(
                        400, code=-2010, msg="Account has insufficient balance."
                    )
                parsed = parse_qs(signed_query)
                return _live_oco_body(
                    list_id=5002,
                    client_id=parsed["listClientOrderId"][0],
                    above_id=7001,
                    below_id=7002,
                    qty=parsed["quantity"][0],
                    tp_price=parsed["abovePrice"][0],
                    sl_price=parsed["belowStopPrice"][0],
                )

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                self.calls.append("new_order")
                return {
                    "symbol": "BNBUSDT",
                    "orderId": 88801,
                    "side": "SELL",
                    "type": "MARKET",
                    "status": "FILLED",
                    "origQty": "1.50",
                    "executedQty": "1.50",
                    "cummulativeQuoteQty": "161.25",
                    "fills": [{"price": "107.50", "qty": "1.50", "commission": "0", "commissionAsset": "USDT"}],
                }

            def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                if parse_qs(signed_query).get("origClientOrderId"):
                    raise BinanceRequestError(400, code=-2013, msg="Order does not exist.")
                oid = (parse_qs(signed_query).get("orderId") or ["99"])[0]
                if str(oid) in {"6001", "6002", "7001", "7002"}:
                    return {
                        "symbol": "BNBUSDT",
                        "orderId": int(oid),
                        "side": "SELL",
                        "type": "LIMIT_MAKER" if str(oid) in {"6001", "7001"} else "STOP_LOSS_LIMIT",
                        "status": "NEW",
                        "origQty": "1.50",
                        "executedQty": "0",
                    }
                is_sell = str(oid) == "88801"
                return {
                    "symbol": "BNBUSDT",
                    "orderId": int(oid),
                    "side": "SELL" if is_sell else "BUY",
                    "status": "FILLED",
                    "origQty": "1.50",
                    "executedQty": "1.50",
                }

            def my_trades(self, signed_query: str, *, api_key: str) -> list[dict[str, Any]]:
                if parse_qs(signed_query).get("fromId"):
                    return []
                oid = (parse_qs(signed_query).get("orderId") or ["99"])[0]
                is_sell = str(oid) == "88801"
                return [
                    {
                        "symbol": "BNBUSDT",
                        "id": 901 if is_sell else 900,
                        "orderId": int(oid),
                        "qty": "1.50",
                        "price": "107.50" if is_sell else "100.00",
                        "commission": "0",
                        "commissionAsset": "USDT",
                        "isBuyer": not is_sell,
                    }
                ]

        # 4a) Confirmed cancel + transient exchange_info blip before replacement POST + SQLite restart -> next tick STILL places -r1 replacement OCO!
        db_path = Path(self._tmp.name) / "ratchet_restart.db"
        ledger = ExecutionLedger(db_path)
        armed = ledger.arm_binance_spot_position(
            {
                "decision_log_id": 42,
                "symbol": "BNBUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "oco_working",
                "protection": PROTECTION_EXCHANGE,
                "quantity": "1.50",
                "entry_price": "100.00",
                "stop_price": "96.00",
                "take_profit_price": "110.00",
                "entry_order_id": "99",
            }
        )
        pid = int(armed["id"])
        ledger.update_binance_spot_position(
            pid,
            {
                "oco_order_list_id": "5001",
                "oco_list_client_id": "bnbfade-ol-42",
                "oco_above_order_id": "6001",
                "oco_below_order_id": "6002",
            },
        )
        # Call 1 of exchange_info is in _maybe_ratchet_working_oco; Call 2 is inside _try_place_oco -> fail Call 2!
        c_blip = _RatchetScenarioClient(fail_info_on_call=2)
        with mock.patch.dict(os.environ, ratchet_env, clear=False):
            # auto_flatten=False so we test next-tick replacement recovery across SQLite restart
            t1 = _make_adapter_bound(self.absent_kill, c_blip, auto_flatten=False).maintain_held_position(
                dict(ledger.binance_spot_position_by_id(pid) or {}),
                "107.50",
                persist=lambda f: ledger.update_binance_spot_position(pid, f),
            )
        self.assertEqual(c_blip.calls.count("cancel_order_list"), 1)
        self.assertEqual(c_blip.calls.count("new_oco"), 0)
        self.assertEqual(t1["book"], "oco_failed")
        ledger.close()

        reopened = ExecutionLedger(db_path)
        mid_row = reopened.binance_spot_position_by_id(pid)
        assert mid_row is not None
        # Old canceled list IDs MUST be cleared so next tick is not frozen by _readback_hold_row!
        self.assertEqual(mid_row["oco_order_list_id"] or "", "")
        self.assertEqual(mid_row["oco_list_client_id"] or "", "")
        self.assertEqual(int(mid_row["oco_ratchet_count"]), 1)

        c_recover = _RatchetScenarioClient()
        with mock.patch.dict(os.environ, ratchet_env, clear=False):
            t2 = _make_adapter_bound(self.absent_kill, c_recover, auto_flatten=True).maintain_held_position(
                dict(reopened.binance_spot_position_by_id(pid) or {}),
                "107.50",
                persist=lambda f: reopened.update_binance_spot_position(pid, f),
            )
        self.assertEqual(t2["book"], "oco_working")
        self.assertEqual(c_recover.calls.count("new_oco"), 1)
        self.assertIn("bnbfade-ol-42-r1", c_recover.placed_queries[0])
        final_row = reopened.binance_spot_position_by_id(pid)
        assert final_row is not None
        self.assertEqual(final_row["status"], "oco_working")
        self.assertEqual(final_row["oco_order_list_id"], "5002")
        reopened.close()

        # 4b) Unconfirmed cancel -> holds in oco_unknown with unprotected_qty, never posts replacement OCO
        c_unconf = _RatchetScenarioClient(unconfirmed_cancel=True)
        row_u = {
            "id": 62,
            "decision_log_id": 43,
            "symbol": "BNBUSDT",
            "status": "oco_working",
            "protection": PROTECTION_EXCHANGE,
            "quantity": "1.50",
            "entry_price": "100.00",
            "stop_price": "96.00",
            "take_profit_price": "110.00",
            "entry_order_id": "99",
            "oco_order_list_id": "5001",
            "oco_list_client_id": "bnbfade-ol-43",
            "oco_above_order_id": "6001",
            "oco_below_order_id": "6002",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        saved_u = dict(row_u)
        with mock.patch.dict(os.environ, ratchet_env, clear=False):
            out_u = _make_adapter_bound(self.absent_kill, c_unconf, auto_flatten=True).maintain_held_position(
                dict(row_u),
                "107.50",
                persist=lambda f: (saved_u.update(f), dict(saved_u))[1],
            )
        self.assertEqual(out_u["book"], "oco_unknown")
        self.assertEqual(saved_u["status"], "oco_unknown")
        self.assertEqual(saved_u["last_error"], "oco_cancel_unconfirmed")
        self.assertEqual(saved_u["unprotected_qty"], "1.50")
        self.assertEqual(c_unconf.calls.count("new_oco"), 0)
        self.assertEqual(c_unconf.calls.count("new_order"), 0)

        # 4c) Confirmed cancel + decoded oco_rejected on replacement OCO with auto_flatten=True -> auto-flattens!
        c_rej = _RatchetScenarioClient(reject_new_oco=True)
        row_r = dict(row_u, id=63, decision_log_id=44)
        saved_r = dict(row_r)
        with mock.patch.dict(os.environ, ratchet_env, clear=False):
            out_r = _make_adapter_bound(self.absent_kill, c_rej, auto_flatten=True).maintain_held_position(
                dict(row_r),
                "107.50",
                persist=lambda f: (saved_r.update(f), dict(saved_r))[1],
            )
        self.assertTrue(out_r.get("oco_ratcheted"))
        self.assertEqual(c_rej.calls.count("cancel_order_list"), 1)
        self.assertEqual(c_rej.calls.count("new_oco"), 1)
        self.assertEqual(c_rej.calls.count("new_order"), 1)
        self.assertEqual(saved_r["status"], "closed")

        # 4d) Confirmed cancel + immediate replacement OCO in the exact same tick
        c_same = _RatchetScenarioClient()
        row_s = dict(row_u, id=64, decision_log_id=45, oco_list_client_id="bnbfade-ol-45")
        saved_s = dict(row_s)
        with mock.patch.dict(os.environ, ratchet_env, clear=False):
            out_s = _make_adapter_bound(self.absent_kill, c_same, auto_flatten=True).maintain_held_position(
                dict(row_s),
                "107.50",
                persist=lambda f: (saved_s.update(f), dict(saved_s))[1],
            )
        self.assertTrue(out_s.get("oco_ratcheted"))
        self.assertEqual(out_s["book"], "oco_working")
        self.assertEqual(c_same.calls.count("cancel_order_list"), 1)
        self.assertEqual(c_same.calls.count("new_oco"), 1)
        self.assertEqual(c_same.calls.count("new_order"), 0)
        self.assertIn("bnbfade-ol-45-r1", c_same.placed_queries[0])
        self.assertEqual(saved_s["status"], "oco_working")
        self.assertEqual(saved_s["oco_order_list_id"], "5002")

    def test_tick0_oco_unknown_persists_unprotected_qty_and_manual_halt_immediately(self) -> None:
        """Tick 0 timeout or unproven 200 + -2018 persists unprotected_qty & manual_action immediately."""
        miss_2018 = BinanceRequestError(400, code=-2018, msg="Order list does not exist.")

        class _TimeoutSubmitClient:
            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _EXCHANGE_INFO

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise miss_2018

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise TimeoutError("POST /api/v3/orderList/oco timed out")

        row = {
            "id": 71,
            "decision_log_id": 51,
            "symbol": "BNBUSDT",
            "status": "open",
            "protection": PROTECTION_PENDING,
            "quantity": "1.23",
            "stop_price": "99.00",
            "take_profit_price": "110.00",
            "entry_order_id": "99",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        saved = dict(row)
        with mock.patch.dict(os.environ, self.base_env, clear=False):
            out = _make_adapter_bound(self.absent_kill, _TimeoutSubmitClient(), auto_flatten=True).maintain_held_position(
                dict(row),
                "102.00",
                persist=lambda f: (saved.update(f), dict(saved))[1],
            )
        self.assertEqual(out["book"], "oco_unknown")
        self.assertEqual(saved["status"], "oco_unknown")
        self.assertEqual(saved["last_error"], "oco_submit_timeout")
        self.assertEqual(saved["unprotected_qty"], "1.23")
        self.assertEqual(
            saved["manual_action"],
            "halt_new_buys_and_manually_close_or_protect_the_fill",
        )


    def test_buy_side_legs_never_claim_protection_or_clear_unprotected_qty(self) -> None:
        """t316 Blocker 1: BUY-side legs (both BUY, one BUY, or get_order BUY/missing side) NEVER claim oco_working or clear unprotected_qty."""
        base_row = {
            "id": 81,
            "decision_log_id": 61,
            "symbol": "BNBUSDT",
            "status": "oco_unknown",
            "protection": PROTECTION_PENDING,
            "quantity": "1.23",
            "stop_price": "99.00",
            "take_profit_price": "110.00",
            "entry_order_id": "99",
            "oco_order_list_id": "42",
            "oco_list_client_id": "bnbfade-ol-61",
            "oco_above_order_id": "11",
            "oco_below_order_id": "10",
            "unprotected_qty": "1.23",
            "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }

        def _body_with_sides(above_side: str, below_side: str) -> dict[str, Any]:
            body = _live_oco_body(
                symbol="BNBUSDT",
                list_id=42,
                client_id="bnbfade-ol-61",
                above_id=11,
                below_id=10,
                qty="1.23",
            )
            body["orderReports"][0]["side"] = above_side
            body["orderReports"][1]["side"] = below_side
            return body

        for case_label, above_s, below_s, start_status in (
            ("both_legs_buy_on_unknown", "BUY", "BUY", "oco_unknown"),
            ("above_leg_buy_on_unknown", "BUY", "SELL", "oco_unknown"),
            ("below_leg_buy_on_unknown", "SELL", "BUY", "oco_unknown"),
            ("empty_side_on_unknown", "", "SELL", "oco_unknown"),
            ("both_legs_buy_on_working_refresh", "BUY", "BUY", "oco_working"),
        ):
            with self.subTest(case=case_label):
                payload = _body_with_sides(above_s, below_s)

                class _WrongSideClient:
                    def exchange_info(self, symbol: str) -> dict[str, Any]:
                        return _EXCHANGE_INFO

                    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                        return payload

                    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                        raise AssertionError("wrong-side readback must never post new_oco")

                    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                        raise AssertionError("wrong-side readback must never sell")

                row_in = dict(base_row, status=start_status)
                saved = dict(row_in)
                with mock.patch.dict(os.environ, self.base_env, clear=False):
                    out = _make_adapter_bound(
                        self.absent_kill, _WrongSideClient(), auto_flatten=True
                    ).maintain_held_position(
                        dict(row_in),
                        "102.00",
                        persist=lambda f, s=saved: (s.update(f), dict(s))[1],
                    )
                self.assertEqual(out["book"], "oco_unknown")
                self.assertEqual(saved["status"], "oco_unknown")
                self.assertEqual(saved["last_error"], "oco_readback_side_mismatch")
                self.assertEqual(saved["unprotected_qty"], "1.23")
                self.assertEqual(
                    saved["manual_action"],
                    "halt_new_buys_and_manually_close_or_protect_the_fill",
                )

        # Also test real Binance wire shape where GET /api/v3/orderList omits orderReports and _load_oco_legs calls get_order!
        class _GetOrderBuyLegClient:
            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _EXCHANGE_INFO

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                body = _live_oco_body(list_id=42, client_id="bnbfade-ol-61")
                body.pop("orderReports", None)
                return body

            def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                oid = int(parse_qs(signed_query)["orderId"][0])
                return {
                    "symbol": "BNBUSDT",
                    "orderId": oid,
                    "side": "BUY",
                    "type": "LIMIT_MAKER" if oid == 11 else "STOP_LOSS_LIMIT",
                    "status": "NEW",
                    "origQty": "1.23",
                    "executedQty": "0",
                }

        saved_leg = dict(base_row)
        with mock.patch.dict(os.environ, self.base_env, clear=False):
            out_leg = _make_adapter_bound(
                self.absent_kill, _GetOrderBuyLegClient(), auto_flatten=True
            ).maintain_held_position(
                dict(base_row),
                "102.00",
                persist=lambda f: (saved_leg.update(f), dict(saved_leg))[1],
            )
        self.assertEqual(out_leg["book"], "oco_unknown")
        self.assertEqual(saved_leg["status"], "oco_unknown")
        self.assertEqual(saved_leg["last_error"], "oco_readback_side_mismatch")
        self.assertEqual(saved_leg["unprotected_qty"], "1.23")

    def test_concurrent_same_row_maintenance_serializes_and_next_pass_reads_once(self) -> None:
        """t316 Blocker 2: Concurrent two-thread maintenance on the same row never interleaves, and post-freeze passes read at most once."""
        miss_2018 = BinanceRequestError(400, code=-2018, msg="Order list does not exist.")
        active_in_flight = 0
        max_concurrent = 0
        guard = threading.Lock()
        calls: list[str] = []

        class _ConcurrentProbeClient:
            def exchange_info(self, symbol: str) -> dict[str, Any]:
                return _EXCHANGE_INFO

            def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                nonlocal active_in_flight, max_concurrent
                with guard:
                    active_in_flight += 1
                    max_concurrent = max(max_concurrent, active_in_flight)
                    calls.append("get_order_list")
                try:
                    raise miss_2018
                finally:
                    with guard:
                        active_in_flight -= 1

            def open_orders(self, signed_query: str, *, api_key: str) -> list[dict[str, Any]]:
                with guard:
                    calls.append("open_orders")
                return []

            def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise AssertionError("frozen concurrent maintenance must never post new_oco")

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                raise AssertionError("frozen concurrent maintenance must never sell")

        row = {
            "id": 91,
            "decision_log_id": 71,
            "symbol": "BNBUSDT",
            "status": "oco_unknown",
            "protection": PROTECTION_PENDING,
            "quantity": "1.23",
            "stop_price": "99.00",
            "take_profit_price": "110.00",
            "entry_order_id": "99",
            "oco_order_list_id": "42",
            "oco_list_client_id": "bnbfade-ol-71",
            "oco_above_order_id": "11",
            "oco_below_order_id": "10",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        shared_saved = dict(row)
        save_lock = threading.Lock()

        def persist_shared(fields: dict[str, Any]) -> dict[str, Any]:
            with save_lock:
                shared_saved.update(fields)
                return dict(shared_saved)

        adapter = _make_adapter_bound(self.absent_kill, _ConcurrentProbeClient(), auto_flatten=True)
        barrier = threading.Barrier(2)
        results: list[dict[str, Any]] = []

        def _worker() -> None:
            barrier.wait(timeout=5.0)
            out = adapter.maintain_held_position(dict(row), "90.00", persist=persist_shared)
            with save_lock:
                results.append(out)

        with mock.patch.dict(os.environ, self.base_env, clear=False):
            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

        self.assertEqual(len(results), 2)
        self.assertEqual(max_concurrent, 1)
        self.assertEqual(
            calls,
            [
                "get_order_list",
                "get_order_list",
                "open_orders",
                "get_order_list",
                "get_order_list",
                "open_orders",
            ],
        )
        self.assertEqual(shared_saved["status"], "oco_unknown")
        self.assertEqual(shared_saved["unprotected_qty"], "1.23")
        self.assertEqual(shared_saved["last_error"], "oco_list_absent:-2018")

if __name__ == "__main__":
    unittest.main()
