"""P2-38: decision purpose is sample vs live_candidate, and the log row matches it.

Execution reads decision_log today via raw_json (gateway SELECT does not yet
include the purpose column). New rows carry purpose in both places. Gateway
reject of sample stays in the execution layer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import decision_engine  # noqa: F401 — puts crawler/ on path when logging_setup is not a sibling
from decision_engine import config as de_config
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    DEFAULT_PURPOSE,
    PURPOSE_LIVE_CANDIDATE,
    PURPOSE_LIVE_TECH_BOLL_FADE,
    PURPOSE_SAMPLE,
    PURPOSES,
    SignalBus,
    normalize_signal,
    resolve_run_purpose,
)
from decision_engine.run import main
from decision_engine.strategies import get_strategy
from execution.gateway import ExecutionGateway, parse_decision_row

_BUY = {
    "strategy_id": "jev_strength_v1",
    "action": "buy",
    "chain_id": "solana",
    "token_address": "TokenAddress111",
    "pair_address": "PairAddress111",
    "size_pct": 0.05,
    "reason": "jev_strength=0.4000>=0.3",
    "risk_passed": True,
    "jev_strength": 0.4,
}


def _rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM decision_log ORDER BY id").fetchall())
    finally:
        conn.close()


class PurposeEnumTests(unittest.TestCase):
    def test_documented_values(self) -> None:
        import decision_engine.core.signal_bus as sb

        expected = frozenset(
            v for k, v in vars(sb).items() if k.startswith("PURPOSE_") and isinstance(v, str)
        )
        self.assertEqual(PURPOSES, expected)
        self.assertEqual(DEFAULT_PURPOSE, PURPOSE_LIVE_CANDIDATE)
        self.assertEqual(PURPOSE_LIVE_TECH_BOLL_FADE, "live_tech_boll_fade")
        self.assertIn(PURPOSE_LIVE_TECH_BOLL_FADE, PURPOSES)

    def test_resident_loop_and_once_default_live_candidate(self) -> None:
        # Neither --once nor --loop passes a threshold; both share this default.
        self.assertEqual(
            resolve_run_purpose(None, min_jev_strength=None, live_min_strength=0.3),
            PURPOSE_LIVE_CANDIDATE,
        )
        self.assertEqual(
            resolve_run_purpose(None, min_jev_strength=0.3, live_min_strength=0.3),
            PURPOSE_LIVE_CANDIDATE,
        )

    def test_weekly_check_015_is_sample(self) -> None:
        self.assertEqual(
            resolve_run_purpose(None, min_jev_strength=0.15, live_min_strength=0.3),
            PURPOSE_SAMPLE,
        )

    def test_explicit_purpose_wins_over_threshold(self) -> None:
        self.assertEqual(
            resolve_run_purpose(
                PURPOSE_LIVE_CANDIDATE,
                min_jev_strength=0.15,
                live_min_strength=0.3,
            ),
            PURPOSE_LIVE_CANDIDATE,
        )
        self.assertEqual(
            resolve_run_purpose(
                PURPOSE_SAMPLE,
                min_jev_strength=None,
                live_min_strength=0.3,
            ),
            PURPOSE_SAMPLE,
        )
        self.assertEqual(
            resolve_run_purpose("Sample", min_jev_strength=None, live_min_strength=0.3),
            PURPOSE_SAMPLE,
        )

    def test_unknown_explicit_purpose_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_run_purpose("paper", min_jev_strength=None, live_min_strength=0.3)

    def test_shipped_defaults_unchanged(self) -> None:
        cfg = (Path(__file__).resolve().parents[1] / "config.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("DECISION_COOLDOWN_SEC", "0")', cfg)
        self.assertIn(
            'os.environ.get("DECISION_DEFAULT_STRATEGY", "jev_strength_v1")',
            cfg,
        )
        self.assertIn('os.environ.get("DECISION_JEV_MIN_STRENGTH", "0.3")', cfg)
        self.assertIn('os.environ.get("DECISION_MIN_LIQ_USD", "50000")', cfg)


class SignalBusPurposeTests(unittest.TestCase):
    def test_default_bus_writes_live_candidate_and_keeps_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            bus = SignalBus(db)
            try:
                out = bus.publish(dict(_BUY))
            finally:
                bus.close()
            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(out["purpose"], PURPOSE_LIVE_CANDIDATE)
            self.assertEqual(out["mode"], "dry_run")
            self._assert_row(db, PURPOSE_LIVE_CANDIDATE, "dry_run")

    def test_sample_bus_does_not_change_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            bus = SignalBus(db, purpose=PURPOSE_SAMPLE)
            try:
                bus.publish(dict(_BUY))
            finally:
                bus.close()
            self._assert_row(db, PURPOSE_SAMPLE, "dry_run")

    def test_live_stub_still_gets_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            bus = SignalBus(db, dry_run=False)
            try:
                bus.publish(dict(_BUY))
            finally:
                bus.close()
            self._assert_row(db, PURPOSE_LIVE_CANDIDATE, "live_stub")

    def test_signal_purpose_overrides_bus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            bus = SignalBus(db, purpose=PURPOSE_LIVE_CANDIDATE)
            try:
                sig = dict(_BUY)
                sig["purpose"] = PURPOSE_SAMPLE
                bus.publish(sig)
            finally:
                bus.close()
            row = _rows(db)[0]
            raw = json.loads(row["raw_json"])
            self.assertEqual(row["purpose"], PURPOSE_SAMPLE)
            self.assertEqual(raw["purpose"], PURPOSE_SAMPLE)
            self.assertNotIn("purpose", raw.get("extras") or {})

    def test_unknown_signal_purpose_falls_back(self) -> None:
        norm = normalize_signal(
            {"purpose": "nope", "strategy_id": "s", "action": "buy"},
            purpose=PURPOSE_SAMPLE,
        )
        self.assertEqual(norm["purpose"], PURPOSE_SAMPLE)

    def test_migration_does_not_relabel_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    chain_id TEXT,
                    token_address TEXT,
                    size_pct REAL,
                    reason TEXT,
                    risk_passed INTEGER,
                    mode TEXT NOT NULL,
                    raw_json TEXT
                );
                """
            )
            old_raw = json.dumps({"pair_address": "oldpair", "extras": {}})
            conn.execute(
                """
                INSERT INTO decision_log
                    (ts, strategy_id, action, chain_id, token_address, size_pct,
                     reason, risk_passed, mode, raw_json)
                VALUES ('2026-10-01T00:00:00Z', 'jev_strength_v1', 'buy', 'solana',
                        'oldtoken', 0.05, 'historical-0.15', 1, 'dry_run', ?)
                """,
                (old_raw,),
            )
            conn.commit()
            conn.close()

            bus = SignalBus(db, purpose=PURPOSE_SAMPLE)
            try:
                bus.publish(dict(_BUY, token_address="newtoken"))
            finally:
                bus.close()

            rows = _rows(db)
            self.assertEqual(len(rows), 2)
            self.assertIsNone(rows[0]["purpose"])
            self.assertEqual(json.loads(rows[0]["raw_json"])["pair_address"], "oldpair")
            self.assertNotIn("purpose", json.loads(rows[0]["raw_json"]))
            self.assertEqual(rows[1]["purpose"], PURPOSE_SAMPLE)
            self.assertEqual(rows[1]["token_address"], "newtoken")
            conn = sqlite3.connect(db)
            try:
                idx = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_decision_log_purpose'"
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(idx)

    def test_gateway_read_shape_sees_sample_in_raw_json(self) -> None:
        """Current gateway SELECT loads raw_json, not the purpose column."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            ledger = Path(tmp) / "execution_ledger.db"
            bus = SignalBus(db, purpose=PURPOSE_SAMPLE)
            try:
                bus.publish(dict(_BUY))
            finally:
                bus.close()

            gateway = ExecutionGateway(
                decision_db=db,
                ledger_path=ledger,
                paper_enabled=False,
                pm_enabled=False,
            )
            try:
                pending = gateway.fetch_pending(0)
            finally:
                gateway.close()
            self.assertEqual(len(pending), 1)
            parsed = parse_decision_row(pending[0])
            self.assertEqual(parsed["mode"], "dry_run")
            self.assertEqual(parsed["strategy_id"], "jev_strength_v1")
            self.assertEqual(parsed["raw"]["purpose"], PURPOSE_SAMPLE)
            self.assertEqual(parsed["raw"]["pair_address"], "PairAddress111")
            self.assertNotIn("purpose", parsed["extras"])
            # Column is there for the gateway once it adds purpose to the SELECT.
            self.assertEqual(_rows(db)[0]["purpose"], PURPOSE_SAMPLE)

    def _assert_row(self, db: Path, purpose: str, mode: str) -> None:
        row = _rows(db)[0]
        raw = json.loads(row["raw_json"])
        self.assertEqual(row["purpose"], purpose)
        self.assertEqual(row["mode"], mode)
        self.assertEqual(raw["purpose"], purpose)
        self.assertEqual(raw["mode"], mode)
        self.assertEqual(raw["extras"]["jev_strength"], 0.4)
        self.assertNotIn("purpose", raw["extras"])


def _seed_indicators(db: Path, *, live_strength: float, weak_strength: float) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE token_indicators (
            chain_id TEXT,
            token_address TEXT,
            jev_strength REAL,
            jev_signal TEXT,
            jev_score REAL,
            tier TEXT,
            primary_pair_address TEXT,
            updated_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO token_indicators VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "solana",
                "TokLive",
                live_strength,
                "up",
                1.0,
                "hot",
                "PairLive",
                "2026-10-13T00:00:00Z",
            ),
            (
                "solana",
                "TokWeak",
                weak_strength,
                "up",
                1.0,
                "hot",
                "PairWeak",
                "2026-10-13T00:00:00Z",
            ),
        ],
    )
    # Purpose tests are about sample vs live_candidate, not the liquidity floor.
    # Seed both pairs at the configured floor so those buys still emit.
    floor = float(de_config.DECISION_MIN_LIQ_USD)
    raw = json.dumps({"liquidity": {"usd": floor}})
    conn.execute(
        """
        CREATE TABLE pairs (
            chain_id TEXT,
            pair_address TEXT,
            token_address TEXT,
            raw_json TEXT,
            updated_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO pairs VALUES (?, ?, ?, ?, ?)",
        [
            ("solana", "PairLive", "TokLive", raw, "2026-10-13T00:00:00Z"),
            ("solana", "PairWeak", "TokWeak", raw, "2026-10-13T00:00:00Z"),
        ],
    )
    conn.commit()
    conn.close()


class ProducerPurposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_handler = os.environ.get("LOG_HANDLER")
        os.environ["LOG_HANDLER"] = "stdout"
        self.live_min = float(de_config.JEV_MIN_STRENGTH)
        self.weak = self.live_min / 2.0

    def tearDown(self) -> None:
        if self._log_handler is None:
            os.environ.pop("LOG_HANDLER", None)
        else:
            os.environ["LOG_HANDLER"] = self._log_handler

    def _run(self, db: Path, extra: list[str]) -> list[sqlite3.Row]:
        rc = main(
            [
                "--dry-run",
                "--once",
                "--db",
                str(db),
                "--strategy",
                "jev_strength_v1",
                *extra,
            ]
        )
        self.assertEqual(rc, 0)
        return _rows(db)

    def test_once_default_emits_live_candidate_only_above_live_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed_indicators(db, live_strength=self.live_min + 0.1, weak_strength=self.weak)
            rows = self._run(db, [])
            self.assertEqual([r["token_address"] for r in rows], ["TokLive"])
            self.assertEqual(rows[0]["purpose"], PURPOSE_LIVE_CANDIDATE)
            self.assertEqual(rows[0]["mode"], "dry_run")
            self.assertEqual(rows[0]["strategy_id"], de_config.DEFAULT_STRATEGY)
            raw = json.loads(rows[0]["raw_json"])
            self.assertEqual(raw["purpose"], PURPOSE_LIVE_CANDIDATE)

    def test_weekly_threshold_marks_every_row_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed_indicators(db, live_strength=self.live_min + 0.1, weak_strength=self.weak)
            rows = self._run(db, ["--min-jev-strength", str(self.weak)])
            self.assertEqual({r["token_address"] for r in rows}, {"TokLive", "TokWeak"})
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row["purpose"], PURPOSE_SAMPLE)
                self.assertEqual(row["mode"], "dry_run")
                self.assertEqual(json.loads(row["raw_json"])["purpose"], PURPOSE_SAMPLE)

    def test_purpose_flag_sample_at_live_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed_indicators(db, live_strength=self.live_min + 0.1, weak_strength=self.weak)
            rows = self._run(db, ["--purpose", PURPOSE_SAMPLE])
            self.assertEqual([r["token_address"] for r in rows], ["TokLive"])
            self.assertEqual(rows[0]["purpose"], PURPOSE_SAMPLE)
            self.assertEqual(rows[0]["mode"], "dry_run")

    def test_purpose_flag_overrides_weekly_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed_indicators(db, live_strength=self.live_min + 0.1, weak_strength=self.weak)
            rows = self._run(
                db,
                ["--purpose", PURPOSE_LIVE_CANDIDATE, "--min-jev-strength", str(self.weak)],
            )
            self.assertEqual({r["token_address"] for r in rows}, {"TokLive", "TokWeak"})
            for row in rows:
                self.assertEqual(row["purpose"], PURPOSE_LIVE_CANDIDATE)
                self.assertEqual(row["mode"], "dry_run")

    def test_engine_tick_uses_bus_purpose(self) -> None:
        """DecisionEngine does not strip purpose; the bus stamps every emit."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed_indicators(db, live_strength=self.live_min + 0.1, weak_strength=self.weak)
            adapter = IndicatorDataAdapter(db)
            bus = SignalBus(db, purpose=PURPOSE_LIVE_CANDIDATE)
            try:
                engine = DecisionEngine(
                    adapter,
                    bus,
                    [get_strategy("jev_strength_v1", min_jev_strength=self.live_min)],
                    risk=RiskManager(cooldown_sec=0),
                    hot_limit=10,
                )
                result = engine.tick()
            finally:
                bus.close()
                adapter.close()
            self.assertEqual(result["emitted"], 1)
            row = _rows(db)[0]
            self.assertEqual(row["purpose"], PURPOSE_LIVE_CANDIDATE)
            self.assertEqual(row["mode"], "dry_run")


if __name__ == "__main__":
    unittest.main()
