"""Spec A: change_7d / volume_7d_avg, and change_24h coverage from pair_history.

Short history stays NULL. Nothing scales change_24h by 7 or divides volume_h24 by 7.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from decision_engine.core.data_adapter import IndicatorDataAdapter
from indicators.base import INTERVAL_SEC, IndicatorStore
from indicators.technical import (
    TechnicalIndicators,
    change_pct,
    volume_7d_avg,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _point(
    when: datetime,
    price: float,
    *,
    volume_h24: float | None = None,
    volume_h1: float | None = None,
) -> dict[str, object]:
    return {
        "recorded_at": _iso(when),
        "price_usd": price,
        "volume_h24": volume_h24,
        "volume_h1": volume_h1,
    }


def _seed(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE pair_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id TEXT,
            pair_address TEXT,
            token_address TEXT,
            recorded_at TEXT,
            price_usd REAL,
            volume_h24 REAL,
            volume_h1 REAL,
            liquidity_usd REAL,
            txns_h24 INTEGER,
            tier TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO pair_history (
            chain_id, pair_address, token_address, recorded_at,
            price_usd, volume_h24, volume_h1, liquidity_usd, tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'hot')
        """,
        rows,
    )
    conn.commit()
    conn.close()


class LifecycleDeltaMathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    def _at(self, seconds: float, price: float, volume_h24: float | None = None) -> dict:
        return _point(
            self.now + timedelta(seconds=seconds),
            price,
            volume_h24=volume_h24,
        )

    def test_change_pct_skew_matches_half_lookback_cap(self) -> None:
        # 5m lookback skew is 150s (half of 300), not the 6h cap.
        inside = [self._at(-440, 100.0), self._at(0, 110.0)]
        outside = [self._at(-500, 100.0), self._at(0, 110.0)]
        self.assertAlmostEqual(change_pct(inside, INTERVAL_SEC["change_5m"]), 10.0)
        self.assertIsNone(change_pct(outside, INTERVAL_SEC["change_5m"]))

    def test_change_7d_accepts_anchor_inside_six_hour_skew(self) -> None:
        skew_inside = 5 * 3600
        series = [
            self._at(-(INTERVAL_SEC["change_7d"] + skew_inside), 100.0, 1000.0),
            self._at(0, 150.0, 3000.0),
        ]
        self.assertAlmostEqual(change_pct(series, INTERVAL_SEC["change_7d"]), 50.0)
        self.assertAlmostEqual(volume_7d_avg(series), 2000.0)

    def test_change_7d_and_volume_null_just_outside_skew(self) -> None:
        skew_outside = 7 * 3600
        series = [
            self._at(-(INTERVAL_SEC["change_7d"] + skew_outside), 100.0, 1000.0),
            self._at(0, 150.0, 3000.0),
        ]
        self.assertIsNone(change_pct(series, INTERVAL_SEC["change_7d"]))
        self.assertIsNone(volume_7d_avg(series))
        self.assertNotEqual(volume_7d_avg(series), 3000.0 / 7.0)

    def test_short_history_does_not_scale_change_24h_or_split_volume(self) -> None:
        series = [self._at(-86400, 110.0, 700.0), self._at(0, 121.0, 700.0)]
        change_24h = change_pct(series, INTERVAL_SEC["change_24h"])
        change_7d = change_pct(series, INTERVAL_SEC["change_7d"])
        self.assertAlmostEqual(change_24h, 10.0)
        self.assertIsNone(change_7d)
        self.assertNotEqual(change_7d, change_24h * 7)
        self.assertIsNone(volume_7d_avg(series))
        self.assertNotEqual(volume_7d_avg(series), 700.0 / 7.0)

    def test_full_window_is_price_change_and_mean_of_daily_buckets(self) -> None:
        series = [
            self._at(-INTERVAL_SEC["change_7d"], 100.0, 100.0),
            self._at(-3 * 86400, 120.0, 200.0),
            self._at(-86400, 110.0, 400.0),
            self._at(-2 * 3600, 140.0, 10.0),
            self._at(0, 150.0, 300.0),
        ]
        change_24h = change_pct(series, INTERVAL_SEC["change_24h"])
        change_7d = change_pct(series, INTERVAL_SEC["change_7d"])
        self.assertAlmostEqual(change_7d, 50.0)
        self.assertAlmostEqual(change_24h, (150.0 - 110.0) / 110.0 * 100.0)
        self.assertNotAlmostEqual(change_7d, change_24h * 7)
        # Latest reading wins inside a day: 300 replaces 10 and 400 in the newest bucket.
        # Buckets: 100 (day 0), 200 (day 4), 300 (day 6).
        averaged = volume_7d_avg(series)
        self.assertAlmostEqual(averaged, 200.0)
        self.assertNotAlmostEqual(averaged, 300.0 / 7.0)

    def test_volume_h1_is_not_promoted_to_a_daily_average(self) -> None:
        series = [
            _point(self.now - timedelta(days=7), 100.0, volume_h1=10.0),
            _point(self.now, 150.0, volume_h1=20.0),
        ]
        self.assertIsNone(volume_7d_avg(series))
        self.assertAlmostEqual(change_pct(series, INTERVAL_SEC["change_7d"]), 50.0)


class LifecycleDeltaWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ind.db"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self) -> None:
        store = getattr(self, "store", None)
        if store is not None:
            store.close()
        self._tmp.cleanup()

    def _open(self, rows: list[tuple]) -> IndicatorStore:
        _seed(self.db_path, rows)
        self.store = IndicatorStore(self.db_path)
        return self.store

    def _row(
        self,
        pair: str,
        when: datetime,
        price: float,
        *,
        volume_h24: float | None = None,
        volume_h1: float | None = None,
        liquidity: float = 1000.0,
        token: str = "tokena",
    ) -> tuple:
        return (
            "solana",
            pair,
            token,
            _iso(when),
            price,
            volume_h24,
            volume_h1,
            liquidity,
        )

    def test_change_24h_uses_anchor_inside_skew_but_outside_old_25h_cutoff(self) -> None:
        # 28h ago is within the 6h skew of a 24h lookback and outside now-25h.
        store = self._open(
            [
                self._row("pair1", self.now - timedelta(hours=28), 100.0, volume_h24=700.0),
                self._row("pair1", self.now - timedelta(hours=1), 108.0, volume_h24=700.0),
                self._row("pair1", self.now, 110.0, volume_h24=700.0),
            ]
        )
        tech = TechnicalIndicators(store, write_history_for_hot=True)
        written = tech.upsert_one("solana", "tokena", tier="hot")
        assert written is not None
        self.assertAlmostEqual(written["change_24h"], 10.0)
        self.assertIsNone(written["change_7d"])
        self.assertIsNone(written["volume_7d_avg"])
        self.assertNotEqual(written["change_7d"], written["change_24h"] * 7)
        self.assertNotEqual(written["volume_7d_avg"], 700.0 / 7.0)
        saved = store.get_token_indicators("solana", "tokena", use_cache=False)
        assert saved is not None
        self.assertAlmostEqual(saved["change_24h"], 10.0)
        self.assertIsNone(saved["change_7d"])
        self.assertIsNone(saved["volume_7d_avg"])
        hist = store.conn.execute(
            "SELECT change_24h, change_7d, volume_7d_avg FROM indicator_history"
        ).fetchone()
        assert hist is not None
        self.assertAlmostEqual(hist["change_24h"], 10.0)
        self.assertIsNone(hist["change_7d"])
        self.assertIsNone(hist["volume_7d_avg"])

    def test_stale_tail_still_merges_the_24h_anchor_band(self) -> None:
        # Last snapshot is 7d old, so a wall-clock 7d load does not contain
        # the price ~24h before that snapshot. The anchor band is merged.
        last = self.now - timedelta(days=7)
        store = self._open(
            [
                self._row("pair1", last - timedelta(hours=24), 100.0, volume_h24=50.0),
                self._row("pair1", last - timedelta(hours=1), 140.0, volume_h24=80.0),
                self._row("pair1", last, 150.0, volume_h24=90.0),
            ]
        )
        tech = TechnicalIndicators(store, write_history_for_hot=False)
        written = tech.upsert_one("solana", "tokena", tier="hot")
        assert written is not None
        self.assertAlmostEqual(written["change_24h"], 50.0)

    def test_seven_day_primary_pair_writes_history(self) -> None:
        store = self._open(
            [
                self._row("pair1", self.now - timedelta(days=7), 100.0, volume_h24=100.0),
                self._row("pair1", self.now - timedelta(days=3), 120.0, volume_h24=200.0),
                self._row("pair1", self.now - timedelta(hours=24), 110.0, volume_h24=400.0),
                self._row("pair1", self.now, 150.0, volume_h24=300.0, liquidity=5000.0),
            ]
        )
        tech = TechnicalIndicators(store, write_history_for_hot=True)
        written = tech.upsert_one("solana", "tokena", tier="hot")
        assert written is not None
        self.assertAlmostEqual(written["change_7d"], 50.0)
        self.assertAlmostEqual(written["volume_7d_avg"], 200.0)
        self.assertNotAlmostEqual(written["change_7d"], written["change_24h"] * 7)
        self.assertNotAlmostEqual(written["volume_7d_avg"], 300.0 / 7.0)
        hist = store.conn.execute(
            "SELECT change_7d, volume_7d_avg FROM indicator_history"
        ).fetchone()
        assert hist is not None
        self.assertAlmostEqual(hist["change_7d"], 50.0)
        self.assertAlmostEqual(hist["volume_7d_avg"], 200.0)

    def test_other_pair_history_does_not_fill_primary_gaps(self) -> None:
        store = self._open(
            [
                self._row(
                    "other",
                    self.now - timedelta(days=7),
                    100.0,
                    volume_h24=1000.0,
                    liquidity=10.0,
                ),
                self._row(
                    "other",
                    self.now,
                    200.0,
                    volume_h24=5000.0,
                    liquidity=10.0,
                ),
                self._row(
                    "primary",
                    self.now - timedelta(hours=2),
                    10.0,
                    volume_h24=700.0,
                    liquidity=100.0,
                ),
                self._row(
                    "primary",
                    self.now,
                    11.0,
                    volume_h24=700.0,
                    liquidity=9000.0,
                ),
            ]
        )
        self.assertEqual(store.pick_primary_pair("solana", "tokena"), "primary")
        tech = TechnicalIndicators(store, write_history_for_hot=False)
        written = tech.upsert_one("solana", "tokena", tier="cold")
        assert written is not None
        self.assertEqual(written["primary_pair_address"], "primary")
        self.assertIsNone(written["change_7d"])
        self.assertIsNone(written["volume_7d_avg"])
        self.assertNotEqual(written["volume_7d_avg"], 700.0 / 7.0)

    def test_history_keeps_explicit_null_and_fills_absent_keys(self) -> None:
        store = self._open(
            [
                self._row("pair1", self.now - timedelta(days=7), 100.0, volume_h24=100.0),
                self._row("pair1", self.now, 150.0, volume_h24=300.0),
            ]
        )
        tech = TechnicalIndicators(store, write_history_for_hot=False)
        written = tech.upsert_one("solana", "tokena", tier="warm")
        assert written is not None
        self.assertAlmostEqual(written["change_7d"], 50.0)
        store.insert_indicator_history(
            {
                "chain_id": "solana",
                "token_address": "tokena",
                "recorded_at": "2026-11-01T00:00:00Z",
                "change_7d": None,
            }
        )
        explicit = store.conn.execute(
            """
            SELECT change_7d, volume_7d_avg FROM indicator_history
            WHERE recorded_at = '2026-11-01T00:00:00Z'
            """
        ).fetchone()
        assert explicit is not None
        self.assertIsNone(explicit["change_7d"])
        self.assertAlmostEqual(explicit["volume_7d_avg"], written["volume_7d_avg"])
        store.insert_indicator_history(
            {
                "chain_id": "solana",
                "token_address": "tokena",
                "recorded_at": "2026-11-01T00:00:01Z",
            }
        )
        filled = store.conn.execute(
            """
            SELECT change_7d, volume_7d_avg FROM indicator_history
            WHERE recorded_at = '2026-11-01T00:00:01Z'
            """
        ).fetchone()
        assert filled is not None
        self.assertAlmostEqual(filled["change_7d"], 50.0)
        self.assertAlmostEqual(filled["volume_7d_avg"], written["volume_7d_avg"])

    def test_hot_reader_projects_lifecycle_columns(self) -> None:
        store = self._open(
            [self._row("pair1", self.now, 1.0, volume_h24=1.0)]
        )
        stamp = _iso(self.now)
        store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "tokena",
                "tier": "hot",
                "change_24h": 9.0,
                "change_7d": 15.0,
                "volume_7d_avg": 80.0,
                "tech_updated_at": stamp,
                "updated_at": stamp,
            }
        )
        store.close()
        self.store = None
        adapter = IndicatorDataAdapter(self.db_path)
        try:
            rows = adapter.list_hot_tokens(limit=10)
        finally:
            adapter.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["change_24h"], 9.0)
        self.assertEqual(rows[0]["change_7d"], 15.0)
        self.assertEqual(rows[0]["volume_7d_avg"], 80.0)


