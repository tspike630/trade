"""Public-feed collector: one shot, loop, source time, top-5 OBI, PEPE errors.

No network, no orders, no API keys. volume_z is not produced here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from decision_engine.core.perp_microstructure_and_beta_hedge import (
    attach_counterparty_context,
)
from scripts import fetch_public_derivatives_and_counterparty as feed


def _bars(close_ts: int = 4000) -> list[list[object]]:
    closed = [close_ts - 60_000, "10", "11", "9", "10.5", "2", close_ts, "21", 3, "1", "10", "0"]
    forming = [close_ts, "10.5", "11", "10", "10.8", "2", close_ts + 60_000, "22", 3, "1", "11", "0"]
    return [closed, forming]


def _book() -> dict[str, object]:
    # Payload order is not best-first. Top-20 keeps this prefix. Top-5 sorts.
    return {
        "lastUpdateId": 100,
        "bids": [["9", "100"], ["10", "1"], ["10", "1"], ["10", "1"], ["10", "1"], ["10", "1"]],
        "asks": [["12", "50"], ["11", "1"], ["11", "1"], ["11", "1"], ["11", "1"], ["11", "1"]],
    }


_MATCH_EVENT = {"e": "depthUpdate", "E": 5000, "s": "PEPEUSDT", "U": 101, "u": 110, "b": [], "a": []}
_GAP_EVENTS = [
    {"e": "depthUpdate", "E": 99999, "s": "PEPEUSDT", "U": 200, "u": 210, "b": [], "a": []},
    {"e": "depthUpdate", "E": 5000, "s": "PEPEUSDT", "U": 101, "u": 110, "b": [], "a": []},
]


class PublicFeedCollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            name: getattr(feed, name)
            for name in ("_HTTP_EXCHANGE", "_SLEEP", "_CLOCK", "_DEPTH_EVENT_ITER")
        }
        self.clock = {"t": 9.0}
        self.sleeps: list[float] = []
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.depth_mode = "match"
        self.raise_symbol: str | None = None
        self.stable_ts: int | None = 8000
        self._env = {
            key: os.environ.get(key)
            for key in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_SECRET")
        }
        os.environ["BINANCE_API_KEY"] = "test-key-should-not-leak"
        os.environ["BINANCE_API_SECRET"] = "test-secret-should-not-leak"
        feed._CLOCK = lambda: self.clock["t"]
        feed._SLEEP = self.sleeps.append
        feed._HTTP_EXCHANGE = self._exchange
        feed._DEPTH_EVENT_ITER = lambda symbol, timeout: self._depth_events()

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            setattr(feed, name, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _depth_events(self) -> list[dict[str, object]]:
        if self.depth_mode == "gap":
            return list(_GAP_EVENTS)
        if self.depth_mode == "missing":
            return []
        return [dict(_MATCH_EVENT)]

    def _exchange(self, url: str, params: dict[str, object] | None, timeout: float, accept: str):
        del timeout, accept
        self.calls.append((url, dict(params or {})))
        secret = "test-key-should-not-leak"
        self.assertNotIn(secret, url)
        self.assertNotIn("test-secret-should-not-leak", url)
        self.assertNotIn("/api/v3/order", url)
        self.assertNotIn("newOrder", url)
        joined = json.dumps(params or {})
        self.assertNotIn(secret, joined)
        if "premiumIndex" in url:
            symbol = str((params or {}).get("symbol") or "")
            if symbol == self.raise_symbol:
                raise RuntimeError("symbol fetch crashed")
            if symbol == "PEPEUSDT":
                body = b'{"code":-1121,"msg":"Invalid symbol."}'
                return (400, body, "Invalid symbol.", "-1121", False)
            body = json.dumps(
                {
                    "symbol": symbol,
                    "markPrice": "0.01000000",
                    "indexPrice": "0.01000000",
                    "lastFundingRate": "0.0001",
                    "time": 111000,
                }
            ).encode()
            return (200, body, None, None, False)
        if "/fapi/" in url:
            return (200, b"[]", None, None, False)
        if "/api/v3/klines" in url:
            return (200, json.dumps(_bars()).encode(), None, None, False)
        if "/api/v3/depth" in url:
            return (200, json.dumps(_book()).encode(), None, None, False)
        if "/api/v3/aggTrades" in url:
            return (200, b"[]", None, None, False)
        if "/api/v3/ticker/price" in url:
            return (200, b'{"price":"10.5"}', None, None, False)
        if "okx.com" in url:
            return (200, b'{"code":"0","data":[{"last":"10.4","ts":"7000"}]}', None, None, False)
        if "bybit.com" in url:
            return (200, b'{"result":{"list":[{"lastPrice":"10.6"}]}}', None, None, False)
        if "get_volatility_index_data" in url:
            return (200, b'{"result":{"data":[[6000,48,52,47,50]]}}', None, None, False)
        if "get_index_price" in url:
            return (200, b'{"result":{"index_price":10.2}}', None, None, False)
        if "get_book_summary_by_currency" in url:
            return (200, b'{"result":[]}', None, None, False)
        if "fredgraph.csv" in url:
            return (200, b"DATE,VALUE\n2026-11-01,6800000\n", None, None, False)
        if "stablecoins.llama.fi" in url:
            payload: dict[str, object] = {
                "peggedAssets": [
                    {
                        "symbol": "USDT",
                        "circulating": {"peggedUSD": 10},
                        "circulatingPrevDay": {"peggedUSD": 9},
                        "circulatingPrevWeek": {"peggedUSD": 8},
                    }
                ]
            }
            if self.stable_ts is not None:
                payload["updatedAt"] = self.stable_ts
            return (200, json.dumps(payload).encode(), None, None, False)
        return (404, b'{"code":-1,"msg":"unstubbed"}', "unstubbed", "unstubbed", False)

    def _collect(self, db: Path, **kwargs: object) -> dict[str, object]:
        args = {
            "symbols": ["1000PEPEUSDT"],
            "spot_symbols": ["PEPEUSDT"],
            "cross_symbols": ["BTCUSDT"],
            "option_currencies": ["BTC"],
        }
        args.update(kwargs)
        return feed.collect_all_to_sqlite(db, **args)

    def test_top5_notional_differs_from_top20_and_fails_closed(self) -> None:
        metrics = feed.compute_spot_orderbook_and_aggtrades_metrics(_book(), [])
        self.assertAlmostEqual(metrics["obi_top5"], round((50 - 55) / 105, 4))
        self.assertAlmostEqual(metrics["orderbook_imbalance_20"], round((950 - 655) / (950 + 655), 4))
        self.assertNotEqual(metrics["obi_top5"], metrics["orderbook_imbalance_20"])
        self.assertIsNone(metrics["obi_top5_error"])

        short = feed.compute_top_n_notional_imbalance(
            [["10", "1"], ["9", "1"]],
            [["11", "1"]] * 5,
        )
        self.assertIsNone(short["imbalance"])
        self.assertEqual(short["error"], "obi_top5_levels_missing")
        # The 20-level field is a different metric and keeps its name.
        kept = feed.compute_spot_orderbook_and_aggtrades_metrics(
            {"bids": [["10", "1"], ["9", "1"]], "asks": [["11", "1"], ["12", "1"]]},
            [],
        )
        self.assertIsNotNone(kept["orderbook_imbalance_20"])
        self.assertIsNone(kept["obi_top5"])
        self.assertEqual(kept["obi_top5_error"], "obi_top5_levels_missing")

        imbalance, error = feed.notional_imbalance_result(5, 5, 0.0, 0.0)
        self.assertIsNone(imbalance)
        self.assertEqual(error, "obi_top5_denominator_zero")
        missing, missing_error = feed.notional_imbalance_result(4, 5, 1.0, 1.0)
        self.assertIsNone(missing)
        self.assertEqual(missing_error, "obi_top5_levels_missing")

    def test_depth_source_time_rejects_a_gap_before_a_later_event(self) -> None:
        self.assertEqual(feed.resolve_depth_source_ts(100, [_MATCH_EVENT]), (5000, None))
        self.assertEqual(feed.resolve_depth_source_ts(100, _GAP_EVENTS), (None, "obi_top5_source_ts_gap"))
        self.assertEqual(feed.resolve_depth_source_ts(100, []), (None, "obi_top5_source_ts_missing"))

    def test_single_run_uses_source_time_and_keeps_it_on_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "public_derivatives.db"
            stats = self._collect(db)
            self.assertEqual(feed.round_exit_code(stats), 0)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            perp = conn.execute(
                "SELECT * FROM binance_perp_counterparty WHERE symbol='1000PEPEUSDT'"
            ).fetchall()
            self.assertEqual(len(perp), 1)
            self.assertEqual(perp[0]["ts_ms"], 111000)
            self.assertEqual(perp[0]["source_ts_ms"], 111000)
            self.assertEqual(perp[0]["collected_at_ms"], 9000)
            self.assertEqual(perp[0]["strategy_symbol"], "PEPEUSDT")
            self.assertEqual(perp[0]["price_scale"], 1000)
            self.assertAlmostEqual(perp[0]["mark_price"], 0.01)
            spot = conn.execute(
                "SELECT * FROM binance_spot_microstructure WHERE symbol='PEPEUSDT'"
            ).fetchone()
            self.assertIsNotNone(spot)
            self.assertEqual(spot["ts_ms"], 5000)
            self.assertEqual(spot["obi_top5_source_ts_ms"], 5000)
            self.assertEqual(spot["collected_at_ms"], 9000)
            self.assertEqual(spot["kline_5m_close_ts_ms"], 4000)
            self.assertAlmostEqual(spot["obi_top5"], round((50 - 55) / 105, 4))
            self.assertAlmostEqual(spot["orderbook_imbalance_20"], round((950 - 655) / (950 + 655), 4))
            self.assertIsNone(spot["obi_top5_error"])
            stable = conn.execute("SELECT ts_ms, source_ts_ms, collected_at_ms FROM stablecoin_supply").fetchone()
            self.assertEqual(stable["ts_ms"], 8000)
            self.assertEqual(stable["source_ts_ms"], 8000)
            self.assertEqual(stable["collected_at_ms"], 9000)
            gamma = conn.execute("SELECT ts_ms, source_ts_ms FROM deribit_options_gamma").fetchone()
            self.assertEqual(gamma["ts_ms"], 6000)
            cross = conn.execute("SELECT ts_ms, source_ts_ms FROM cross_exchange_spot_baseline").fetchone()
            self.assertEqual(cross["ts_ms"], 7000)
            conn.close()

            self.clock["t"] = 9.5
            again = self._collect(db)
            self.assertEqual(feed.round_exit_code(again), 0)
            conn = sqlite3.connect(db)
            rows = conn.execute(
                "SELECT ts_ms, collected_at_ms FROM binance_perp_counterparty WHERE symbol='1000PEPEUSDT'"
            ).fetchall()
            spots = conn.execute(
                "SELECT ts_ms, collected_at_ms, obi_top5_source_ts_ms FROM binance_spot_microstructure"
            ).fetchall()
            conn.close()
            self.assertEqual(rows, [(111000, 9500)])
            self.assertEqual(spots, [(5000, 9500, 5000)])

    def test_pepe_invalid_symbol_is_recorded_and_one_shot_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "public_derivatives.db"
            code = feed.main(
                ["--db", str(db), "--symbols", "PEPEUSDT,1000PEPEUSDT", "--spot-symbols", "PEPEUSDT"]
            )
            self.assertEqual(code, 2)
            conn = sqlite3.connect(db)
            symbols = [row[0] for row in conn.execute("SELECT symbol FROM binance_perp_counterparty")]
            errors = conn.execute(
                "SELECT symbol, error_code, message, http_status FROM collector_fetch_errors WHERE symbol='PEPEUSDT'"
            ).fetchall()
            conn.close()
        self.assertIn("1000PEPEUSDT", symbols)
        self.assertNotIn("PEPEUSDT", symbols)
        self.assertTrue(errors)
        self.assertEqual(errors[0][1], "-1121")
        self.assertIn("Invalid symbol", errors[0][2])
        self.assertEqual(errors[0][3], 400)

    def test_symbol_crash_does_not_drop_the_next_symbol(self) -> None:
        self.raise_symbol = "BTCUSDT"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "public_derivatives.db"
            stats = self._collect(db, symbols=["BTCUSDT", "ETHUSDT"], spot_symbols=[])
            self.assertEqual(feed.round_exit_code(stats), 2)
            conn = sqlite3.connect(db)
            symbols = [row[0] for row in conn.execute("SELECT symbol FROM binance_perp_counterparty")]
            messages = [row[0] for row in conn.execute(
                "SELECT message FROM collector_fetch_errors WHERE symbol='BTCUSDT'"
            )]
            conn.close()
        self.assertEqual(symbols, ["ETHUSDT"])
        self.assertTrue(any("symbol fetch crashed" in message for message in messages))

    def test_gap_does_not_publish_top5_or_stamp_refresh_time(self) -> None:
        self.depth_mode = "gap"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "public_derivatives.db"
            stats = self._collect(db)
            self.assertEqual(feed.round_exit_code(stats), 2)
            conn = sqlite3.connect(db)
            spot = conn.execute(
                "SELECT ts_ms, collected_at_ms, obi_top5, obi_top5_error, obi_top5_source_ts_ms, orderbook_imbalance_20 FROM binance_spot_microstructure"
            ).fetchone()
            conn.close()
        self.assertEqual(spot[0], 4000)
        self.assertEqual(spot[1], 9000)
        self.assertIsNone(spot[2])
        self.assertEqual(spot[3], "obi_top5_source_ts_gap")
        self.assertIsNone(spot[4])
        self.assertIsNotNone(spot[5])
        self.assertNotIn(99999, spot)
        self.assertNotIn(5000, (spot[0], spot[4]))

    def test_stablecoin_without_source_time_is_not_written(self) -> None:
        self.stable_ts = None
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "public_derivatives.db"
            stats = self._collect(db)
            self.assertEqual(feed.round_exit_code(stats), 2)
            self.assertTrue(any(err["error_code"] == "stablecoin_source_ts_unavailable" for err in stats["errors"]))
            conn = sqlite3.connect(db)
            count = conn.execute("SELECT COUNT(*) FROM stablecoin_supply").fetchone()[0]
            conn.close()
        self.assertEqual(count, 0)

    def test_timeout_retries_with_backoff_and_http_400_does_not(self) -> None:
        attempts = {"n": 0}

        def flaky(url, params, timeout, accept):
            del url, params, timeout, accept
            attempts["n"] += 1
            if attempts["n"] < 3:
                return (None, None, "timeout:test", "timeout", True)
            return (200, b'{"time": 1}', None, None, False)

        feed._HTTP_EXCHANGE = flaky
        payload = feed._http_get_json("https://example.test/premium")
        self.assertEqual(payload, {"time": 1})
        self.assertEqual(self.sleeps, [feed.HTTP_BACKOFF_BASE_SEC, feed.HTTP_BACKOFF_BASE_SEC * 2])

        self.sleeps.clear()

        def rejected(url, params, timeout, accept):
            del url, params, timeout, accept
            return (400, b'{"code":-1121,"msg":"Invalid symbol."}', "Invalid symbol.", "-1121", False)

        feed._HTTP_EXCHANGE = rejected
        self.assertIsNone(feed._http_get_json("https://example.test/premium"))
        self.assertEqual(self.sleeps, [])

    def test_loop_failure_backoff_grows_when_interval_is_300(self) -> None:
        calls = {"n": 0}

        def collect(db, **kwargs):
            del db, kwargs
            calls["n"] += 1
            return {"errors": [{"fails_round": True, "message": "round failed"}]}

        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as tmp:
            code = feed.run_collection(
                Path(tmp) / "public_derivatives.db",
                symbols=["ETHUSDT"],
                spot_symbols=[],
                loop=True,
                interval=300,
                max_rounds=3,
                collect=collect,
                sleep=sleeps.append,
            )
        self.assertEqual(calls["n"], 3)
        self.assertEqual(code, feed.PARTIAL_EXIT)
        self.assertGreaterEqual(len(sleeps), 2)
        self.assertGreater(sleeps[0], 300.0)
        self.assertGreater(sleeps[1], sleeps[0])

    def test_depth_gap_refetches_snapshot_before_publishing_top5(self) -> None:
        """A later diff event must not cover U > lastUpdateId+1. Re-GET depth."""
        stale = {
            "lastUpdateId": 100,
            "bids": [["10", "5"]] * 5,
            "asks": [["11", "1"]] * 5,
        }
        fresh = {
            "lastUpdateId": 419,
            "bids": [["10", "1"]] * 5,
            "asks": [["11", "5"]] * 5,
        }
        order: list[str] = []
        depth_calls = {"n": 0}

        def exchange(url, params, timeout, accept):
            del params, timeout, accept
            if "/api/v3/depth" in url:
                order.append("depth")
                depth_calls["n"] += 1
                body = fresh if depth_calls["n"] >= 2 else stale
                return (200, json.dumps(body).encode(), None, None, False)
            if "aggTrades" in url:
                order.append("aggTrades")
                return (200, b"[]", None, None, False)
            if "/api/v3/klines" in url:
                return (200, json.dumps(_bars()).encode(), None, None, False)
            return (200, b"{}", None, None, False)

        def events(symbol, timeout):
            del symbol, timeout
            order.append("stream")
            return [
                {"U": 400, "u": 430, "E": 5555},
                {"U": 101, "u": 200, "E": 9999},
            ]

        feed._HTTP_EXCHANGE = exchange
        feed._DEPTH_EVENT_ITER = events
        snap = feed.fetch_binance_spot_microstructure_snapshot("PEPEUSDT")
        self.assertIsNotNone(snap)
        assert snap is not None
        expected = feed.compute_spot_orderbook_and_aggtrades_metrics(fresh, [])
        self.assertEqual(snap["obi_top5_source_ts_ms"], 5555)
        self.assertEqual(snap["book_update_id"], 419)
        self.assertAlmostEqual(snap["obi_top5"], expected["obi_top5"])
        self.assertNotEqual(snap["obi_top5"], feed.compute_spot_orderbook_and_aggtrades_metrics(stale, [])["obi_top5"])
        self.assertGreaterEqual(depth_calls["n"], 2)
        self.assertLess(order.index("stream"), order.index("depth"))
        self.assertGreater(order.index("aggTrades"), order.index("depth"))
        decision = feed.depth_sync_decision(
            100,
            [
                {"U": 400, "u": 430, "E": 5555},
                {"U": 101, "u": 200, "E": 9999},
            ],
        )
        self.assertEqual(decision, ("gap", None))

    def test_loop_continues_after_a_round_crash(self) -> None:
        calls = {"n": 0}

        def collect(db, **kwargs):
            del db, kwargs
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("round down")
            return {"errors": []}

        with tempfile.TemporaryDirectory() as tmp:
            code = feed.run_collection(
                Path(tmp) / "public_derivatives.db",
                symbols=["ETHUSDT"],
                spot_symbols=[],
                loop=True,
                interval=1,
                max_rounds=2,
                collect=collect,
                sleep=self.sleeps.append,
            )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(code, 0)
        self.assertTrue(self.sleeps)

    def test_second_process_lock_is_busy_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "public_derivatives.db"
            held = feed.acquire_collector_lock(db)
            try:
                self.assertEqual(
                    feed.run_collection(db, symbols=[], spot_symbols=[], loop=False),
                    feed.LOCK_BUSY_EXIT,
                )
            finally:
                held.close()
            again = feed.acquire_collector_lock(db)
            again.close()

    def test_reader_attaches_top5_and_pepe_perp_without_volume_z_or_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "public_derivatives.db"
            self.assertEqual(feed.round_exit_code(self._collect(db)), 0)
            rows = [{"binance_symbol": "PEPEUSDT", "taker_buy_ratio": 0.61}]
            attach_counterparty_context(rows, db_path=db)
            row = rows[0]
            self.assertAlmostEqual(row["funding_rate"], 0.0001)
            self.assertAlmostEqual(row["obi_top5"], round((50 - 55) / 105, 4))
            self.assertEqual(row["obi_top5_source_ts_ms"], 5000)
            self.assertEqual(row["taker_buy_ratio"], 0.61)
            self.assertNotIn("mark_price", row)
            self.assertNotIn("index_price", row)
            self.assertNotIn("volume_z", row)
            self.assertNotIn("vol_z_1h", row)

            preset = [{"binance_symbol": "PEPEUSDT", "obi_top5": 0.5}]
            attach_counterparty_context(preset, db_path=db)
            self.assertEqual(preset[0]["obi_top5"], 0.5)

    def test_public_client_has_no_order_route_or_private_key(self) -> None:
        headers = feed.public_request_headers("application/json")
        self.assertNotIn("X-MBX-APIKEY", headers)
        self.assertNotIn("test-key-should-not-leak", " ".join(headers.values()))
        text = Path(feed.__file__).read_text(encoding="utf-8")
        for banned in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "newOrder", "/api/v3/order", "X-MBX-APIKEY"):
            self.assertNotIn(banned, text)
        self.assertNotIn("volume_z", text)
        url = feed.spot_depth_ws_url("PEPEUSDT")
        self.assertTrue(url.startswith("wss://data-stream.binance.vision/ws/pepeusdt@depth@100ms"))
        self.assertNotIn("test-key-should-not-leak", url)


if __name__ == "__main__":
    unittest.main()
