"""Unit and integration tests for Binance Spot WebSocket L1/L2 & Kline Stream Architecture."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from decision_engine.core.bnb_kline_fetch import fetch_public_klines
from execution.binance_ws_stream import (
    BinanceMarketStreamCache,
    build_combined_stream_urls,
    parse_ws_message,
)
from execution.executors.binance_spot import BinanceSpotAdapter
from execution.executors.binance_spot_gate import PURPOSE_LIVE_BNB_BOLL_FADE, REST_BOOK
from indicators.binance_spot import (
    SpotSymbol,
    bind_ws_kline_close_writer,
    ingest_spot_klines,
    parse_kline_row,
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


class _LatencyTrackingClient:
    """Mock REST client that tracks whether ticker_price and order_book HTTP endpoints were called."""

    def __init__(self) -> None:
        self.ticker_http_calls = 0
        self.depth_http_calls = 0
        self.new_order_calls = 0

    def exchange_info(self, symbol: str) -> dict:
        return _exchange_info(symbol)

    def ticker_price(self, symbol: str) -> dict:
        self.ticker_http_calls += 1
        return {"symbol": symbol, "price": "100.00"}

    def order_book(self, symbol: str, *, limit: int = 20) -> dict:
        self.depth_http_calls += 1
        return {
            "bids": [["99.90", "50.0"]],
            "asks": [["100.10", "50.0"]],
        }

    def new_order(self, signed_query: str, *, api_key: str) -> dict:
        self.new_order_calls += 1
        return {
            "symbol": "SOLUSDT",
            "orderId": 77701,
            "status": "FILLED",
            "executedQty": "1.00",
            "cummulativeQuoteQty": "100.00",
            "fills": [],
        }

    def new_oco(self, signed_query: str, *, api_key: str) -> dict:
        return {
            "orderListId": 88801,
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "orders": [{"orderId": 6001}, {"orderId": 6002}],
            "orderReports": [
                {"orderId": 6001, "type": "LIMIT_MAKER", "status": "NEW"},
                {"orderId": 6002, "type": "STOP_LOSS_LIMIT", "status": "NEW"},
            ],
        }


class BinanceWsStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_live = os.environ.get("BINANCE_SPOT_LIVE_ENABLED")
        os.environ["BINANCE_SPOT_LIVE_ENABLED"] = "1"

    def tearDown(self) -> None:
        if self._old_live is None:
            os.environ.pop("BINANCE_SPOT_LIVE_ENABLED", None)
        else:
            os.environ["BINANCE_SPOT_LIVE_ENABLED"] = self._old_live

    def test_combined_stream_url_chunking_and_message_parsing(self) -> None:
        urls = build_combined_stream_urls(
            ["SOLUSDT", "BTCUSDT"],
            book_ticker=True,
            depth20=True,
            kline_interval="5m",
            max_streams_per_conn=4,
        )
        self.assertEqual(len(urls), 2)
        self.assertIn("solusdt@bookTicker/solusdt@depth20@100ms/solusdt@kline_5m", urls[0])

        # 1. Parse L1 bookTicker
        l1 = parse_ws_message(
            {
                "stream": "solusdt@bookTicker",
                "data": {"u": 12345, "s": "SOLUSDT", "b": "99.95", "B": "12.5", "a": "100.05", "A": "8.0"},
            },
            now_mono_ms=10_000,
        )
        assert l1 is not None
        self.assertEqual(l1.symbol, "SOLUSDT")
        self.assertEqual(l1.mid_price, "100")

        # 2. Parse L2 depth20@100ms
        l2 = parse_ws_message(
            {
                "stream": "solusdt@depth20@100ms",
                "data": {
                    "lastUpdateId": 99999,
                    "bids": [["99.98", "20.0"], ["99.95", "40.0"]],
                    "asks": [["100.02", "25.0"], ["100.05", "50.0"]],
                },
            },
            now_mono_ms=10_010,
        )
        assert l2 is not None
        self.assertEqual(l2.symbol, "SOLUSDT")
        self.assertEqual(len(l2.bids), 2)
        self.assertEqual(l2.asks[0][0], "100.02")

        # 3. Parse Kline 5m with active taker buy volumes
        kl = parse_ws_message(
            {
                "stream": "solusdt@kline_5m",
                "data": {
                    "e": "kline",
                    "E": 1700000299999,
                    "s": "SOLUSDT",
                    "k": {
                        "t": 1700000000000,
                        "T": 1700000299999,
                        "s": "SOLUSDT",
                        "i": "5m",
                        "o": "101.0",
                        "c": "99.5",
                        "h": "101.5",
                        "l": "99.0",
                        "v": "1000.0",
                        "n": 450,
                        "x": True,
                        "q": "100000.0",
                        "V": "620.0",
                        "Q": "62000.0",
                    },
                },
            },
            now_mono_ms=10_020,
        )
        assert kl is not None
        self.assertTrue(kl.is_closed)
        self.assertEqual(kl.taker_buy_quote_volume, 62000.0)

    def test_adapter_uses_ws_l1_and_l2_with_zero_rest_calls_and_falls_back_when_stale(self) -> None:
        cache = BinanceMarketStreamCache(max_age_ms=2500)
        # Ingest fresh L1 and L2 snapshots
        cache.ingest_message(
            {
                "stream": "solusdt@bookTicker",
                "data": {"s": "SOLUSDT", "b": "99.98", "B": "40.0", "a": "100.02", "A": "40.0"},
            },
            now_mono_ms=50_000,
        )
        cache.ingest_message(
            {
                "stream": "solusdt@depth20@100ms",
                "data": {
                    "lastUpdateId": 501,
                    "bids": [["99.98", "50.0"]],
                    "asks": [["100.02", "50.0"]],
                },
            },
            now_mono_ms=50_000,
        )
        # Verify fresh read at +120ms
        l1_hit = cache.get_ticker_price("SOLUSDT", now_mono_ms=50_120)
        assert l1_hit is not None
        self.assertEqual(l1_hit["source"], "ws_l1")
        self.assertEqual(l1_hit["ws_age_ms"], 120)

        # Verify stale miss at +3000ms (> 2500ms threshold)
        self.assertIsNone(cache.get_ticker_price("SOLUSDT", now_mono_ms=53_000))
        self.assertIsNone(cache.get_order_book("SOLUSDT", now_mono_ms=53_000))

        # Re-ingest at current monotonic clock so adapter sees fresh WS data
        cache.ingest_message(
            {
                "stream": "solusdt@depth20@100ms",
                "data": {
                    "lastUpdateId": 502,
                    "bids": [["99.98", "50.0"]],
                    "asks": [["100.02", "50.0"]],
                },
            }
        )
        sig = {
            "decision_log_id": 160001,
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
        client = _LatencyTrackingClient()
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
                ws_cache=cache,
            )
            res = adapter.execute(sig)
            self.assertTrue(res["ok"], res.get("error"))
            # Neither ticker_price nor order_book REST HTTP was called!
            self.assertEqual(client.ticker_http_calls, 0)
            self.assertEqual(client.depth_http_calls, 0)
            self.assertEqual(client.new_order_calls, 1)
            self.assertEqual(res["orderbook_metrics"]["market_data_source"], "ws_l2")
            self.assertEqual(res["mark_telemetry"]["mark_source"], "ws_l1")

            # Now force cache staleness (max_age_ms=0 with future check or empty cache) -> falls back to REST
            empty_cache = BinanceMarketStreamCache(max_age_ms=2500)
            adapter.ws_cache = empty_cache
            sig["decision_log_id"] = 160002
            res_fallback = adapter.execute(sig)
            self.assertTrue(res_fallback["ok"], res_fallback.get("error"))
            self.assertEqual(client.ticker_http_calls, 1)
            self.assertEqual(client.depth_http_calls, 1)
            self.assertEqual(res_fallback["orderbook_metrics"]["market_data_source"], "rest_fallback")
            self.assertEqual(res_fallback["mark_telemetry"]["mark_source"], "rest_fallback")

    def test_ws_kline_stream_populates_indicators_and_decision_fetch_without_rest(self) -> None:
        cache = BinanceMarketStreamCache()
        start_ms = 1_700_000_000_000
        step_ms = 300_000
        raw_bars = []
        for idx in range(29):
            open_ms = start_ms + idx * step_ms
            raw_bars.append(
                parse_kline_row(
                    [
                        open_ms,
                        "100.0",
                        "101.0",
                        "99.0",
                        "100.0",
                        "10.0",
                        open_ms + 299_999,
                        "1000.0",
                        20,
                        "6.0",
                        "600.0",
                    ]
                )
            )
        cache.seed_klines("SOLUSDT", "5m", [b for b in raw_bars if b is not None])

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            conn = sqlite3.connect(db)
            symbols = [SpotSymbol("SOLUSDT", "SOL")]
            bind_ws_kline_close_writer(conn, symbols, cache, interval="5m")

            # Emit 30th closed 5m bar via WebSocket (sharp fade to 90.0 with 65% taker buy ratio)
            last_open = start_ms + 29 * step_ms
            cache.ingest_message(
                {
                    "stream": "solusdt@kline_5m",
                    "data": {
                        "e": "kline",
                        "s": "SOLUSDT",
                        "k": {
                            "t": last_open,
                            "T": last_open + 299_999,
                            "s": "SOLUSDT",
                            "i": "5m",
                            "o": "100.0",
                            "h": "100.5",
                            "l": "89.5",
                            "c": "90.0",
                            "v": "50.0",
                            "q": "4500.0",
                            "n": 100,
                            "x": True,
                            "V": "32.5",
                            "Q": "2925.0",
                        },
                    },
                }
            )
            # Event-driven callback already wrote binance_indicators without any REST call!
            row = conn.execute(
                "SELECT price_usd, boll_lower, taker_buy_ratio FROM binance_indicators WHERE symbol = 'SOLUSDT'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertAlmostEqual(float(row[0]), 90.0, places=4)
            self.assertAlmostEqual(float(row[2]), 0.65, places=4)

            # ingest_spot_klines also hits WS cache with zero REST calls
            def _fail_fetcher(url: str):
                raise AssertionError(f"REST fetcher should not be called on WS hit: {url}")

            stats = ingest_spot_klines(conn, symbols, fetcher=_fail_fetcher, ws_cache=cache)
            self.assertEqual(stats["ws_hits"], 1)
            self.assertEqual(stats["rest_fetches"], 0)

            # fetch_public_klines also hits WS cache directly
            fetched = fetch_public_klines("SOLUSDT", interval="5m", limit=30, ws_cache=cache)
            self.assertEqual(len(fetched), 30)
            self.assertAlmostEqual(fetched[-1][4], 90.0, places=4)
            conn.close()


if __name__ == "__main__":
    unittest.main()