class LifecycleDeltaMigrateTest(unittest.TestCase):
    def test_fresh_schema_has_both_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fresh.db"
            store = IndicatorStore(path)
            try:
                for table in ("token_indicators", "indicator_history"):
                    cols = {
                        row["name"]
                        for row in store.conn.execute(f"PRAGMA table_info({table})")
                    }
                    self.assertIn("change_7d", cols)
                    self.assertIn("volume_7d_avg", cols)
            finally:
                store.close()

    def test_alter_adds_columns_and_upsert_roundtrip(self) -> None:
        sql_path = Path(__file__).resolve().parents[1] / "models" / "indicator_tables.sql"
        old_sql = sql_path.read_text(encoding="utf-8")
        old_sql = old_sql.replace(
            "    -- ~7d price change and mean daily volume. NULL when pair_history is short.\n",
            "",
        )
        old_sql = old_sql.replace("    change_7d REAL,\n", "")
        old_sql = old_sql.replace("    volume_7d_avg REAL,\n", "")
        self.assertNotIn("change_7d", old_sql)
        self.assertNotIn("volume_7d_avg", old_sql)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(old_sql)
            conn.execute(
                """
                INSERT INTO token_indicators (chain_id, token_address, change_24h, updated_at)
                VALUES ('solana', 'tokena', 1.5, '2020-01-01T00:00:00Z')
                """
            )
            conn.commit()
            conn.close()
            store = IndicatorStore(path)
            try:
                cols = {
                    row["name"]
                    for row in store.conn.execute("PRAGMA table_info(token_indicators)")
                }
                hist_cols = {
                    row["name"]
                    for row in store.conn.execute("PRAGMA table_info(indicator_history)")
                }
                self.assertIn("change_7d", cols)
                self.assertIn("volume_7d_avg", cols)
                self.assertIn("change_7d", hist_cols)
                self.assertIn("volume_7d_avg", hist_cols)
                kept = store.get_token_indicators("solana", "tokena", use_cache=False)
                assert kept is not None
                self.assertEqual(kept["change_24h"], 1.5)
                self.assertIsNone(kept["change_7d"])
                store.upsert_token_indicators(
                    {
                        "chain_id": "solana",
                        "token_address": "tokena",
                        "change_7d": 12.0,
                        "volume_7d_avg": 40.0,
                        "updated_at": "2026-11-01T00:00:00Z",
                    }
                )
                saved = store.get_token_indicators("solana", "tokena", use_cache=False)
                assert saved is not None
                self.assertEqual(saved["change_24h"], 1.5)
                self.assertEqual(saved["change_7d"], 12.0)
                self.assertEqual(saved["volume_7d_avg"], 40.0)
                store.insert_indicator_history(
                    {
                        "chain_id": "solana",
                        "token_address": "tokena",
                        "recorded_at": "2026-11-01T00:00:00Z",
                    }
                )
                hist = store.conn.execute(
                    "SELECT change_24h, change_7d, volume_7d_avg FROM indicator_history"
                ).fetchone()
                assert hist is not None
                self.assertEqual(hist["change_7d"], 12.0)
                self.assertEqual(hist["volume_7d_avg"], 40.0)
            finally:
                store.close()
