"""Unit checks for the decision-engine code-sweep fixes."""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from decision_engine import config as de_config
from decision_engine.backtest.replay import merge_bar_rows
from decision_engine.core.data_adapter import _INDICATOR_COLS, IndicatorDataAdapter
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import SignalBus
from decision_engine.factors.buy.boll_breakout import BollBreakoutBuyFactor
from decision_engine.factors.buy.rsi_bounce import RsiBounceBuyFactor
from decision_engine.strategies.factor_strategy import FactorStrategy


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReplayMergeTests(unittest.TestCase):
    def test_later_null_tech_does_not_wipe_earlier_values(self) -> None:
        tech = {
            "chain_id": "solana",
            "token_address": "tok",
            "rsi_14": 100.0,
            "macd": 1.2,
            "boll_upper": 5.0,
            "boll_mid": 4.0,
            "boll_lower": 3.0,
            "jev_strength": 0.40,
            "tier": "hot",
        }
        funding = {
            "chain_id": "solana",
            "token_address": "tok",
            "rsi_14": None,
            "macd": None,
            "boll_upper": None,
            "boll_mid": None,
            "boll_lower": None,
            "jev_strength": 0.48,
            "tier": "hot",
        }
        merged = merge_bar_rows(tech, funding)
        self.assertEqual(merged["rsi_14"], 100.0)
        self.assertEqual(merged["macd"], 1.2)
        self.assertEqual(merged["boll_upper"], 5.0)
        self.assertEqual(merged["jev_strength"], 0.48)

    def test_later_non_null_tech_wins(self) -> None:
        merged = merge_bar_rows({"rsi_14": 10.0, "macd": None}, {"rsi_14": 22.0, "macd": 0.5})
        self.assertEqual(merged["rsi_14"], 22.0)
        self.assertEqual(merged["macd"], 0.5)


class RsiBounceTests(unittest.TestCase):
    def test_flat_zero_is_rejected(self) -> None:
        factor = RsiBounceBuyFactor()
        self.assertIsNone(factor.fit_tick({"rsi_14": 20, "change_5m": 0.0}))
        self.assertIsNone(factor.fit_tick({"rsi_14": 20, "change_5m": -0.4}))
        self.assertIsNone(factor.fit_tick({"rsi_14": 20, "change_5m": None}))

    def test_positive_change_passes_default(self) -> None:
        hit = RsiBounceBuyFactor().fit_tick({"rsi_14": 20, "change_5m": 0.01})
        self.assertIsNotNone(hit)

    def test_positive_floor_still_uses_threshold(self) -> None:
        factor = RsiBounceBuyFactor(min_change_5m=1.0)
        self.assertIsNone(factor.fit_tick({"rsi_14": 20, "change_5m": 0.5}))
        self.assertIsNotNone(factor.fit_tick({"rsi_14": 20, "change_5m": 1.0}))


class SellPairAddressTests(unittest.TestCase):
    def _strategy(self) -> FactorStrategy:
        return FactorStrategy(
            strategy_id="factor_test_sell",
            buy_factors=["jev_strength"],
            sell_factors=["time_exit"],
            sell_params={"time_exit": {"max_hold_seconds": 10}},
        )

    def test_sell_uses_pair_then_primary(self) -> None:
        strat = self._strategy()
        primary = strat.on_tick(
            {
                "hot_tokens": [],
                "now_ts": 100,
                "open_positions": [
                    {
                        "chain_id": "solana",
                        "token_address": "tok",
                        "primary_pair_address": "pair_primary",
                        "opened_ts": 0,
                        "size_pct": 1.0,
                    }
                ],
            }
        )
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0]["action"], "sell")
        self.assertEqual(primary[0]["pair_address"], "pair_primary")

        explicit = strat.on_tick(
            {
                "hot_tokens": [],
                "now_ts": 100,
                "open_positions": [
                    {
                        "chain_id": "solana",
                        "token_address": "tok",
                        "pair_address": "pair_explicit",
                        "primary_pair_address": "pair_primary",
                        "opened_ts": 0,
                        "size_pct": 1.0,
                    }
                ],
            }
        )
        self.assertEqual(explicit[0]["pair_address"], "pair_explicit")


class BollColumnTests(unittest.TestCase):
    def test_adapter_requests_stored_names(self) -> None:
        self.assertIn("boll_upper", _INDICATOR_COLS)
        self.assertIn("boll_mid", _INDICATOR_COLS)
        self.assertIn("boll_lower", _INDICATOR_COLS)
        self.assertNotIn("boll_up", _INDICATOR_COLS)
        self.assertNotIn("boll_low", _INDICATOR_COLS)

    def test_factor_reads_boll_upper(self) -> None:
        factor = BollBreakoutBuyFactor()
        hit = factor.fit_tick({"boll_upper": 1.0, "price_usd": 2.0})
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["boll_upper"], 1.0)
        self.assertIsNotNone(factor.fit_tick({"boll_up": 1.0, "price_usd": 2.0}))


