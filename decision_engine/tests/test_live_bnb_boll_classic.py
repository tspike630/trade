"""live_bnb_boll_classic keeps the pre-entry-zone legs on the Binance scan.

``live_bnb_boll_fade`` still uses the entry zone. ``race_tech_boll_fade``
stays on the paper emit and the DEX rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

import decision_engine  # noqa: F401 — crawler/ on path for logging_setup
from decision_engine import config as de_config
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine, scan_limit_for_purpose
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_RACE_TECH_BOLL_FADE,
    SignalBus,
)
from decision_engine.run import main as run_main
from decision_engine.strategies import get_strategy, resolve_strategy_id
from decision_engine.strategies.entry_sizing import buy_size_fields
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from decision_engine.strategies.live_bnb_size_floor import ENTRY_MIN_NOTIONAL_USDT
from decision_engine.strategies.tech_boll_fade import (
    CLASSIC_EXIT_SHELL,
    CLASSIC_MAX_HOLD_MIN,
    CLASSIC_SL_PCT,
    CLASSIC_TP_PCT,
    TechBollFadeStrategy,
)
from decision_engine.tests.test_binance_indicators import _insert_cex, _seed, _tick


def _admit_listed_quote(db: Path, spot: str, ccxt_symbol: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS symbols (symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT)"
    )
    base = spot[: -len("USDT")] if spot.endswith("USDT") else spot
    conn.execute(
        "INSERT INTO symbols VALUES (?, 'TRADING', ?, 'USDT')",
        (spot, base),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ccxt_ticks (
            exchange TEXT, symbol TEXT, ts INTEGER,
            quote_volume_24h REAL, volume_24h REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO ccxt_ticks VALUES ('binance', ?, ?, 3000000, 1)",
        (ccxt_symbol, int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()
from execution.exits import DEFAULT_MAX_HOLD_MIN, DEFAULT_SL_PCT, DEFAULT_TP_PCT
from execution.executors.binance_spot import build_exit_plan

_LOW = 100.0
_SPAN = 10.0
_MID = _LOW + _SPAN


def _zone_row() -> dict:
    """A large-trade entry-zone bar whose 5m change fails the classic leg."""
    price = _LOW + 0.20 * _SPAN
    return {
        "binance_symbol": "BNBUSDT",
        "chain_id": "binance",
        "token_address": "BNBUSDT",
        "kline_low": _LOW,
        "boll_mid": _MID,
        "price_usd": price,
        "trade_size_ratio": 1.6,
        "change_5m": 0.0,
        "volume_strength": 0.0,
        "boll_lower": _LOW,
        "liquidity_usd": 90_000.0,
    }


def _classic_row() -> dict:
    """Paper legs pass. No entry-zone coordinates."""
    return {
        "binance_symbol": "ONLYCEXUSDT",
        "chain_id": "binance",
        "token_address": "ONLYCEXUSDT",
        "primary_pair_address": "ONLYCEX/USDT",
        "change_5m": -4.0,
        "volume_strength": 0.9,
        "boll_lower": 1.0,
        "price_usd": 0.8,
        "liquidity_usd": 90_000.0,
    }


def _paper_row() -> dict:
    return {
        "chain_id": "solana",
        "token_address": "MintA",
        "change_5m": -3.0,
        "volume_strength": 0.9,
        "boll_lower": 1.0,
        "price_usd": 0.9,
        "liquidity_usd": 90_000.0,
    }


class ClassicRecipeTests(unittest.TestCase):
    def test_percents_match_the_paper_exit_defaults(self) -> None:
        self.assertEqual(CLASSIC_TP_PCT, DEFAULT_TP_PCT)
        self.assertEqual(CLASSIC_SL_PCT, DEFAULT_SL_PCT)
        self.assertEqual(CLASSIC_MAX_HOLD_MIN, DEFAULT_MAX_HOLD_MIN)
        self.assertEqual(CLASSIC_TP_PCT, 10.0)
        self.assertEqual(CLASSIC_SL_PCT, 5.0)
        self.assertEqual(CLASSIC_MAX_HOLD_MIN, 60.0)
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, Decimal("20"))

    def test_alias_is_the_same_strategy(self) -> None:
        self.assertEqual(resolve_strategy_id("live_bnb_boll_classic"), "tech_boll_fade_v1")
        classic = get_strategy("live_bnb_boll_classic")
        self.assertIsInstance(classic, TechBollFadeStrategy)
        self.assertEqual(classic.strategy_id, "tech_boll_fade_v1")

    def test_classic_does_not_take_the_entry_zone_path(self) -> None:
        zone = _zone_row()
        decision = evaluate_entry_zone(zone)
        self.assertTrue(decision.emit, decision.reject_reason)
        self.assertAlmostEqual(decision.take_profit_price or 0.0, _MID)

        strategy = TechBollFadeStrategy()
        classic = strategy.on_tick(
            {"hot_tokens": [zone], "purpose": PURPOSE_LIVE_BNB_BOLL_CLASSIC}
        )
        self.assertEqual(classic, [])
        self.assertGreater(strategy.rejected.get("tech_leg_fail", 0), 0)

        live = TechBollFadeStrategy().on_tick(
            {"hot_tokens": [zone], "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
        )
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["and_mode"], "entry_zone")
        self.assertEqual(live[0]["entry_reason"], "large_trade_market")
        self.assertAlmostEqual(live[0]["take_profit_price"], _MID)
        self.assertNotIn("exit_shell", live[0])

    def test_classic_emit_is_the_fixed_tp_sl_shell(self) -> None:
        row = _classic_row()
        planned = buy_size_fields(row, floor_pct=0.05)
        old_book = os.environ.get("BINANCE_SPOT_BOOK_USDT")
        os.environ["BINANCE_SPOT_BOOK_USDT"] = "100"
        try:
            signals = TechBollFadeStrategy(position_pct=0.05).on_tick(
                {"hot_tokens": [row], "purpose": PURPOSE_LIVE_BNB_BOLL_CLASSIC}
            )
        finally:
            if old_book is None:
                os.environ.pop("BINANCE_SPOT_BOOK_USDT", None)
            else:
                os.environ["BINANCE_SPOT_BOOK_USDT"] = old_book
        self.assertEqual(len(signals), 1)
        got = signals[0]
        self.assertEqual(got["and_mode"], "tech_fade&boll_lower")
        self.assertEqual(got["exit_shell"], CLASSIC_EXIT_SHELL)
        self.assertEqual(got["binance_symbol"], "ONLYCEXUSDT")
        self.assertEqual(got["order_type"], "MARKET")
        self.assertEqual(got["tp_pct"], 10.0)
        self.assertEqual(got["sl_pct"], 5.0)
        self.assertEqual(got["max_hold_min"], 60.0)
        self.assertAlmostEqual(got["price_usd"], 0.8)
        self.assertAlmostEqual(got["stop_price"], 0.8 * 0.95)
        self.assertAlmostEqual(got["take_profit_price"], 0.8 * 1.10)
        self.assertNotAlmostEqual(got["take_profit_price"], 1.0)
        self.assertAlmostEqual(got["size_pct"], planned["size_pct"])
        self.assertNotIn("entry_reason", got)
        self.assertNotIn("entry_zone_min", got)
        self.assertNotIn("min_notional_lift", got)
        self.assertNotIn("legacy_gate", got)
        plan, err = build_exit_plan(
            {"action": "buy"},
            {
                "stop_price": got["stop_price"],
                "take_profit_price": got["take_profit_price"],
                "order_type": got["order_type"],
                "price_usd": got["price_usd"],
            },
        )
        self.assertIsNone(err)
        assert plan is not None
        self.assertEqual(plan["price_source"], "signal_absolute")
        self.assertFalse(plan["defaults_applied"])
        self.assertFalse(plan["submitted"])
        self.assertEqual(plan["protective_order"], "not_supported_not_sent")
        self.assertAlmostEqual(float(plan["stop_price"]), 0.8 * 0.95)
        self.assertAlmostEqual(float(plan["take_profit_price"]), 0.8 * 1.10)

        zone_strategy = TechBollFadeStrategy()
        live = zone_strategy.on_tick(
            {"hot_tokens": [row], "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
        )
        self.assertEqual(live, [])
        self.assertGreater(zone_strategy.rejected.get("kline_low_missing", 0), 0)

    def test_paper_race_shell_is_unchanged(self) -> None:
        row = _paper_row()
        strategy = TechBollFadeStrategy()
        paper = strategy.on_tick(
            {"hot_tokens": [row], "purpose": PURPOSE_RACE_TECH_BOLL_FADE}
        )
        self.assertEqual(len(paper), 1)
        got = paper[0]
        self.assertEqual(got["and_mode"], "tech_fade&boll_lower")
        self.assertEqual(got["chain_id"], "solana")
        self.assertEqual(got["token_address"], "MintA")
        self.assertNotIn("binance_symbol", got)
        self.assertNotIn("stop_price", got)
        self.assertNotIn("take_profit_price", got)
        self.assertNotIn("tp_pct", got)
        self.assertNotIn("sl_pct", got)
        self.assertNotIn("max_hold_min", got)
        self.assertNotIn("exit_shell", got)
        self.assertNotIn("entry_reason", got)
        self.assertNotIn("order_type", got)
        bare = TechBollFadeStrategy().on_tick({"hot_tokens": [row]})
        self.assertEqual(
            {key: bare[0][key] for key in bare[0] if key != "ts"},
            {key: got[key] for key in got if key != "ts"},
        )


class ClassicScanTests(unittest.TestCase):
    def test_scan_window_matches_the_live_bnb_arm(self) -> None:
        self.assertGreaterEqual(
            scan_limit_for_purpose(PURPOSE_LIVE_BNB_BOLL_CLASSIC),
            600,
        )
        self.assertEqual(
            scan_limit_for_purpose(PURPOSE_LIVE_BNB_BOLL_CLASSIC),
            scan_limit_for_purpose(PURPOSE_LIVE_BNB_BOLL_FADE),
        )
        self.assertEqual(
            scan_limit_for_purpose(PURPOSE_RACE_TECH_BOLL_FADE),
            de_config.HOT_TOKEN_LIMIT,
        )
        self.assertLess(
            scan_limit_for_purpose(PURPOSE_RACE_TECH_BOLL_FADE),
            600,
        )

    def test_cli_classic_defaults_to_the_wide_scan(self) -> None:
        captured: dict[str, int] = {}

        def spy(engine, adapter, bus, strategies, **kwargs):
            del engine, adapter, strategies
            captured["hot_limit"] = int(kwargs["hot_limit"])
            self.assertEqual(bus.purpose, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            raise RuntimeError("stop-before-tick")

        original = DecisionEngine.__init__
        DecisionEngine.__init__ = spy  # type: ignore[method-assign]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "d.db"
                sqlite3.connect(db).close()
                with self.assertRaises(RuntimeError):
                    run_main(
                        [
                            "--strategy",
                            "live_bnb_boll_classic",
                            "--purpose",
                            PURPOSE_LIVE_BNB_BOLL_CLASSIC,
                            "--once",
                            "--db",
                            str(db),
                        ]
                    )
        finally:
            DecisionEngine.__init__ = original  # type: ignore[method-assign]
        self.assertGreaterEqual(captured["hot_limit"], 600)

    def test_auto_without_indicators_uses_dex_map_and_classic_legs(self) -> None:
        old = os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "d.db"
                _seed(db)
                bare_rows, bare_payload = _tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
                self.assertEqual(bare_payload["bnb_factor_source"], "dex_map")
                self.assertEqual(bare_rows, [])
                self.assertIn(
                    "universe_unavailable",
                    bare_payload["quote_turnover"]["reasons"],
                )
                # A symbols table is a primary snapshot, so auto would leave
                # dex_map for the kline book. Pin dex_map to keep this feed.
                os.environ["LIVE_BNB_FACTOR_SOURCE"] = "dex_map"
                _admit_listed_quote(db, "ZZQBOLLUSDT", "ZZQBOLL/USDT")
                rows, payload = _tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
        finally:
            if old is None:
                os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
            else:
                os.environ["LIVE_BNB_FACTOR_SOURCE"] = old
        self.assertEqual(payload["bnb_factor_source"], "dex_map")
        classic = [row for row in rows if row["token_address"] == "ZZQBOLLUSDT"]
        self.assertEqual(len(classic), 1)
        extras = json.loads(classic[0]["raw_json"])["extras"]
        self.assertEqual(extras["and_mode"], "tech_fade&boll_lower")
        self.assertEqual(extras["exit_shell"], CLASSIC_EXIT_SHELL)
        self.assertEqual(extras["binance_symbol"], "ZZQBOLLUSDT")
        self.assertNotIn("entry_reason", extras)
        self.assertAlmostEqual(extras["price_usd"], 0.9)
        self.assertAlmostEqual(extras["stop_price"], 0.9 * 0.95)
        self.assertAlmostEqual(extras["take_profit_price"], 0.9 * 1.10)

    def test_engine_reads_binance_indicators_not_the_entry_zone(self) -> None:
        old = os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "d.db"
                _seed(db)
                _insert_cex(db)
                rows, payload = _tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
                self.assertEqual(payload["bnb_factor_source"], "binance_indicators")
                classic = [
                    row for row in rows if row["purpose"] == PURPOSE_LIVE_BNB_BOLL_CLASSIC
                ]
                self.assertEqual(len(classic), 1)
                row = classic[0]
                self.assertEqual(row["strategy_id"], "tech_boll_fade_v1")
                self.assertEqual(row["token_address"], "ONLYCEXUSDT")
                self.assertEqual(row["chain_id"], "binance")
                extras = json.loads(row["raw_json"])["extras"]
                self.assertEqual(extras["binance_symbol"], "ONLYCEXUSDT")
                self.assertEqual(extras["and_mode"], "tech_fade&boll_lower")
                self.assertEqual(extras["exit_shell"], CLASSIC_EXIT_SHELL)
                self.assertAlmostEqual(extras["stop_price"], 0.8 * 0.95)
                self.assertAlmostEqual(extras["take_profit_price"], 0.8 * 1.10)
                self.assertEqual(extras["max_hold_min"], 60.0)
                self.assertNotIn("entry_reason", extras)
                self.assertNotIn("entry_zone_min", extras)

                live_rows, live_payload = _tick(db, PURPOSE_LIVE_BNB_BOLL_FADE)
                self.assertEqual(live_payload["bnb_factor_source"], "binance_indicators")
                live = [
                    item
                    for item in live_rows
                    if item["purpose"] == PURPOSE_LIVE_BNB_BOLL_FADE
                ]
                self.assertEqual(len(live), 1)
                live_extras = json.loads(live[0]["raw_json"])["extras"]
                self.assertEqual(live_extras["and_mode"], "entry_zone")
                self.assertAlmostEqual(live_extras["take_profit_price"], 1.0)
                self.assertNotIn("exit_shell", live_extras)

                race_rows, race_payload = _tick(db, PURPOSE_RACE_TECH_BOLL_FADE)
                self.assertNotIn("bnb_factor_source", race_payload)
                race = [
                    item
                    for item in race_rows
                    if item["purpose"] == PURPOSE_RACE_TECH_BOLL_FADE
                ]
                self.assertTrue(race)
                for item in race:
                    self.assertEqual(item["chain_id"], "solana")
                    blob = json.loads(item["raw_json"]).get("extras", {})
                    self.assertNotIn("binance_symbol", blob)
                    self.assertNotIn("stop_price", blob)
                    self.assertNotIn("ONLYCEXUSDT", item["token_address"])
        finally:
            if old is not None:
                os.environ["LIVE_BNB_FACTOR_SOURCE"] = old

    def test_wide_scan_keeps_a_classic_break_ranked_past_fifty(self) -> None:
        old = os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
        try:
            self._assert_wide_scan()
        finally:
            if old is not None:
                os.environ["LIVE_BNB_FACTOR_SOURCE"] = old

    def _assert_wide_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE binance_indicators (
                    symbol TEXT PRIMARY KEY,
                    base_asset TEXT,
                    quote_asset TEXT,
                    status TEXT,
                    price_usd REAL,
                    change_5m REAL,
                    volume_strength REAL,
                    boll_lower REAL,
                    quote_volume_24h REAL
                )
                """
            )
            liq = float(de_config.DECISION_MIN_LIQ_USD) + 10_000.0
            rows = [
                (
                    f"D{index:03d}USDT",
                    f"D{index:03d}",
                    "USDT",
                    "TRADING",
                    5.0,
                    -10.0,
                    0.9,
                    1.0,
                    liq,
                )
                for index in range(60)
            ]
            rows.append(
                ("KEEPUSDT", "KEEP", "USDT", "TRADING", 0.8, -3.0, 0.9, 1.0, liq)
            )
            conn.executemany(
                """
                INSERT INTO binance_indicators (
                    symbol, base_asset, quote_asset, status,
                    price_usd, change_5m, volume_strength, boll_lower,
                    quote_volume_24h
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            now_ms = int(time.time() * 1000)
            conn.execute(
                "CREATE TABLE symbols (symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT)"
            )
            conn.execute(
                """
                CREATE TABLE ccxt_ticks (
                    exchange TEXT, symbol TEXT, ts INTEGER,
                    quote_volume_24h REAL, volume_24h REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO symbols VALUES (?, 'TRADING', ?, 'USDT')",
                [(row[0], row[1]) for row in rows],
            )
            conn.executemany(
                "INSERT INTO ccxt_ticks VALUES ('binance', ?, ?, 3000000, 1)",
                [(f"{row[1]}/USDT", now_ms) for row in rows],
            )
            conn.commit()
            conn.close()
            adapter = IndicatorDataAdapter(db)
            bus = SignalBus(db, purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            try:
                engine = DecisionEngine(
                    adapter,
                    bus,
                    [get_strategy("tech_boll_fade_v1")],
                    risk=RiskManager(cooldown_sec=0),
                )
                self.assertGreaterEqual(engine.hot_limit, 600)
                payload = engine.tick()
            finally:
                bus.close()
                adapter.close()
            self.assertEqual(payload["bnb_factor_source"], "binance_indicators")
            self.assertGreaterEqual(payload["hot_loaded"], 61)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                written = list(
                    conn.execute(
                        "SELECT token_address, raw_json FROM decision_log "
                        "WHERE purpose = ?",
                        (PURPOSE_LIVE_BNB_BOLL_CLASSIC,),
                    )
                )
            finally:
                conn.close()
            self.assertEqual([item["token_address"] for item in written], ["KEEPUSDT"])
            extras = json.loads(written[0]["raw_json"])["extras"]
            self.assertEqual(extras["and_mode"], "tech_fade&boll_lower")
            self.assertAlmostEqual(extras["take_profit_price"], 0.8 * 1.10)
            self.assertAlmostEqual(extras["stop_price"], 0.8 * 0.95)


if __name__ == "__main__":
    unittest.main()
