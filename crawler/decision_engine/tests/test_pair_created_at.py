"""decision_log.raw_json extras.pairCreatedAt (ms) for review token_age.

Does not change gates, weights, or run params. Historical rows are not rewritten.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

import decision_engine  # noqa: F401
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_CANDIDATE,
    SignalBus,
    coerce_pair_created_at_ms,
    normalize_signal,
)
from decision_engine.strategies.base_strategy import BaseStrategy

_CREATED_MS = 1_669_602_304_000


def _rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM decision_log ORDER BY id").fetchall())
    finally:
        conn.close()


class CoerceTests(unittest.TestCase):
    def test_ms_and_seconds(self) -> None:
        self.assertEqual(coerce_pair_created_at_ms(_CREATED_MS), _CREATED_MS)
        self.assertEqual(coerce_pair_created_at_ms(_CREATED_MS / 1000.0), _CREATED_MS)
        self.assertEqual(coerce_pair_created_at_ms(str(_CREATED_MS)), _CREATED_MS)
        self.assertIsNone(coerce_pair_created_at_ms(None))
        self.assertIsNone(coerce_pair_created_at_ms(""))
        self.assertIsNone(coerce_pair_created_at_ms(True))


class SignalBusPairCreatedAtTests(unittest.TestCase):
    def test_explicit_pairCreatedAt_lands_in_extras(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            bus = SignalBus(db, purpose=PURPOSE_LIVE_CANDIDATE)
            try:
                bus.publish(
                    {
                        "strategy_id": "jev_strength_v1",
                        "action": "buy",
                        "chain_id": "solana",
                        "token_address": "Tok1",
                        "pair_address": "Pair1",
                        "size_pct": 0.05,
                        "reason": "t",
                        "risk_passed": True,
                        "pairCreatedAt": _CREATED_MS,
                        "jev_strength": 0.4,
                    }
                )
            finally:
                bus.close()
            raw = json.loads(_rows(db)[0]["raw_json"])
            self.assertEqual(raw["extras"]["pairCreatedAt"], _CREATED_MS)
            self.assertNotIn("pair_created_at", raw["extras"])

    def test_lookup_from_pairs_when_signal_lacks_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE pairs (
                    chain_id TEXT, pair_address TEXT, token_address TEXT,
                    raw_json TEXT, pair_created_at INTEGER, updated_at TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO pairs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "solana",
                    "PairLookup",
                    "TokL",
                    "{}",
                    _CREATED_MS,
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.commit()
            conn.close()
            bus = SignalBus(db, purpose=PURPOSE_LIVE_CANDIDATE)
            try:
                bus.publish(
                    {
                        "strategy_id": "jev_strength_v1",
                        "action": "buy",
                        "chain_id": "solana",
                        "token_address": "TokL",
                        "pair_address": "PairLookup",
                        "size_pct": 0.05,
                        "reason": "t",
                        "risk_passed": True,
                    }
                )
            finally:
                bus.close()
            raw = json.loads(_rows(db)[0]["raw_json"])
            self.assertEqual(raw["extras"]["pairCreatedAt"], _CREATED_MS)

    def test_missing_pairs_row_omits_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            bus = SignalBus(db)
            try:
                bus.publish(
                    {
                        "strategy_id": "jev_strength_v1",
                        "action": "buy",
                        "chain_id": "solana",
                        "token_address": "TokX",
                        "pair_address": "NoPair",
                        "size_pct": 0.05,
                        "reason": "t",
                        "risk_passed": True,
                    }
                )
            finally:
                bus.close()
            raw = json.loads(_rows(db)[0]["raw_json"])
            extras = raw.get("extras") or {}
            self.assertNotIn("pairCreatedAt", extras)
            self.assertNotIn("pair_created_at", extras)

    def test_normalize_puts_field_in_extras_only(self) -> None:
        norm = normalize_signal(
            {
                "strategy_id": "s",
                "action": "buy",
                "pairCreatedAt": _CREATED_MS,
            }
        )
        self.assertEqual(norm["_extras"]["pairCreatedAt"], _CREATED_MS)
        self.assertNotIn("pairCreatedAt", norm)


class _ForcedBuy(BaseStrategy):
    strategy_id = "forced_buy_v1"

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        for row in data.get("hot_tokens") or []:
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=0.05,
                reason="forced",
            )
        return self.drain()


class EngineStampPairCreatedAtTests(unittest.TestCase):
    def test_hot_row_pair_created_at_reaches_decision_log(self) -> None:
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
                    raw_json TEXT, pair_created_at INTEGER, updated_at TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES (?, ?, 'hot', ?, ?, ?)",
                (
                    "solana",
                    "TokHot",
                    0.9,
                    "pair-hot",
                    "2026-10-19T00:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO pairs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "solana",
                    "pair-hot",
                    "TokHot",
                    json.dumps({"liquidity": {"usd": 80_000}}),
                    _CREATED_MS,
                    "2026-10-19T00:00:00Z",
                ),
            )
            conn.commit()
            conn.close()

            adapter = IndicatorDataAdapter(path)
            bus = SignalBus(path, purpose=PURPOSE_LIVE_CANDIDATE)
            try:
                hot = adapter.list_hot_tokens(limit=5)
                self.assertEqual(hot[0].get("pair_created_at"), _CREATED_MS)
                engine = DecisionEngine(
                    adapter,
                    bus,
                    [_ForcedBuy()],
                    risk=RiskManager(min_liq_usd=50_000),
                )
                result = engine.tick()
                self.assertEqual(result["emitted"], 1)
            finally:
                bus.close()
                adapter.close()

            raw = json.loads(_rows(path)[0]["raw_json"])
            self.assertEqual(raw["extras"]["pairCreatedAt"], _CREATED_MS)
            self.assertEqual(raw["extras"]["liquidity_usd"], 80_000.0)


if __name__ == "__main__":
    unittest.main()
