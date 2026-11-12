"""Unit and integration tests for Post-Only LIMIT_MAKER, BBO Maker Pegging, SOR Fallback, and Entry TTL Auto-Cancel."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from execution.executors.binance_spot import BinanceSpotAdapter
from execution.executors.binance_spot_gate import PURPOSE_LIVE_BNB_BOLL_FADE, REST_BOOK
from execution.executors.binance_spot_protect import (
    peg_maker_buy_price,
    sor_maker_fallback_spec,
)


def _exchange_info(symbol: str = "SOLUSDT") -> dict:
    return {
        "symbols": [
            {
                "symbol": symbol,
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "100000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "100000"},
                    {"filterType": "NOTIONAL", "minNotional": "10.0", "applyMinToMarket": True},
                ],
            }
        ]
    }


def _account_snapshot() -> dict:
    return {
        "accountType": "SPOT",
        "canTrade": True,
        "permissions": ["SPOT"],
        "restrictions": {
            "enableSpotAndMarginTrading": True,
            "enableReading": True,
            "ipRestrict": True,
        },
        "balances": [{"asset": "USDT", "free": "500.0"}],
    }


class _MakerAndSorMockClient:
    def __init__(
        self,
        *,
        depth_payload: dict | None = None,
        order_status_on_new: str = "FILLED",
        order_on_get: dict | None = None,
    ) -> None:
        self.depth_payload = depth_payload or {
            "bids": [["99.95", "50.0"]],
            "asks": [["100.05", "50.0"]],
        }
        self.order_status_on_new = order_status_on_new
        self.order_on_get = order_on_get or {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "status": "FILLED",
            "executedQty": "1.00",
            "cummulativeQuoteQty": "99.95",
            "time": 1_700_000_000_000,
            "fills": [{"tradeId": 1, "qty": "1.00", "commission": "0", "commissionAsset": "SOL"}],
        }
        self.new_order_queries: list[str] = []
        self.cancel_order_queries: list[str] = []
        self.new_oco_calls = 0

    def exchange_info(self, symbol: str) -> dict:
        return _exchange_info(symbol)

    def ticker_price(self, symbol: str) -> dict:
        return {"symbol": symbol, "price": "100.00"}

    def order_book(self, symbol: str, *, limit: int = 20) -> dict:
        return self.depth_payload

    def new_order(self, signed_query: str, *, api_key: str) -> dict:
        self.new_order_queries.append(signed_query)
        if self.order_status_on_new == "NEW":
            return {
                "symbol": "SOLUSDT",
                "orderId": 91001,
                "status": "NEW",
                "executedQty": "0.00",
                "cummulativeQuoteQty": "0.00",
                "price": "99.95",
            }
        return {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "status": "FILLED",
            "executedQty": "1.00",
            "cummulativeQuoteQty": "99.95",
            "fills": [{"tradeId": 1, "qty": "1.00", "commission": "0", "commissionAsset": "SOL"}],
        }

    def get_order(self, signed_query: str, *, api_key: str) -> dict:
        return self.order_on_get

    def cancel_order(self, signed_query: str, *, api_key: str) -> dict:
        self.cancel_order_queries.append(signed_query)
        canceled = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "status": "CANCELED",
            "executedQty": "0.00",
            "cummulativeQuoteQty": "0.00",
        }
        self.order_on_get = canceled
        return canceled

    def new_oco(self, signed_query: str, *, api_key: str) -> dict:
        self.new_oco_calls += 1
        return {
            "orderListId": 92001,
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "orders": [{"orderId": 8001}, {"orderId": 8002}],
            "orderReports": [
                {"orderId": 8001, "type": "LIMIT_MAKER", "status": "NEW"},
                {"orderId": 8002, "type": "STOP_LOSS_LIMIT", "status": "NEW"},
            ],
        }


class LimitMakerAndSorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_live = os.environ.get("BINANCE_SPOT_LIVE_ENABLED")
        os.environ["BINANCE_SPOT_LIVE_ENABLED"] = "1"

    def tearDown(self) -> None:
        if self._old_live is None:
            os.environ.pop("BINANCE_SPOT_LIVE_ENABLED", None)
        else:
            os.environ["BINANCE_SPOT_LIVE_ENABLED"] = self._old_live

    def test_peg_maker_buy_price_clamps_crossing_limit_to_best_bid(self) -> None:
        filters = _exchange_info()["symbols"][0]["filters"]
        # 1. Limit price 99.80 is already below best_ask 100.05 -> unchanged
        px, clamped, err = peg_maker_buy_price(
            "99.80",
            best_bid="99.95",
            best_ask="100.05",
            stop_price="98.00",
            take_profit_price="104.00",
            filters=filters,
        )
        self.assertIsNone(err)
        self.assertFalse(clamped)
        self.assertEqual(px, "99.8")

        # 2. Limit price 100.10 >= best_ask 100.05 -> clamped to best_bid 99.95!
        px2, clamped2, err2 = peg_maker_buy_price(
            "100.10",
            best_bid="99.95",
            best_ask="100.05",
            stop_price="98.00",
            take_profit_price="104.00",
            filters=filters,
        )
        self.assertIsNone(err2)
        self.assertTrue(clamped2)
        self.assertEqual(px2, "99.95")

    def test_post_only_limit_maker_omits_time_in_force_and_clamps_bbo(self) -> None:
        sig = {
            "decision_log_id": 170001,
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "mode": "live_stub",
            "action": "buy",
            "token_address": "SOLUSDT",
            "extras": {
                "binance_symbol": "SOLUSDT",
                "order_type": "LIMIT",
                "limit_price": "100.10",  # Would cross best_ask=100.05 without BBO pegging!
                "quantity": "1.00",
                "stop_price": "98.00",
                "take_profit_price": "104.00",
                "entry_reason": "neutral_limit",
            },
        }
        client = _MakerAndSorMockClient()
        with tempfile.TemporaryDirectory() as tmp:
            adapter = BinanceSpotAdapter(
                client=client,
                kill_switch_path=Path(tmp) / "NO_KILL",
                api_key="k",
                api_secret="s",
                account_snapshot=_account_snapshot(),
                production_book=REST_BOOK,
                rest_expected={"account_type": "SPOT", "usdt_free": "500"},
                post_only_limit=True,
                auto_flatten_on_oco_fail=True,
            )
            res = adapter.execute(sig)
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual(len(client.new_order_queries), 1)
            q = client.new_order_queries[0]
            self.assertIn("type=LIMIT_MAKER", q)
            self.assertNotIn("timeInForce", q)
            self.assertIn("price=99.95", q)
            self.assertTrue(res["exit_plan"].get("maker_pegged"))

    def test_sor_downgrades_high_impact_market_buy_to_passive_limit_maker(self) -> None:
        # Thin ask book: ask1 has only 0.10 SOL at 100.05, ask2 is at 102.50 (+240 bps impact)
        thin_book = {
            "bids": [["99.95", "50.0"]],
            "asks": [["100.05", "0.10"], ["102.50", "50.0"]],
        }
        sig = {
            "decision_log_id": 170002,
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "mode": "live_stub",
            "action": "buy",
            "token_address": "SOLUSDT",
            "extras": {
                "binance_symbol": "SOLUSDT",
                "order_type": "MARKET",
                "quote_order_qty": "100.0",
                "price_usd": "100.0",
                "stop_price": "98.0",
                "take_profit_price": "104.0",
                "entry_reason": "large_trade_market",
            },
        }
        client = _MakerAndSorMockClient(depth_payload=thin_book)
        with tempfile.TemporaryDirectory() as tmp:
            adapter = BinanceSpotAdapter(
                client=client,
                kill_switch_path=Path(tmp) / "NO_KILL",
                api_key="k",
                api_secret="s",
                account_snapshot=_account_snapshot(),
                production_book=REST_BOOK,
                rest_expected={"account_type": "SPOT", "usdt_free": "500"},
                orderbook_gate=True,
                sor_maker_fallback=True,
                auto_flatten_on_oco_fail=True,
            )
            res = adapter.execute(sig)
            self.assertTrue(res["ok"], res.get("error"))
            self.assertEqual(len(client.new_order_queries), 1)
            q = client.new_order_queries[0]
            # Converted from MARKET quoteOrderQty=100 to LIMIT_MAKER at best_bid=99.95, quantity=1
            self.assertIn("type=LIMIT_MAKER", q)
            self.assertIn("price=99.95", q)
            self.assertNotIn("quoteOrderQty", q)
            self.assertEqual(
                res["orderbook_metrics"].get("sor_routed"),
                "limit_maker_at_best_bid",
            )
            self.assertEqual(
                res["orderbook_metrics"].get("sor_original_block"),
                "orderbook_impact_too_high",
            )

    def test_reconcile_resting_entry_adopts_fill_or_cancels_on_ttl_expiry(self) -> None:
        # 1. Resting order fills on subsequent tick -> adopts fill and places native OCO
        filled_client = _MakerAndSorMockClient(
            order_on_get={
                "symbol": "SOLUSDT",
                "orderId": 91001,
                "status": "FILLED",
                "executedQty": "1.00",
                "cummulativeQuoteQty": "99.95",
                "time": 1_700_000_000_000,
                "fills": [{"tradeId": 1, "qty": "1.00", "commission": "0", "commissionAsset": "SOL"}],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            adapter = BinanceSpotAdapter(
                client=filled_client,
                kill_switch_path=Path(tmp) / "NO_KILL",
                api_key="k",
                api_secret="s",
                entry_ttl_sec=180,
            )
            pos = {
                "id": 1,
                "decision_log_id": 170003,
                "symbol": "SOLUSDT",
                "entry_order_id": "91001",
                "status": "qty_unconfirmed",
                "protection": "held_position_exit",
                "quantity": None,
                "entry_price": "99.95",
                "stop_price": "98.00",
                "take_profit_price": "104.00",
                "created_at_ms": 1_700_000_000_000,
            }
            out_filled = adapter.reconcile_resting_entry(
                pos,
                mark="100.00",
                now_ms=1_700_000_030_000,
            )
            self.assertTrue(out_filled["ok"])
            self.assertEqual(out_filled["book"], "oco_working")
            self.assertEqual(filled_client.new_oco_calls, 1)

            # 2. Resting order still NEW after 200s (> 180s TTL) -> canceled via DELETE /api/v3/order
            unfilled_client = _MakerAndSorMockClient(
                order_on_get={
                    "symbol": "SOLUSDT",
                    "orderId": 91002,
                    "status": "NEW",
                    "executedQty": "0.00",
                    "cummulativeQuoteQty": "0.00",
                    "time": 1_700_000_000_000,
                }
            )
            adapter.client = unfilled_client
            pos_stale = dict(pos, entry_order_id="91002")
            out_canceled = adapter.reconcile_resting_entry(
                pos_stale,
                mark="100.50",
                now_ms=1_700_000_200_000,
            )
            self.assertTrue(out_canceled["ok"])
            self.assertEqual(out_canceled["book"], "closed_canceled")
            self.assertEqual(out_canceled["reason"], "entry_ttl_expired")
            self.assertEqual(len(unfilled_client.cancel_order_queries), 1)


if __name__ == "__main__":
    unittest.main()
