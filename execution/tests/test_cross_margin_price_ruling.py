"""t447 price ruling. Producers write coordinates; the plan does not invent them."""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from decision_engine.core.cross_margin_price_contract import order_for_strategy
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_CROSS_MARGIN, SignalBus
from decision_engine.strategies.regime_cross_margin import (
    Regime1PositiveGammaFadeStrategy,
    Regime4CrowdedLongCarryShortStrategy,
)
from execution.margin_cross.entry import _closed_bar_reference_drift
from execution.margin_cross.strategy_gate import _ruled_stop_limit


def _bar(**overrides: object) -> dict:
    row = {
        "binance_symbol": "BTCUSDT",
        "price_usd": 100.0,
        "kline_low": 90.0,
        "boll_mid": 100.0,
    }
    row.update(overrides)
    return row


class PriceRulingTests(unittest.TestCase):
    def test_regime3_odds_bound_is_inclusive_and_raw(self) -> None:
        on_bound, reason = order_for_strategy(
            "regime3_xs_leader_pullback",
            _bar(price_usd=113.2, kline_low=90, boll_mid=100),
        )
        self.assertIsNone(reason)
        assert on_bound is not None
        self.assertGreaterEqual(on_bound["raw_odds"], 1.2)
        self.assertEqual(on_bound["limit_price"], 100)
        self.assertEqual(on_bound["take_profit_price"], 113.2)
        self.assertAlmostEqual(on_bound["stop_price"], 89.0)
        under = order_for_strategy(
            "regime3_xs_leader_pullback",
            _bar(price_usd=113.19, kline_low=90, boll_mid=100),
        )[1]
        self.assertEqual(under, "regime3_odds_below_min")

    def test_eight_formulas_and_short_high_failures(self) -> None:
        self.assertEqual(
            order_for_strategy("regime5_crisis_contagion_short", _bar())[1],
            "native_high_missing",
        )
        self.assertEqual(
            order_for_strategy(
                "regime5_crisis_contagion_short",
                _bar(kline_high=float("nan")),
            )[1],
            "native_high_missing",
        )
        self.assertEqual(
            order_for_strategy(
                "regime5_unlock_cliff_short",
                _bar(kline_high=99),
            )[1],
            "high_below_close",
        )
        contagion, reason = order_for_strategy(
            "regime5_crisis_contagion_short",
            _bar(price_usd=100, kline_low=90, boll_mid=95, kline_high=110),
        )
        self.assertIsNone(reason)
        assert contagion is not None
        self.assertEqual(contagion["limit_price"], 100)
        self.assertEqual(contagion["take_profit_price"], 90)
        self.assertAlmostEqual(contagion["stop_price"], 113.0)
        unlock, reason = order_for_strategy(
            "regime5_unlock_cliff_short",
            _bar(price_usd=100, kline_low=90, boll_mid=95, kline_high=110),
        )
        self.assertIsNone(reason)
        assert unlock is not None
        self.assertGreaterEqual(unlock["limit_price"], 100)
        self.assertEqual(unlock["take_profit_price"], 90)

    def test_reference_band_is_thirty_bps_not_the_close_matcher(self) -> None:
        doc = {"closed_bar_close": "100"}
        self.assertIsNone(
            _closed_bar_reference_drift(doc, {"reference_price": "100.3"})
        )
        self.assertEqual(
            _closed_bar_reference_drift(doc, {"reference_price": "100.3000001"}),
            "closed_bar_reference_drift",
        )

    def test_stop_limit_uses_twenty_bps_and_two_ticks(self) -> None:
        tick = Decimal("0.01")
        stop = Decimal("100")
        self.assertEqual(
            _ruled_stop_limit("OPEN_LONG", stop, tick),
            Decimal("99.80"),
        )
        self.assertEqual(
            _ruled_stop_limit("OPEN_SHORT", stop, tick),
            Decimal("100.20"),
        )
        wide = Decimal("1")
        self.assertEqual(_ruled_stop_limit("OPEN_LONG", stop, wide), Decimal("98"))
        self.assertEqual(_ruled_stop_limit("OPEN_SHORT", stop, wide), Decimal("102"))

    def test_producer_survives_signal_bus_without_an_intent_id(self) -> None:
        import tempfile
        from pathlib import Path

        from execution.tests.test_margin_cross_policy_and_idempotency import (
            _fresh_clocks,
            _state1_long_market_row,
        )

        row = dict(_state1_long_market_row())
        row.update(
            binance_symbol="BNBUSDT",
            chain_id="binance",
            token_address="BNBUSDT",
            kline_low=100,
            boll_mid=110,
            price_usd=100.8,
            trade_size_ratio=1.3,
            change_5m=-0.5,
            volume_strength=0.1,
            boll_lower=100,
            liquidity_usd=90_000,
            clocks=_fresh_clocks(1_700_000_000_000),
        )
        produced = Regime1PositiveGammaFadeStrategy().on_tick(
            {
                "hot_tokens": [row],
                "purpose": PURPOSE_LIVE_BNB_CROSS_MARGIN,
                "environ": {
                    "CROSS_MARGIN_ENABLE_STATE2_LONG": "1",
                    "CROSS_MARGIN_ENABLE_STATE4_LONG": "1",
                },
            }
        )
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0]["limit_price"], 100.8)
        self.assertNotIn("intent_id", produced[0])
        with tempfile.TemporaryDirectory() as raw:
            bus = SignalBus(
                Path(raw) / "dexscreener.db",
                dry_run=False,
                purpose=PURPOSE_LIVE_BNB_CROSS_MARGIN,
            )
            try:
                bus.publish(produced[0])
            finally:
                bus.close()
            import sqlite3

            conn = sqlite3.connect(Path(raw) / "dexscreener.db")
            stored = json.loads(
                conn.execute("SELECT raw_json FROM decision_log").fetchone()[0]
            )
            conn.close()
        self.assertNotIn("intent_id", stored)
        self.assertNotIn("intent_id", stored["extras"])
        self.assertEqual(stored["extras"]["limit_price"], 100.8)
        self.assertEqual(stored["extras"]["cross_margin_price_contract"], "t447")
        self.assertEqual(stored["mode"], "live_stub")

    def test_carry_short_producer_emits_the_ruled_stop(self) -> None:
        from execution.tests.test_margin_cross_policy_and_idempotency import (
            _state4_short_market_row,
        )

        row = dict(_state4_short_market_row())
        row.update(
            price_usd=100000,
            kline_low=99000,
            boll_mid=99600,
            kline_high=100100,
            binance_symbol="BTCUSDT",
            chain_id="binance",
            token_address="BTCUSDT",
        )
        produced = Regime4CrowdedLongCarryShortStrategy().on_tick({"hot_tokens": [row]})
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0]["action"], "sell")
        self.assertEqual(produced[0]["requested_action"], "OPEN_SHORT")
        self.assertEqual(produced[0]["limit_price"], 100000)
        self.assertEqual(produced[0]["take_profit_price"], 99600)
        self.assertAlmostEqual(produced[0]["stop_price"], 100210)
        self.assertGreaterEqual(produced[0]["raw_odds"], 1.5)


