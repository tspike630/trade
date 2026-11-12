"""Strategy size_pct through the live arm with paper off. No live HTTP.

Proves the current chain: entry-zone buy has no quote qty, the execution
book turns size_pct into quoteOrderQty, the 20 USDT floor still refuses a
small plan, and the held-position loop reaches TP/SL and OCO with
paper_enabled false.
"""

from __future__ import annotations

import json
import os
import sqlite3
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

from decision_engine.core.signal_bus import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    SignalBus,
)
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy  # noqa: E402
from decision_engine.tests.test_live_bnb_entry_zone import _row  # noqa: E402
from execution.executors.binance_spot import (  # noqa: E402
    BinanceRequestError,
    BinanceSpotAdapter,
    _order_pairs,
)
from execution.executors.binance_spot_protect import (  # noqa: E402
    ENTRY_MIN_NOTIONAL_USDT,
    resolve_entry_size,
)
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.run import tick_live_bnb_exits  # noqa: E402

_INFO = {
    "symbol": "BNBUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "isSpotTradingAllowed": True,
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "900000", "stepSize": "0.01"},
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
        {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
    ],
}


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.oco = True

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _INFO

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        if "side=SELL" in signed_query:
            return {"orderId": "77", "status": "FILLED", "executedQty": "1.23"}
        return {"orderId": "99", "status": "FILLED", "executedQty": "1.239"}

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        if not self.oco:
            raise BinanceRequestError(400)
        return {
            "orderListId": 42,
            "listOrderStatus": "EXECUTING",
            "listStatusType": "EXEC_STARTED",
            "orderReports": [
                {"orderId": 11, "type": "LIMIT_MAKER", "status": "NEW", "price": "110", "executedQty": "0"},
                {
                    "orderId": 10,
                    "type": "STOP_LOSS_LIMIT",
                    "status": "NEW",
                    "price": "98.99",
                    "stopPrice": "99",
                    "executedQty": "0",
                },
            ],
        }

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": "102"}

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order_list", signed_query, api_key))
        if not self.oco:
            raise BinanceRequestError(400)
        return {
            "orderListId": 42,
            "listOrderStatus": "EXECUTING",
            "listStatusType": "EXEC_STARTED",
            "orderReports": [
                {"orderId": 11, "type": "LIMIT_MAKER", "status": "NEW", "executedQty": "0"},
                {"orderId": 10, "type": "STOP_LOSS_LIMIT", "status": "NEW", "executedQty": "0"},
            ],
        }


