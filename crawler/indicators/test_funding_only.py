"""--funding-only refreshes JEV fields without a technical pass.

Non-BULLISH stance keeps jev_strength at 0 (direction_coef 0). A newer
jev_signals.fetched_at advances token_indicators.jev_updated_at.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from indicators.base import IndicatorStore
from indicators.funding import compute_funding_fields, direction_coef, map_jev_signal
from indicators.run import main, run_funding_pass

_ROOT = Path(__file__).resolve().parents[1]

_JEV_DDL = """
CREATE TABLE IF NOT EXISTS jev_signals (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    jev_score REAL,
    observed_score REAL,
    coverage_pct REAL,
    score_kind TEXT,
    stance TEXT,
    flags_json TEXT,
    metrics_json TEXT,
    analyzed_at TEXT,
    fetched_at TEXT NOT NULL,
    raw TEXT,
    latency_ms REAL,
    cached INTEGER,
    error TEXT,
    PRIMARY KEY (chain_id, token_address)
);
"""

# Production freeze cited in the 2026-10-23 draft (UTC).
_FROZEN_JEV_UPDATED_AT = "2026-10-20T17:05:58Z"
_FRESH_FETCHED_AT = "2026-10-23T02:18:00Z"


class FundingOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ind.db"
        store = IndicatorStore(self.db_path)
        store.conn.executescript(_JEV_DDL)
        store.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _signal(
        self,
        token: str,
        *,
        stance: str,
        score: float,
        coverage: float,
        fetched_at: str = _FRESH_FETCHED_AT,
    ) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO jev_signals (
                chain_id, token_address, jev_score, coverage_pct, stance, fetched_at
            ) VALUES ('solana', ?, ?, ?, ?, ?)
            """,
            (token, score, coverage, stance, fetched_at),
        )
        conn.commit()
        conn.close()

    def test_non_bullish_strength_stays_zero_and_clock_advances(self) -> None:
        """BULLISH count is 0: high scores stay neutral and strength stays 0."""
        rows = [
            ("neutralhigh", "NEUTRAL", 92.0, 80.0),
            ("notbull", "NOT BULLISH", 79.0, 30.0),
            ("thin", "INSUFFICIENT EVIDENCE", 100.0, 100.0),
        ]
        for token, stance, score, coverage in rows:
            self._signal(token, stance=stance, score=score, coverage=coverage)
            self.assertNotEqual(map_jev_signal(stance, score), "bullish")
            fields = compute_funding_fields(
                {
                    "stance": stance,
                    "jev_score": score,
                    "coverage_pct": coverage,
                    "fetched_at": _FRESH_FETCHED_AT,
                }
            )
            assert fields is not None
            self.assertEqual(direction_coef(fields["jev_signal"]), 0.0)
            self.assertEqual(fields["jev_strength"], 0.0)

        with IndicatorStore(self.db_path) as store:
            for token, _stance, _score, _coverage in rows:
                store.upsert_token_indicators(
                    {
                        "chain_id": "solana",
                        "token_address": token,
                        "jev_updated_at": _FROZEN_JEV_UPDATED_AT,
                        "jev_strength": 0.0,
                        "jev_signal": "neutral",
                    }
                )
            stats = run_funding_pass(store)
            self.assertEqual(stats["seen"], 3)
            self.assertEqual(stats["ok"], 3)
            self.assertEqual(stats["err"], 0)
            for token, _stance, _score, _coverage in rows:
                saved = store.get_token_indicators("solana", token, use_cache=False)
                assert saved is not None
                self.assertEqual(saved["jev_signal"], "neutral")
                self.assertEqual(saved["jev_strength"], 0.0)
                self.assertEqual(saved["jev_updated_at"], _FRESH_FETCHED_AT)
                self.assertGreater(saved["jev_updated_at"], _FROZEN_JEV_UPDATED_AT)

    def test_bullish_formula_unchanged(self) -> None:
        fields = compute_funding_fields(
            {
                "stance": "BULLISH",
                "jev_score": 80.0,
                "coverage_pct": 50.0,
                "fetched_at": _FRESH_FETCHED_AT,
            }
        )
        assert fields is not None
        self.assertEqual(fields["jev_signal"], "bullish")
        self.assertEqual(direction_coef("bullish"), 1.0)
        self.assertEqual(fields["jev_strength"], 0.4)  # (80/100) * 0.5 * 1

    def test_cli_skips_technical_and_logs_funding_pass(self) -> None:
        self._signal("neutralhigh", stance="NEUTRAL", score=92.0, coverage=80.0)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_ROOT)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "indicators.run",
                "--funding-only",
                "--db",
                str(self.db_path),
            ],
            cwd=self._tmp.name,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("funding pass", proc.stdout)
        self.assertNotIn("technical priority", proc.stdout)
        with IndicatorStore(self.db_path) as store:
            saved = store.get_token_indicators(
                "solana", "neutralhigh", use_cache=False
            )
        assert saved is not None
        self.assertEqual(saved["jev_strength"], 0.0)
        self.assertEqual(saved["jev_signal"], "neutral")
        self.assertEqual(saved["jev_updated_at"], _FRESH_FETCHED_AT)
        self.assertIsNone(saved.get("tech_updated_at"))

    def test_help_lists_funding_only(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "indicators.run", "--help"],
            cwd=self._tmp.name,
            env={**os.environ, "PYTHONPATH": str(_ROOT)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--funding-only", proc.stdout)

    def test_main_funding_only_return_code(self) -> None:
        self._signal("notbull", stance="NOT BULLISH", score=79.0, coverage=100.0)
        # Stay out of the repo log dir: setup_logging writes ./logs.
        prev = os.getcwd()
        os.chdir(self._tmp.name)
        try:
            rc = main(["--funding-only", "--db", str(self.db_path)])
        finally:
            os.chdir(prev)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