class FormulaBFallbackTests(unittest.TestCase):
    def test_empty_formula_keeps_the_planned_take_profit(self) -> None:
        import tempfile
        from types import SimpleNamespace

        from execution.margin_cross.errors import MarginTransportTimeout
        from execution.margin_cross.track import CrossMarginTrack

        class _Client:
            performs_network_io = False

            def query_order(self, _payload):
                return {"orderId": 1}

            def query_trades(self, _payload):
                return [{"qty": "1", "price": "100"}]

        intent = SimpleNamespace(
            action="OPEN_SHORT",
            intent_id="dl-9",
            symbol="BTCUSDT",
            limit_price=Decimal("100"),
            take_profit_price=Decimal("110"),
            stop_price=Decimal("120"),
            stop_limit_price=Decimal("120.24"),
        )
        contract = SimpleNamespace(formula_b_enabled=True)
        snapshot = SimpleNamespace(filters=SimpleNamespace(tick_size=Decimal("0.01")))
        with tempfile.TemporaryDirectory() as raw:
            track = CrossMarginTrack(str(raw) + "/book.db", _Client())
            kept = track._effective_protection_intent(intent, snapshot, contract)
        self.assertIs(kept, intent)
        self.assertEqual(kept.take_profit_price, Decimal("110"))
        self.assertEqual(kept.stop_price, Decimal("120"))

        class _Unknown(_Client):
            def query_order(self, _payload):
                raise MarginTransportTimeout("query_order")

        with tempfile.TemporaryDirectory() as raw:
            track = CrossMarginTrack(str(raw) + "/book.db", _Unknown())
            frozen = track._effective_protection_intent(intent, snapshot, contract)
        self.assertIs(frozen, intent)