def _signal() -> dict[str, Any]:
    row = _row(0.20, 1.6)
    row["primary_pair_address"] = "BNBUSDT"
    signals = TechBollFadeStrategy().on_tick(
        {"hot_tokens": [row], "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
    )
    if len(signals) != 1:
        raise AssertionError(signals)
    return signals[0]


def _env(root: Path, book: str) -> dict[str, str]:
    return {
        "BINANCE_SPOT_LIVE_ENABLED": "1",
        "BINANCE_SPOT_BOOK_USDT": book,
        "BINANCE_SPOT_TAKER_FEE_BPS": "10",
        "LIVE_KILL_SWITCH_PATH": str(root / "absent-kill"),
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "LIVE_EXCHANGE_API_KEY": "",
        "LIVE_EXCHANGE_API_SECRET": "",
    }


class SizeChainTests(unittest.TestCase):
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
            kill_switch_path=self.root / "absent-kill",
            auto_flatten_on_oco_fail=True,
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
        self.signal = _signal()
        self.assertNotIn("quote_order_qty", self.signal)
        self.assertNotIn("quantity", self.signal)
        self.assertAlmostEqual(self.signal["size_pct"], 0.05)
        self.assertAlmostEqual(self.signal["stop_price"], 99.0)
        self.assertAlmostEqual(self.signal["take_profit_price"], 110.0)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _publish_and_run(self, book: str) -> tuple[ExecutionGateway, dict[str, Any]]:
        db = self.root / "dexscreener.db"
        with SignalBus(db, dry_run=False, purpose=PURPOSE_LIVE_BNB_BOLL_FADE) as bus:
            bus.publish(self.signal)
        wrote = sqlite3.connect(db)
        wrote.execute("UPDATE decision_log SET id = 147841")
        wrote.commit()
        wrote.close()
        gw = ExecutionGateway(
            decision_db=db,
            ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
            paper_enabled=False,
            pm_enabled=False,
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            binance_spot=self.adapter,
        )
        gw.__enter__()
        self.assertFalse(gw.paper_enabled)
        stats = gw.process_pending()
        self.assertEqual(stats.processed, 1)
        return gw, stats.receipts[0]

    def test_small_book_hits_the_20u_floor_and_does_not_order(self) -> None:
        # 0.05 * 300 = 15 < 20. The floor is on the sized quote, not a second cap.
        with mock.patch.dict(os.environ, _env(self.root, "300")):
            gw, receipt = self._publish_and_run("300")
            gw.__exit__(None, None, None)
        self.assertEqual(receipt["error"], "entry_notional_below_min")
        self.assertEqual(receipt["executed"], 0)
        self.assertEqual(self.client.calls, [])
        plan = receipt["exec_result"]["size_plan"]
        self.assertEqual(plan["source"], "size_pct")
        self.assertEqual(plan["quote_usdt"], "15")

    def test_size_pct_becomes_quote_and_the_loop_covers_tp_sl(self) -> None:
        with mock.patch.dict(os.environ, _env(self.root, "1000")):
            gw, receipt = self._publish_and_run("1000")
            try:
                self.assertEqual(receipt["executed"], 1, receipt)
                plan = receipt["exec_result"]["size_plan"]
                self.assertEqual(plan["quote_usdt"], "50")
                self.assertEqual(plan["size_pct"], "0.05")
                buy = parse_qs(
                    next(call[1] for call in self.client.calls if call[0] == "new_order")
                )
                self.assertEqual(buy["side"], ["BUY"])
                self.assertEqual(buy["quoteOrderQty"], ["50"])
                self.assertNotIn("quantity", buy)
                # Inside the strategy stop 99 and take-profit 110: exchange OCO.
                resting = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "100"}
                )
                self.assertEqual(resting[0]["protection"], "exchange_oco", resting[0])
                oco = parse_qs(next(c[1] for c in self.client.calls if c[0] == "new_oco"))
                self.assertEqual(oco["abovePrice"], ["110"])
                self.assertEqual(oco["belowStopPrice"], ["99"])
                self.assertEqual(oco["quantity"], ["1.23"])
                held = gw.ledger.binance_spot_position_by_decision(147841)
                assert held is not None
                self.assertEqual(held["residual_qty"], "0.009")
                self.assertEqual(held["status"], "oco_working")
                # Stop print must not add a market sell over the resting list.
                again = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "98"}
                )
                self.assertEqual(again[0]["position_status"], "oco_working")
                self.assertFalse(
                    any(c[0] == "new_order" and "side=SELL" in c[1] for c in self.client.calls)
                )
            finally:
                gw.__exit__(None, None, None)

    def test_oco_reject_falls_back_to_the_stop_with_paper_off(self) -> None:
        self.client.oco = False
        with mock.patch.dict(os.environ, _env(self.root, "1000")):
            gw, receipt = self._publish_and_run("1000")
            try:
                self.assertEqual(receipt["executed"], 0, receipt)
                self.assertFalse(receipt["exec_result"]["consumer_allowed"])
                stopped = tick_live_bnb_exits(
                    gw, frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}), {"BNBUSDT": "98"}
                )
            finally:
                gw.__exit__(None, None, None)
        self.assertEqual(receipt["exec_result"]["protection_state"], "dust_residual", receipt)
        self.assertTrue(
            any(c[0] == "new_order" and "side=SELL" in c[1] for c in self.client.calls)
        )
        self.assertEqual(stopped, [])

    def test_long_book_quote_is_sent_at_exchange_precision(self) -> None:
        # 0.05 * 437.63809903 = 21.8819049515. Binance -1111 rejects >8 dp.
        book = "437.63809903"
        with mock.patch.dict(os.environ, _env(self.root, book)):
            gw, receipt = self._publish_and_run(book)
            gw.__exit__(None, None, None)
        self.assertIsNone(receipt["error"], receipt)
        self.assertEqual(receipt["executed"], 1, receipt)
        plan = receipt["exec_result"]["size_plan"]
        self.assertEqual(plan["quote_usdt"], "21.88190495")
        self.assertEqual(plan["size_pct"], "0.05")
        buy = parse_qs(next(call[1] for call in self.client.calls if call[0] == "new_order"))
        self.assertEqual(buy["side"], ["BUY"])
        self.assertEqual(buy["type"], ["MARKET"])
        self.assertEqual(buy["quoteOrderQty"], ["21.88190495"])
        self.assertLessEqual(len(buy["quoteOrderQty"][0].split(".")[1]), 8)


