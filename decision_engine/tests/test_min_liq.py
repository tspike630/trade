"""Live entry liquidity floor: align dry-run/live buys with backtest min_liq."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from decision_engine import config as de_config
from decision_engine.core.data_adapter import IndicatorDataAdapter, liquidity_usd_from_raw
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_CANDIDATE, SignalBus
from decision_engine.strategies import get_strategy
from decision_engine.strategies.base_strategy import BaseStrategy

_FLOOR = 50_000.0


def _buy(**extra: Any) -> dict[str, Any]:
    sig = {
        "strategy_id": "jev_strength_v1",
        "action": "buy",
        "chain_id": "solana",
        "token_address": "7536qc4lesThinPool",
        "pair_address": "pair",
        "size_pct": 0.05,
        "reason": "jev_strength=0.4000>=0.3",
    }
    sig.update(extra)
    return sig


def _risk_script(liq_literal: str) -> str:
    return f"""
from decision_engine.core.risk_manager import RiskManager
risk = RiskManager(cooldown_sec=0)
kept = risk.filter([
    {{
        "strategy_id": "jev_strength_v1",
        "action": "buy",
        "chain_id": "solana",
        "token_address": "7536qc4lesThinPool",
        "liquidity_usd": {liq_literal},
    }}
])
print(len(kept))
print(risk.min_liq_usd)
"""


class LiquidityParseTests(unittest.TestCase):
    def test_pair_raw_json_usd(self) -> None:
        raw = json.dumps({"liquidity": {"usd": "50000"}, "priceUsd": "0.1"})
        self.assertEqual(liquidity_usd_from_raw(raw), 50_000.0)
        self.assertIsNone(liquidity_usd_from_raw("{"))
        self.assertIsNone(liquidity_usd_from_raw(json.dumps({"liquidity": 12})))
        self.assertIsNone(liquidity_usd_from_raw(json.dumps({"liquidity": {"usd": None}})))


class RiskFloorTests(unittest.TestCase):
    def _capture(self) -> tuple[logging.Handler, list[str]]:
        messages: list[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        handler = _Handler()
        logger = logging.getLogger("dexscreener.decision_engine.risk")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        return handler, messages

    def test_below_floor_dropped(self) -> None:
        risk = RiskManager(cooldown_sec=0, min_liq_usd=_FLOOR)
        handler, messages = self._capture()
        try:
            checked = risk.check(_buy(liquidity_usd=12_000))
            kept = risk.filter([_buy(liquidity_usd=12_000), _buy(token_address="other", liquidity_usd=49_999.99)])
        finally:
            logging.getLogger("dexscreener.decision_engine.risk").removeHandler(handler)
        self.assertFalse(checked["risk_passed"])
        self.assertEqual(checked["skip_reason"], "liq_below_min")
        self.assertEqual(kept, [])
        self.assertTrue(any("reason=liq_below_min" in line for line in messages), messages)
        self.assertTrue(any("7536qc4lesThinPo" in line for line in messages), messages)

    def test_at_and_above_floor_kept(self) -> None:
        risk = RiskManager(cooldown_sec=0, min_liq_usd=_FLOOR)
        at_floor = risk.filter([_buy(token_address="at", liquidity_usd=50_000)])
        above = risk.filter([_buy(token_address="above", liquidity_usd="80000")])
        self.assertEqual([s["token_address"] for s in at_floor], ["at"])
        self.assertEqual([s["token_address"] for s in above], ["above"])
        self.assertTrue(at_floor[0]["risk_passed"])
        self.assertNotIn("skip_reason", at_floor[0])

    def test_missing_liquidity_fail_closed(self) -> None:
        risk = RiskManager(cooldown_sec=0, min_liq_usd=_FLOOR)
        handler, messages = self._capture()
        try:
            missing = [
                risk.check(_buy(token_address="absent")),
                risk.check(_buy(token_address="none", liquidity_usd=None)),
                risk.check(_buy(token_address="blank", liquidity_usd="")),
                risk.check(_buy(token_address="nan", liquidity_usd=float("nan"))),
            ]
        finally:
            logging.getLogger("dexscreener.decision_engine.risk").removeHandler(handler)
        self.assertTrue(all(not row["risk_passed"] for row in missing))
        self.assertTrue(all(row["skip_reason"] == "no_liquidity" for row in missing))
        self.assertTrue(any("reason=no_liquidity" in line for line in messages), messages)

    def test_sell_ignores_floor(self) -> None:
        risk = RiskManager(cooldown_sec=0, min_liq_usd=_FLOOR)
        sell = risk.check(_buy(action="sell", liquidity_usd=None))
        self.assertTrue(sell["risk_passed"])
        self.assertNotIn("skip_reason", sell)

    def test_liq_skip_does_not_start_cooldown(self) -> None:
        risk = RiskManager(cooldown_sec=60, min_liq_usd=_FLOOR)
        self.assertEqual(len(risk.filter([_buy(liquidity_usd=12_000)])), 0)
        kept = risk.filter([_buy(liquidity_usd=50_000)])
        self.assertEqual(len(kept), 1)

    def test_floor_follows_config_when_unset(self) -> None:
        risk = RiskManager(cooldown_sec=0)
        self.assertEqual(risk.min_liq_usd, float(de_config.DECISION_MIN_LIQ_USD))


class EnvOverrideTests(unittest.TestCase):
    def _run(self, env_updates: dict[str, str | None], script: str) -> str:
        env = os.environ.copy()
        for key, value in env_updates.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_default_is_50000_and_blocks_12k(self) -> None:
        out = self._run(
            {"DECISION_MIN_LIQ_USD": None},
            "import decision_engine.config as c; print(c.DECISION_MIN_LIQ_USD)\n"
            + _risk_script("12000"),
        )
        lines = out.splitlines()
        self.assertEqual(float(lines[0]), 50_000.0)
        self.assertEqual(int(lines[1]), 0)
        self.assertEqual(float(lines[2]), 50_000.0)

    def test_env_override_lowers_floor(self) -> None:
        out = self._run({"DECISION_MIN_LIQ_USD": "10000"}, _risk_script("12000"))
        lines = out.splitlines()
        self.assertEqual(int(lines[0]), 1)
        self.assertEqual(float(lines[1]), 10_000.0)

    def test_invalid_env_falls_back_to_50000(self) -> None:
        out = self._run(
            {"DECISION_MIN_LIQ_USD": "nope"},
            "import decision_engine.config as c; print(c.DECISION_MIN_LIQ_USD)",
        )
        self.assertEqual(float(out.splitlines()[-1]), 50_000.0)


class AdapterLiquidityTests(unittest.TestCase):
    def test_list_hot_tokens_batches_pair_liquidity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, tier TEXT,
                    jev_strength REAL, primary_pair_address TEXT, updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE pairs (
                    chain_id TEXT, pair_address TEXT, token_address TEXT,
                    raw_json TEXT, updated_at TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO token_indicators VALUES (?, ?, 'hot', ?, ?, '2026-01-01T00:00:00Z')",
                [
                    ("solana", "thick", 0.9, "pair-thick"),
                    ("solana", "thin", 0.8, "pair-thin"),
                    ("solana", "badjson", 0.7, "pair-bad"),
                    ("solana", "nopair", 0.6, None),
                    ("ethereum", "other", 0.5, "pair-eth"),
                ],
            )
            conn.executemany(
                "INSERT INTO pairs VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')",
                [
                    (
                        "solana",
                        "pair-thick",
                        "thick",
                        json.dumps({"liquidity": {"usd": 80000}}),
                    ),
                    (
                        "solana",
                        "pair-thin",
                        "thin",
                        json.dumps({"liquidity": {"usd": 12000}}),
                    ),
                    ("solana", "pair-bad", "badjson", "{not-json"),
                    (
                        "ethereum",
                        "pair-eth",
                        "other",
                        json.dumps({"liquidity": {"usd": "50000"}}),
                    ),
                ],
            )
            conn.commit()
            conn.close()
            with IndicatorDataAdapter(path) as adapter:
                rows = adapter.list_hot_tokens(limit=10)
                snap = adapter.get_pair_snapshot("solana", "pair-thin")
            by_token = {row["token_address"]: row["liquidity_usd"] for row in rows}
            self.assertEqual(
                by_token,
                {
                    "thick": 80_000.0,
                    "thin": 12_000.0,
                    "badjson": None,
                    "nopair": None,
                    "other": 50_000.0,
                },
            )
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertEqual(snap["liquidity_usd"], 12_000.0)


class _BuyAndSell(BaseStrategy):
    strategy_id = "buy_and_sell_v1"

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        for row in data.get("hot_tokens") or []:
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=0.05,
                reason="forced-buy",
            )
            self.sell(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=1.0,
                reason="forced-sell",
            )
        return self.drain()


class EngineFloorTests(unittest.TestCase):
    def _seed(self, path: Path, rows: list[tuple[Any, ...]], *, tech: bool) -> None:
        conn = sqlite3.connect(path)
        extra_cols = ", change_5m REAL, volume_strength REAL" if tech else ""
        conn.execute(
            f"""
            CREATE TABLE token_indicators (
                chain_id TEXT, token_address TEXT, jev_strength REAL,
                jev_signal TEXT, jev_score REAL, tier TEXT,
                primary_pair_address TEXT, updated_at TEXT
                {extra_cols}
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE pairs (
                chain_id TEXT, pair_address TEXT, token_address TEXT,
                raw_json TEXT, updated_at TEXT
            )
            """
        )
        if tech:
            # (chain, token, jev, pair, change_5m, volume_strength, liquidity)
            indicator_rows = [row[:6] for row in rows]
            conn.executemany(
                """
                INSERT INTO token_indicators VALUES
                    (?, ?, ?, 'up', 1, 'hot', ?, '2026-10-14T00:00:00Z', ?, ?)
                """,
                indicator_rows,
            )
        else:
            # (chain, token, jev, pair, liquidity)
            indicator_rows = [row[:4] for row in rows]
            conn.executemany(
                """
                INSERT INTO token_indicators VALUES
                    (?, ?, ?, 'up', 1, 'hot', ?, '2026-10-14T00:00:00Z')
                """,
                indicator_rows,
            )
        pair_rows = []
        for row in rows:
            token = row[1]
            pair = row[3]
            liquidity = row[-1] if tech else row[4]
            if pair is None or liquidity is None:
                continue
            pair_rows.append(
                (
                    row[0],
                    pair,
                    token,
                    json.dumps({"liquidity": {"usd": liquidity}}),
                    "2026-10-14T00:00:00Z",
                )
            )
        if pair_rows:
            conn.executemany(
                "INSERT INTO pairs VALUES (?, ?, ?, ?, ?)",
                pair_rows,
            )
        conn.commit()
        conn.close()

    def _emit(self, path: Path, strategies: list[BaseStrategy]) -> list[sqlite3.Row]:
        adapter = IndicatorDataAdapter(path)
        bus = SignalBus(path, purpose=PURPOSE_LIVE_CANDIDATE)
        try:
            engine = DecisionEngine(
                adapter,
                bus,
                strategies,
                risk=RiskManager(cooldown_sec=0, min_liq_usd=_FLOOR),
                hot_limit=10,
            )
            engine.tick()
        finally:
            bus.close()
            adapter.close()
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute("SELECT * FROM decision_log ORDER BY id").fetchall())
        finally:
            conn.close()

    def test_jev_live_path_blocks_thin_and_missing(self) -> None:
        """12k class (paper incident) is not a live_candidate; 50k is."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.db"
            self._seed(
                path,
                [
                    ("solana", "7536qc4lesThinPool", 0.4, "pair-thin", 12_000),
                    ("solana", "atFloor", 0.5, "pair-at", 50_000),
                    ("solana", "aboveFloor", 0.45, "pair-above", 80_000),
                    ("solana", "noBook", 0.42, "pair-missing", None),
                    ("solana", "below", 0.41, None, 90_000),
                ],
                tech=False,
            )
            rows = self._emit(
                path,
                [get_strategy("jev_strength_v1", min_jev_strength=0.3)],
            )
            self.assertEqual(
                [row["token_address"] for row in rows],
                ["atFloor", "aboveFloor"],
            )
            for row in rows:
                self.assertEqual(row["purpose"], PURPOSE_LIVE_CANDIDATE)
                self.assertEqual(row["mode"], "dry_run")
                self.assertEqual(row["action"], "buy")
                raw = json.loads(row["raw_json"])
                self.assertGreaterEqual(raw["extras"]["liquidity_usd"], _FLOOR)

    def test_non_jev_buy_uses_same_floor_and_sell_still_emits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.db"
            self._seed(
                path,
                [
                    ("solana", "thinTech", 0.1, "pair-thin", 3.0, 1.0, 12_000),
                    ("solana", "okTech", 0.1, "pair-ok", 3.0, 1.0, 50_000),
                ],
                tech=True,
            )
            rows = self._emit(
                path,
                [
                    get_strategy("tech_momentum_v1"),
                    _BuyAndSell(),
                ],
            )
            buys = [row for row in rows if row["action"] == "buy"]
            sells = [row for row in rows if row["action"] == "sell"]
            self.assertEqual(
                sorted(row["token_address"] for row in buys),
                ["okTech", "okTech"],
            )
            self.assertEqual(
                sorted(row["token_address"] for row in sells),
                ["okTech", "thinTech"],
            )
            self.assertTrue(all(row["purpose"] == PURPOSE_LIVE_CANDIDATE for row in rows))