class ProducerExecutionTests(unittest.TestCase):
    def test_carry_short_reaches_fake_borrow_entry_and_oco(self) -> None:
        import contextlib
        import io
        import os
        import socket
        import tempfile
        import time
        from pathlib import Path

        from decision_engine.core.engine import _stamp_explicit_account_route
        from execution.margin_cross import http_client as hc
        from execution.run import main as execution_main
        from execution.tests.test_margin_cross_http_and_live import _InMemoryMarginTransport
        from execution.tests.test_margin_cross_policy_and_idempotency import (
            _fresh_clocks,
            _state4_short_market_row,
        )
        from execution.tests.test_margin_live_production_path import (
            _TEST_API_KEY,
            _TEST_API_SECRET,
            _UID,
            _approved_file,
        )

        class _Blocked(socket.socket):
            def connect(self, address):  # type: ignore[override]
                raise OSError("price_ruling_network_blocked")

        now = int(time.time() * 1000)
        row = dict(_state4_short_market_row())
        row.update(
            price_usd=100000,
            kline_low=99000,
            boll_mid=99600,
            kline_high=100100,
            binance_symbol="BTCUSDT",
            chain_id="binance",
            token_address="BTCUSDT",
            clocks=_fresh_clocks(now),
        )
        produced = Regime4CrowdedLongCarryShortStrategy().on_tick({"hot_tokens": [row]})
        self.assertEqual(len(produced), 1)
        saved_socket = socket.socket
        saved_create = socket.create_connection
        saved_opener = hc._default_opener
        socket.socket = _Blocked  # type: ignore[misc]
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))  # type: ignore[assignment]
        transport = _InMemoryMarginTransport(
            cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID)
        )
        hc._default_opener = transport
        try:
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                decision = root / "dexscreener.db"
                margin = root / "margin-book.db"
                ledger = root / "cross-margin-exec.db"
                route = {
                    "account_domain": "binance_cross_margin",
                    "execution_mode": "live",
                    "spot_book_full": False,
                    "margin_ledger_path": str(margin),
                }
                stamped = _stamp_explicit_account_route(produced, route)[0]
                bus = SignalBus(decision, dry_run=False, purpose=PURPOSE_LIVE_BNB_CROSS_MARGIN)
                try:
                    bus.publish(stamped)
                finally:
                    bus.close()
                os.environ.update(_approved_file(root, now))
                os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
                os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
                os.environ["LIVE_KILL_SWITCH_PATH"] = str(root / "no-kill")
                os.environ.pop("CROSS_MARGIN_LEDGER_PATH", None)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = execution_main(
                        [
                            "--once",
                            "--json",
                            "--db",
                            str(decision),
                            "--ledger",
                            str(ledger),
                            "--accept-purpose",
                            PURPOSE_LIVE_BNB_CROSS_MARGIN,
                        ]
                    )
                self.assertEqual(code, 0, buf.getvalue())
                posts = [item for item in transport.requests if item[0] == "POST"]
                paths = [item[1] for item in posts]
                self.assertIn("/sapi/v1/margin/borrow-repay", paths)
                self.assertIn("/sapi/v1/margin/order", paths)
                self.assertIn("/sapi/v1/margin/order/oco", paths)
                oco = next(item[2] for item in posts if item[1].endswith("/oco"))
                self.assertLess(float(oco["price"]), 100000)
                self.assertGreater(float(oco["stopPrice"]), 100000)
                before = len(transport.requests)
                with contextlib.redirect_stdout(io.StringIO()):
                    execution_main(
                        [
                            "--once",
                            "--json",
                            "--db",
                            str(decision),
                            "--ledger",
                            str(ledger),
                            "--accept-purpose",
                            PURPOSE_LIVE_BNB_CROSS_MARGIN,
                        ]
                    )
                self.assertEqual(len(transport.requests), before)
        finally:
            hc._default_opener = saved_opener
            socket.socket = saved_socket  # type: ignore[misc]
            socket.create_connection = saved_create  # type: ignore[assignment]


