"""Unit and integration tests for P3 Pre-Trade Orderbook Impact Gate and Post-Trade TCA Report."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from execution.executors.binance_spot import BinanceSpotAdapter
from execution.executors.binance_spot_gate import PURPOSE_LIVE_BNB_BOLL_FADE, REST_BOOK
from execution.executors.binance_spot_protect import evaluate_orderbook_impact
from execution.tca_report import build_tca_report, format_tca_markdown


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


class _OrderbookMockClient:
    def __init__(self, depth_payload: dict) -> None:
        self.depth_payload = depth_payload
        self.new_order_calls = 0

    def exchange_info(self, symbol: str) -> dict:
        return _exchange_info(symbol)

    def ticker_price(self, symbol: str) -> dict:
        return {"symbol": symbol, "price": "100.00"}

    def order_book(self, symbol: str, *, limit: int = 20) -> dict:
        return self.depth_payload

    def new_order(self, signed_query: str, *, api_key: str) -> dict:
        self.new_order_calls += 1
        return {
            "symbol": "SOLUSDT",
            "orderId": 88801,
            "status": "FILLED",
            "executedQty": "1.00",
            "cummulativeQuoteQty": "100.10",
            "fills": [],
        }

    def new_oco(self, signed_query: str, *, api_key: str) -> dict:
        return {
            "orderListId": 99901,
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "orders": [{"orderId": 7001}, {"orderId": 7002}],
            "orderReports": [
                {"orderId": 7001, "type": "LIMIT_MAKER", "status": "NEW"},
                {"orderId": 7002, "type": "STOP_LOSS_LIMIT", "status": "NEW"},
            ],
        }


class OrderbookGateAndTcaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_live = os.environ.get("BINANCE_SPOT_LIVE_ENABLED")
        os.environ["BINANCE_SPOT_LIVE_ENABLED"] = "1"

    def tearDown(self) -> None:
        if self._old_live is None:
            os.environ.pop("BINANCE_SPOT_LIVE_ENABLED", None)
        else:
            os.environ["BINANCE_SPOT_LIVE_ENABLED"] = self._old_live

    def test_evaluate_orderbook_impact_metrics_and_blocks(self) -> None:
        # Healthy tight book: bid 99.95, ask 100.05 (spread 10 bps), deep liquidity
        healthy_book = {
            "bids": [["99.95", "50.0"], ["99.90", "100.0"]],
            "asks": [["100.05", "50.0"], ["100.10", "100.0"]],
        }
        metrics, err = evaluate_orderbook_impact(healthy_book, quote_usdt="100.0")
        self.assertIsNone(err)
        assert metrics is not None
        self.assertAlmostEqual(metrics["spread_bps"], 10.0, places=2)
        self.assertAlmostEqual(metrics["impact_bps"], 5.0, places=2)
        self.assertTrue(metrics["depth_covered"])

        # Wide spread book: bid 99.00, ask 100.00 (spread ~100.5 bps > 35 bps cap)
        wide_book = {
            "bids": [["99.00", "50.0"]],
            "asks": [["100.00", "50.0"]],
        }
        _m, wide_err = evaluate_orderbook_impact(wide_book, quote_usdt="100.0")
        self.assertEqual(wide_err, "orderbook_spread_too_wide")

        # Thin ask book: ask1 has only $10 at 100.05, ask2 is at 102.00 (+200 bps impact)
        thin_book = {
            "bids": [["99.95", "50.0"]],
            "asks": [["100.05", "0.10"], ["102.00", "50.0"]],
        }
        _m2, impact_err = evaluate_orderbook_impact(thin_book, quote_usdt="100.0")
        self.assertEqual(impact_err, "orderbook_impact_too_high")

    def test_adapter_blocks_market_buy_on_wide_spread_or_thin_book_and_allows_healthy(self) -> None:
        sig = {
            "decision_log_id": 150001,
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
        # 1. Thin book -> blocked before new_order
        thin_client = _OrderbookMockClient(
            {
                "bids": [["99.95", "50.0"]],
                "asks": [["100.05", "0.10"], ["102.00", "50.0"]],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            adapter = BinanceSpotAdapter(
                client=thin_client,
                kill_switch_path=Path(tmp) / "NO_KILL",
                api_key="k",
                api_secret="s",
                account_snapshot=_account_snapshot(),
                production_book=REST_BOOK,
                rest_expected={"account_type": "SPOT", "usdt_free": "500"},
                orderbook_gate=True,
            )
            res_blocked = adapter.execute(sig)
            self.assertFalse(res_blocked["ok"])
            self.assertFalse(res_blocked["submitted"])
            self.assertEqual(res_blocked["error"], "orderbook_impact_too_high")
            self.assertEqual(thin_client.new_order_calls, 0)
            self.assertIn("orderbook_metrics", res_blocked)

            # 2. Healthy book -> passes orderbook gate and places order + OCO
            healthy_client = _OrderbookMockClient(
                {
                    "bids": [["99.95", "50.0"]],
                    "asks": [["100.05", "50.0"]],
                }
            )
            adapter.client = healthy_client
            res_ok = adapter.execute(sig)
            self.assertTrue(res_ok["ok"], res_ok.get("error"))
            self.assertTrue(res_ok["submitted"])
            self.assertEqual(healthy_client.new_order_calls, 1)
            self.assertIn("orderbook_metrics", res_ok)

    def test_post_trade_tca_report_computes_shortfall_protection_and_win_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_db = Path(tmp) / "execution_ledger.db"
            conn = sqlite3.connect(ledger_db)
            conn.execute(
                """
                CREATE TABLE binance_spot_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_log_id INTEGER,
                    purpose TEXT,
                    symbol TEXT,
                    status TEXT,
                    protection TEXT,
                    quantity TEXT,
                    entry_price TEXT,
                    stop_price TEXT,
                    take_profit_price TEXT,
                    exit_reason TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO binance_spot_positions (
                    decision_log_id, purpose, symbol, status, protection,
                    quantity, entry_price, stop_price, take_profit_price, exit_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    # 1. SOLUSDT: signal 100.0, filled at 100.15 (+15 bps IS), closed at TP 103.15 (+299.55 bps gross)
                    (101, "live_bnb_boll_fade", "SOLUSDT", "oco_filled", "exchange_oco", "1.0", "100.15", "98.0", "103.15", "take_profit"),
                    # 2. DASHUSDT: signal 48.10, filled at 48.02 (-16.63 bps price improvement), re-anchored OCO working
                    (102, "live_bnb_boll_fade", "DASHUSDT", "oco_working", "exchange_oco", "2.0", "48.02", "47.78", "49.50", None),
                    # 3. FETUSDT: auto-flattened on OCO failure
                    (103, "live_bnb_boll_fade", "FETUSDT", "closed", "auto_flatten", "10.0", "1.50", "1.45", "1.60", "oco_failed_auto_flatten"),
                ],
            )
            conn.execute(
                """
                CREATE TABLE execution_log (
                    decision_log_id INTEGER,
                    executed INTEGER,
                    receipt_json TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO execution_log VALUES (?, ?, ?)",
                [
                    (
                        101,
                        1,
                        json.dumps(
                            {
                                "submitted": True,
                                "executed": 1,
                                "exit_plan": {"entry_price": "100.00"},
                                "orderbook_metrics": {"spread_bps": 8.0, "impact_bps": 12.0},
                            }
                        ),
                    ),
                    (
                        102,
                        1,
                        json.dumps(
                            {
                                "submitted": True,
                                "executed": 1,
                                "exit_plan": {"entry_price": "48.10"},
                                "arm_position": {"signal_entry_price": "48.10", "oco_reanchored": True},
                                "orderbook_metrics": {"spread_bps": 14.0, "impact_bps": 22.0},
                            }
                        ),
                    ),
                    (
                        103,
                        0,
                        json.dumps(
                            {
                                "submitted": False,
                                "executed": 0,
                                "error": "oco_failed_auto_flattened:oco_rejected:binance_http_error:400:-2010:Account has insufficient balance",
                                "exchange_error": {"status": 400, "code": -2010, "msg": "Account has insufficient balance"},
                            }
                        ),
                    ),
                ],
            )
            conn.commit()
            conn.close()

            report = build_tca_report(ledger_path=ledger_db, round_trip_fee_bps=20.0)
            self.assertEqual(report["protection_audit"]["oco_working"], 1)
            self.assertEqual(report["protection_audit"]["oco_filled"], 1)
            self.assertEqual(report["protection_audit"]["oco_reanchored_saves"], 1)
            self.assertEqual(report["protection_audit"]["auto_flatten_rescues"], 1)
            self.assertTrue(report["protection_audit"]["zero_naked_exposure"])
            self.assertEqual(report["implementation_shortfall_bps"]["n"], 2)
            self.assertEqual(report["realized_performance"]["take_profit_count"], 1)
            self.assertEqual(report["realized_performance"]["win_rate"], 1.0)
            self.assertEqual(report["execution_funnel"]["exchange_error_codes"].get("-2010"), 1)
            md = format_tca_markdown(report)
            self.assertIn("Implementation Shortfall", md)
            self.assertIn("Zero Naked Exposure", md)


if __name__ == "__main__":
    unittest.main()