class HotFreshnessTests(unittest.TestCase):
    def _capture(self) -> tuple[logging.Handler, list[str]]:
        messages: list[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        handler = _Handler()
        logger = logging.getLogger("dexscreener.decision_engine.data_adapter")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        return handler, messages

    def test_skips_stale_hot_and_does_not_fall_back(self) -> None:
        now = datetime.now(timezone.utc)
        fresh_seen = _iso(now - timedelta(seconds=60))
        stale_seen = _iso(now - timedelta(seconds=7200))
        fresh_tech = _iso(now - timedelta(seconds=30))
        stale_tech = _iso(now - timedelta(seconds=1800))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE tokens (
                    chain_id TEXT, token_address TEXT, last_seen_at TEXT
                );
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, tier TEXT,
                    jev_strength REAL, updated_at TEXT, tech_updated_at TEXT,
                    rsi_14 REAL, boll_upper REAL, boll_mid REAL, boll_lower REAL,
                    primary_pair_address TEXT
                );
                """
            )
            conn.executemany(
                "INSERT INTO tokens VALUES (?, ?, ?)",
                [
                    ("solana", "fresh", fresh_seen),
                    ("solana", "old_seen", fresh_seen),
                    ("solana", "old_tech", fresh_seen),
                    ("solana", "warm_hi", fresh_seen),
                ],
            )
            # old_seen last_seen is stale; fix that row
            conn.execute(
                "UPDATE tokens SET last_seen_at=? WHERE token_address='old_seen'",
                (stale_seen,),
            )
            conn.executemany(
                """
                INSERT INTO token_indicators
                    (chain_id, token_address, tier, jev_strength, updated_at,
                     tech_updated_at, boll_upper)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("solana", "fresh", "hot", 0.40, fresh_tech, fresh_tech, 1.0),
                    ("solana", "old_seen", "hot", 0.99, fresh_tech, fresh_tech, 1.0),
                    ("solana", "old_tech", "hot", 0.80, fresh_tech, stale_tech, 1.0),
                    ("solana", "warm_hi", "warm", 1.50, fresh_tech, fresh_tech, 1.0),
                ],
            )
            conn.commit()
            conn.close()

            handler, messages = self._capture()
            try:
                with IndicatorDataAdapter(path) as adapter:
                    rows = adapter.list_hot_tokens(limit=50)
            finally:
                logging.getLogger("dexscreener.decision_engine.data_adapter").removeHandler(
                    handler
                )
            self.assertEqual([r["token_address"] for r in rows], ["fresh"])
            self.assertTrue(any("skipped_stale=2" in m for m in messages), messages)
            self.assertIn("boll_upper", rows[0])

    def test_all_stale_hot_does_not_fall_back_to_other_tiers(self) -> None:
        now = datetime.now(timezone.utc)
        stale_seen = _iso(now - timedelta(seconds=7200))
        fresh_tech = _iso(now - timedelta(seconds=30))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE tokens (
                    chain_id TEXT, token_address TEXT, last_seen_at TEXT
                );
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, tier TEXT,
                    jev_strength REAL, updated_at TEXT, tech_updated_at TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO tokens VALUES ('solana', 'zombie', ?)", (stale_seen,)
            )
            conn.execute(
                "INSERT INTO tokens VALUES ('solana', 'warm', ?)",
                (_iso(now - timedelta(seconds=30)),),
            )
            conn.execute(
                """
                INSERT INTO token_indicators VALUES
                    ('solana', 'zombie', 'hot', 0.9, ?, ?)
                """,
                (fresh_tech, fresh_tech),
            )
            conn.execute(
                """
                INSERT INTO token_indicators VALUES
                    ('solana', 'warm', 'warm', 0.95, ?, ?)
                """,
                (fresh_tech, fresh_tech),
            )
            conn.commit()
            conn.close()
            with IndicatorDataAdapter(path) as adapter:
                self.assertEqual(adapter.list_hot_tokens(), [])

    def test_missing_freshness_columns_degrade_to_tier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, tier TEXT,
                    jev_strength REAL, updated_at TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES ('solana', 'a', 'hot', 0.2, '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES ('solana', 'b', 'hot', 0.5, '2026-01-01T00:00:00Z')"
            )
            conn.commit()
            conn.close()
            handler, messages = self._capture()
            try:
                with IndicatorDataAdapter(path) as adapter:
                    rows = adapter.list_hot_tokens()
            finally:
                logging.getLogger("dexscreener.decision_engine.data_adapter").removeHandler(
                    handler
                )
            self.assertEqual([r["token_address"] for r in rows], ["b", "a"])
            self.assertTrue(any("freshness columns missing" in m for m in messages), messages)
            self.assertTrue(any("skipped_stale=0" in m for m in messages), messages)