class CrossMarginBarIdentityTests(unittest.TestCase):
    def test_strict_high_does_not_use_the_latest_bar_or_a_carried_value(self) -> None:
        import sqlite3

        from decision_engine.core.live_bnb_entry_coords import attach_live_entry_coordinates

        now = 1_700_000_000_000
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE binance_klines (
                symbol TEXT, interval TEXT, open_time_ms INTEGER,
                high REAL, low REAL, close REAL, close_time_ms INTEGER
            )
            """
        )
        matched_close = now - 60_000
        latest_close = now - 10_000
        conn.executemany(
            "INSERT INTO binance_klines VALUES (?, '5m', ?, ?, ?, ?, ?)",
            [
                ("BTCUSDT", matched_close - 300_000, 111.0, 90.0, 100.0, matched_close),
                ("BTCUSDT", latest_close - 300_000, 99999.0, 40.0, 50.0, latest_close),
            ],
        )
        missing_clock = {
            "binance_symbol": "BTCUSDT",
            "price_usd": 100.0,
            "kline_high": 12345.0,
        }
        attach_live_entry_coordinates(conn, [missing_clock], now_ms=now, strict_bar=True)
        self.assertEqual(missing_clock.get("entry_zone_coord_error"), "kline_bar_missing")
        self.assertNotIn("kline_high", missing_clock)

        carried = {
            "binance_symbol": "BTCUSDT",
            "kline_close_time_ms": matched_close,
            "price_usd": 100.0,
            "kline_high": 1.0,
        }
        attach_live_entry_coordinates(conn, [carried], now_ms=now, strict_bar=True)
        self.assertNotIn("entry_zone_coord_error", carried)
        self.assertEqual(carried.get("kline_high"), 111.0)

        mismatch = {
            "binance_symbol": "BTCUSDT",
            "kline_close_time_ms": matched_close,
            "price_usd": 101.0,
            "kline_high": 1.0,
        }
        attach_live_entry_coordinates(conn, [mismatch], now_ms=now, strict_bar=True)
        self.assertEqual(mismatch.get("entry_zone_coord_error"), "kline_close_mismatch")
        self.assertNotIn("kline_high", mismatch)

        stale = {
            "binance_symbol": "BTCUSDT",
            "kline_close_time_ms": now - 900_001,
            "price_usd": 100.0,
            "kline_high": 1.0,
        }
        conn.execute(
            "INSERT INTO binance_klines VALUES ('BTCUSDT', '5m', ?, 111, 90, 100, ?)",
            (now - 900_001 - 300_000, now - 900_001),
        )
        attach_live_entry_coordinates(conn, [stale], now_ms=now, strict_bar=True)
        self.assertEqual(stale.get("entry_zone_coord_error"), "entry_5m_kline_stale")
        self.assertNotIn("kline_high", stale)

        fresh = {
            "binance_symbol": "BTCUSDT",
            "kline_close_time_ms": now - 900_000,
            "price_usd": 100.0,
        }
        conn.execute(
            "INSERT INTO binance_klines VALUES ('BTCUSDT', '5m', ?, 222, 90, 100, ?)",
            (now - 900_000 - 300_000, now - 900_000),
        )
        attach_live_entry_coordinates(conn, [fresh], now_ms=now, strict_bar=True)
        self.assertNotIn("entry_zone_coord_error", fresh)
        self.assertEqual(fresh.get("kline_high"), 222.0)

        spot = {
            "binance_symbol": "BTCUSDT",
            "price_usd": 50.0,
        }
        attach_live_entry_coordinates(conn, [spot], now_ms=now, strict_bar=False)
        self.assertEqual(spot.get("kline_high"), 99999.0)
        conn.close()

        refused = order_for_strategy(
            "regime5_crisis_contagion_short",
            {
                "price_usd": 100.0,
                "kline_low": 90.0,
                "boll_mid": 95.0,
                "kline_high": 110.0,
                "kline_close_time_ms": 1_000,
                "_freshness_now_ms": 1_000 + 900_001,
            },
        )
        self.assertEqual(refused[1], "entry_5m_kline_stale")

    def test_tech_without_primary_pair_reaches_fake_entry_and_oco(self) -> None:
        import contextlib
        import io
        import os
        import socket
        import tempfile
        import time
        from pathlib import Path

        from decision_engine.core.engine import _stamp_explicit_account_route
        from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE
        from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
        from execution.margin_cross import http_client as hc
        from execution.run import main as execution_main
        from execution.tests.test_margin_cross_http_and_live import _InMemoryMarginTransport
        from execution.tests.test_margin_cross_policy_and_idempotency import (
            _fresh_clocks,
            _state1_long_market_row,
        )
        from execution.tests.test_margin_live_production_path import (
            _TEST_API_KEY,
            _TEST_API_SECRET,
            _UID,
            _approved_file,
        )

        class _Blocked(socket.socket):
            def connect(self, address):  # type: ignore[override]
                raise OSError("tech_pair_network_blocked")

        now = int(time.time() * 1000)
        low = 99600.0
        span = 2000.0
        row = dict(_state1_long_market_row())
        row.update(
            binance_symbol="BTCUSDT",
            chain_id="binance",
            token_address="BTCUSDT",
            kline_low=low,
            boll_mid=low + span,
            price_usd=low + 0.20 * span,
            trade_size_ratio=1.3,
            change_5m=-0.5,
            volume_strength=0.1,
            boll_lower=low,
            liquidity_usd=90_000,
            kline_close_time_ms=now - 60_000,
            _freshness_now_ms=now,
            clocks=_fresh_clocks(now),
        )
        row.pop("primary_pair_address", None)
        spot = TechBollFadeStrategy().on_tick(
            {"hot_tokens": [dict(row)], "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
        )
        self.assertTrue(spot)
        self.assertIsNone(spot[0].get("pair_address"))
        produced = TechBollFadeStrategy().on_tick(
            {"hot_tokens": [row], "purpose": PURPOSE_LIVE_BNB_CROSS_MARGIN}
        )
        self.assertEqual(len(produced), 1, TechBollFadeStrategy().rejected)
        self.assertEqual(produced[0]["pair_address"], "BTCUSDT")
        self.assertEqual(produced[0]["limit_price"], row["price_usd"])
        saved_socket = socket.socket
        saved_create = socket.create_connection
        saved_opener = hc._default_opener
        socket.socket = _Blocked  # type: ignore[misc]
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))  # type: ignore[assignment]
        transport = _InMemoryMarginTransport(
            cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID)
        )
        hc._default_opener = transport
        try:
            with tempfile.TemporaryDirectory() as raw_dir:
                root = Path(raw_dir)
                decision = root / "dexscreener.db"
                margin = root / "margin-book.db"
                ledger = root / "cross-margin-exec.db"
                stamped = _stamp_explicit_account_route(
                    produced,
                    {
                        "account_domain": "binance_cross_margin",
                        "execution_mode": "live",
                        "spot_book_full": False,
                        "margin_ledger_path": str(margin),
                    },
                )[0]
                bus = SignalBus(decision, dry_run=False, purpose=PURPOSE_LIVE_BNB_CROSS_MARGIN)
                try:
                    bus.publish(stamped)
                finally:
                    bus.close()
                os.environ.update(_approved_file(root, now))
                os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
                os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
                os.environ["LIVE_KILL_SWITCH_PATH"] = str(root / "no-kill")
                os.environ.pop("CROSS_MARGIN_LEDGER_PATH", None)
                argv = [
                    "--once",
                    "--json",
                    "--db",
                    str(decision),
                    "--ledger",
                    str(ledger),
                    "--accept-purpose",
                    PURPOSE_LIVE_BNB_CROSS_MARGIN,
                ]
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = execution_main(argv)
                self.assertEqual(code, 0, buf.getvalue())
                posts = [item for item in transport.requests if item[0] == "POST"]
                paths = [item[1] for item in posts]
                self.assertIn("/sapi/v1/margin/order", paths)
                self.assertIn("/sapi/v1/margin/order/oco", paths)
                before = len(transport.requests)
                with contextlib.redirect_stdout(io.StringIO()):
                    execution_main(argv)
                self.assertEqual(len(transport.requests), before)
        finally:
            hc._default_opener = saved_opener
            socket.socket = saved_socket  # type: ignore[misc]
            socket.create_connection = saved_create  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
