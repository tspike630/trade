"""JEV stance mapping + jev_age_min (2026-10-21)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]

from indicators.funding import compute_funding_fields, jev_age_minutes, map_jev_signal

_STANCES = [
    "NOT BULLISH",
    "NEUTRAL",
    "INSUFFICIENT EVIDENCE",
    "",
    None,
    "not bullish",
    "UNKNOWN",
]
_SCORES = [None, 50.0, 65.0, 79.0, 100.0]


def test_non_bullish_stance_never_bullish(stance="NOT BULLISH", score=79.0):
    assert map_jev_signal(stance, score) != "bullish"


def test_non_bullish_stance_high_score_neutral():
    assert map_jev_signal("NOT BULLISH", 79.0) == "neutral"
    assert map_jev_signal("NEUTRAL", 92.0) == "neutral"


def test_explicit_bullish():
    assert map_jev_signal("BULLISH", 80.0) == "bullish"
    assert map_jev_signal("BULLISH", None) == "bullish"
    assert map_jev_signal("BULLISH", 40.0) == "neutral"  # score gate on top


def test_bearish_paths():
    assert map_jev_signal("BEARISH", 90.0) == "bearish"
    assert map_jev_signal("NEUTRAL", 20.0) == "bearish"
    assert map_jev_signal("NOT BEARISH", 50.0) == "neutral"


def test_age_minutes():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    assert jev_age_minutes("2026-10-20T10:30:00Z", now) == 90.0
    assert jev_age_minutes("2026-10-20T10:30:00.000Z", now) == 90.0
    assert jev_age_minutes(None, now) is None
    assert jev_age_minutes("garbage", now) is None
    assert jev_age_minutes("2026-10-20T12:01:00Z", now) == 0.0
    assert jev_age_minutes("2026-10-20T13:00:00Z", now) is None


def test_compute_fields_case_9bb6():
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    f = compute_funding_fields(
        {
            "jev_score": 79.0,
            "coverage_pct": 30.0,
            "stance": "NOT BULLISH",
            "fetched_at": "2026-10-10T02:00:00Z",
        },
        now,
    )
    assert f["jev_signal"] == "neutral"
    assert f["jev_strength"] == 0.0
    assert f["jev_stance_raw"] == "NOT BULLISH"
    assert abs(float(f["jev_age_min"]) - (9 * 1440 + 600)) < 1e-6
    assert f["jev_updated_at"] == "2026-10-10T02:00:00Z"


def test_store_migration_idempotent(tmp_path: Path | None = None):
    from indicators.base import IndicatorStore

    def _run(root: Path) -> None:
        db = root / "t.db"
        c = sqlite3.connect(db)
        c.execute(
            "CREATE TABLE jev_signals ("
            "chain_id TEXT, token_address TEXT, jev_score REAL, observed_score REAL, "
            "coverage_pct REAL, score_kind TEXT, stance TEXT, flags_json TEXT, "
            "metrics_json TEXT, analyzed_at TEXT, fetched_at TEXT NOT NULL, raw TEXT, "
            "latency_ms REAL, cached INTEGER, error TEXT, "
            "PRIMARY KEY (chain_id, token_address))"
        )
        c.commit()
        c.close()
        for _ in range(2):  # idempotent migration
            st = IndicatorStore(db)
            cols = st._table_columns("token_indicators")
            assert {"jev_age_min", "jev_stance_raw"} <= cols
            assert {"jev_age_min", "jev_stance_raw"} <= st._table_columns(
                "indicator_history"
            )
            st.close()

    if tmp_path is not None:
        _run(tmp_path)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            _run(Path(tmp))


if pytest is not None:
    test_non_bullish_stance_never_bullish = pytest.mark.parametrize(
        "stance", _STANCES
    )(
        pytest.mark.parametrize("score", _SCORES)(
            test_non_bullish_stance_never_bullish
        )
    )


class JevMappingAgeUnittest(unittest.TestCase):
    def test_all_non_bullish_stances(self) -> None:
        for stance in _STANCES:
            for score in _SCORES:
                self.assertNotEqual(map_jev_signal(stance, score), "bullish")

    def test_high_score_neutral(self) -> None:
        test_non_bullish_stance_high_score_neutral()

    def test_bullish_and_bearish(self) -> None:
        test_explicit_bullish()
        test_bearish_paths()

    def test_age_and_fields_and_migration(self) -> None:
        test_age_minutes()
        test_compute_fields_case_9bb6()
        test_store_migration_idempotent()


if __name__ == "__main__":
    unittest.main()