class CooldownTests(unittest.TestCase):
    def test_default_zero_keeps_repeat_buys(self) -> None:
        risk = RiskManager(cooldown_sec=0)
        buy = {
            "strategy_id": "jev_strength_v1",
            "action": "buy",
            "chain_id": "solana",
            "token_address": "tok",
        }
        self.assertEqual(len(risk.filter([dict(buy), dict(buy)])), 2)

    def test_cooldown_drops_repeat_buy_not_sell(self) -> None:
        risk = RiskManager(cooldown_sec=60)
        buy = {
            "strategy_id": "jev_strength_v1",
            "action": "buy",
            "chain_id": "solana",
            "token_address": "tok",
        }
        self.assertEqual(len(risk.filter([dict(buy), dict(buy)])), 1)
        sell = dict(buy, action="sell")
        self.assertEqual(len(risk.filter([sell])), 1)
        other = dict(buy, strategy_id="other")
        self.assertTrue(risk.check(other)["risk_passed"])


class SignalBusTests(unittest.TestCase):
    def test_busy_timeout_matches_connect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = SignalBus(Path(tmp) / "d.db")
            try:
                row = bus._rw.execute("PRAGMA busy_timeout").fetchone()
                assert row is not None
                self.assertEqual(int(row[0]), 30000)
            finally:
                bus.close()

    def test_retry_then_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = SignalBus(Path(tmp) / "d.db")
            real = SignalBus._write_log
            state = {"n": 0}

            def flaky(self: SignalBus, norm: dict) -> None:
                state["n"] += 1
                if state["n"] == 1:
                    raise sqlite3.OperationalError("database is locked")
                real(self, norm)

            try:
                with mock.patch.object(SignalBus, "_write_log", flaky), mock.patch(
                    "decision_engine.core.signal_bus._PUBLISH_RETRY_SLEEP_SEC", 0
                ):
                    out = bus.publish(
                        {
                            "strategy_id": "s",
                            "action": "buy",
                            "chain_id": "c",
                            "token_address": "tok",
                            "size_pct": 0.05,
                            "reason": "r",
                        }
                    )
                self.assertIsNotNone(out)
                self.assertEqual(state["n"], 2)
                assert bus._rw is not None
                n = bus._rw.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
                self.assertEqual(n, 1)
            finally:
                bus.close()

    def test_failed_row_does_not_drop_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = SignalBus(Path(tmp) / "d.db")
            real = SignalBus._write_log

            def flaky(self: SignalBus, norm: dict) -> None:
                if norm.get("token_address") == "bad":
                    raise sqlite3.OperationalError("database is locked")
                real(self, norm)

            try:
                with mock.patch.object(SignalBus, "_write_log", flaky), mock.patch(
                    "decision_engine.core.signal_bus._PUBLISH_RETRY_SLEEP_SEC", 0
                ):
                    out = bus.publish_many(
                        [
                            {
                                "strategy_id": "s",
                                "action": "buy",
                                "chain_id": "c",
                                "token_address": "bad",
                                "size_pct": 0.05,
                                "reason": "r",
                            },
                            {
                                "strategy_id": "s",
                                "action": "buy",
                                "chain_id": "c",
                                "token_address": "good",
                                "size_pct": 0.05,
                                "reason": "r",
                            },
                        ]
                    )
                self.assertEqual([s["token_address"] for s in out], ["good"])
            finally:
                bus.close()


class BacktestDefaultTests(unittest.TestCase):
    def test_help_default_follows_live_config(self) -> None:
        from decision_engine.backtest import run as bt_run

        proc = subprocess.run(
            [sys.executable, "-m", "decision_engine.backtest.run", "--help"],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            check=False,
        )
        # parents[2] is the package parent (repo root or crawler/), which is cwd for -m.
        text = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, text)
        self.assertIn(str(de_config.JEV_MIN_STRENGTH), text)
        self.assertIn("0.15", text)
        self.assertIn("same as live", text)
        self.assertIs(bt_run.de_config.JEV_MIN_STRENGTH, de_config.JEV_MIN_STRENGTH)


class ConfigPathTests(unittest.TestCase):
    def _resolved(self, extra: dict[str, str], *, drop_decision: bool) -> str:
        env = os.environ.copy()
        if drop_decision:
            env.pop("DECISION_DB_PATH", None)
        env.pop("DB_PATH", None)
        env.update(extra)
        root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import decision_engine.config as c; print(c.DB_PATH)",
            ],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().splitlines()[-1]

    def test_decision_db_path_wins_over_db_path(self) -> None:
        got = self._resolved(
            {
                "DECISION_DB_PATH": "/tmp/decision_sweep_decision.db",
                "DB_PATH": "/tmp/decision_sweep_db.db",
            },
            drop_decision=False,
        )
        self.assertEqual(got, "/tmp/decision_sweep_decision.db")

    def test_db_path_used_when_decision_unset(self) -> None:
        got = self._resolved(
            {"DB_PATH": "/tmp/decision_sweep_db_only.db"},
            drop_decision=True,
        )
        self.assertEqual(got, "/tmp/decision_sweep_db_only.db")


if __name__ == "__main__":
    unittest.main()