class QuotePrecisionTests(unittest.TestCase):
    def test_half_size_stays_under_the_20u_floor(self) -> None:
        # Neutral half size: 0.025 * 437.63809903 = 10.94095247575 < 20.
        sized, error, plan = resolve_entry_size(
            {"action": "buy", "size_pct": "0.025"},
            {},
            environ={"BINANCE_SPOT_BOOK_USDT": "437.63809903"},
        )
        self.assertEqual(error, "entry_notional_below_min")
        self.assertNotIn("quote_order_qty", sized)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan["quote_usdt"], "10.94095247575")
        self.assertLess(Decimal(plan["quote_usdt"]), ENTRY_MIN_NOTIONAL_USDT)
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, Decimal("20"))

    def test_market_quote_floors_the_long_product(self) -> None:
        sized, error, plan = resolve_entry_size(
            {"action": "buy", "size_pct": "0.05"},
            {},
            environ={"BINANCE_SPOT_BOOK_USDT": "437.63809903"},
        )
        self.assertIsNone(error)
        self.assertEqual(sized["quote_order_qty"], "21.88190495")
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan["quote_usdt"], "21.88190495")
        self.assertEqual(plan["order_type"], "MARKET")

    def test_order_pairs_sends_exchange_ready_quote_and_limit_qty(self) -> None:
        market, market_error = _order_pairs(
            symbol="BNBUSDT",
            side="BUY",
            order_type="MARKET",
            extras={"quote_order_qty": "21.8819049515"},
            client_id=None,
            timestamp_ms=1_700_000_000_000,
        )
        self.assertIsNone(market_error)
        assert market is not None
        self.assertEqual(dict(market)["quoteOrderQty"], "21.88190495")

        short, short_error = _order_pairs(
            symbol="BNBUSDT",
            side="BUY",
            order_type="MARKET",
            extras={"quote_order_qty": "21.88"},
            client_id=None,
            timestamp_ms=1_700_000_000_000,
        )
        self.assertIsNone(short_error)
        assert short is not None
        self.assertEqual(dict(short)["quoteOrderQty"], "21.88")

        sized, error, _plan = resolve_entry_size(
            {"action": "buy", "size_pct": "0.05"},
            {"order_type": "LIMIT", "limit_price": "600"},
            environ={"BINANCE_SPOT_BOOK_USDT": "437.63809903"},
        )
        self.assertIsNone(error)
        self.assertEqual(sized["quantity"], "0.03646984")
        self.assertNotIn("quote_order_qty", sized)
        limit, limit_error = _order_pairs(
            symbol="BNBUSDT",
            side="BUY",
            order_type="LIMIT",
            extras=sized,
            client_id=None,
            timestamp_ms=1_700_000_000_000,
        )
        self.assertIsNone(limit_error)
        assert limit is not None
        self.assertEqual(dict(limit)["quantity"], "0.03646984")
        self.assertNotIn("quoteOrderQty", dict(limit))
        self.assertLessEqual(len(dict(limit)["quantity"].split(".")[1]), 8)


if __name__ == "__main__":
    unittest.main()
