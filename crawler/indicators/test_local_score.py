"""local_score formula and the indicator write path that stores it."""

from __future__ import annotations

import math
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from indicators.base import IndicatorStore
from indicators.local_score import compute_local_score
from indicators.technical import TechnicalIndicators


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LocalScoreFormulaTest(unittest.TestCase):
    def test_all_missing_inputs_return_none(self) -> None:
        self.assertIsNone(compute_local_score({}))
        self.assertIsNone(
            compute_local_score(
                {"change_5m": None, "volume_strength": "nope", "spike_ratio": 12, "spike": 1}
            )
        )
        self.assertIsNone(compute_local_score({"jev_strength": 1.0, "spike": 1}))

    def test_neutral_volume_and_flat_change_is_zero(self) -> None:
        self.assertEqual(
            compute_local_score({"volume_strength": 1.0, "change_5m": 0.0}),
            0.0,
        )

    def test_none_legs_are_skipped(self) -> None:
        change_only = compute_local_score({"change_5m": 0.0, "volume_strength": None})
        self.assertEqual(change_only, 0.0)
        volume_only = compute_local_score({"volume_strength": 5.0})
        self.assertEqual(volume_only, 0.35)

    def test_extreme_change_is_clipped_inside_unit_interval(self) -> None:
        rug = compute_local_score({"change_5m": 10_000.0, "volume_strength": 1_000.0})
        crash = compute_local_score(
            {
                "change_5m": -10_000.0,
                "volume_strength": -100.0,
                "spike_ratio": 100.0,
                "spike": 1,
            },
            liquidity_usd=0.0,
        )
        assert rug is not None and crash is not None
        self.assertGreaterEqual(rug, -1.0)
        self.assertLessEqual(rug, 1.0)
        self.assertEqual(crash, -1.0)
        saturated = round(math.tanh(10_000.0 / 20.0) * 0.45 + 0.35, 6)
        self.assertEqual(rug, saturated)
        self.assertLess(rug, 0.9)

    def test_non_finite_change_is_ignored(self) -> None:
        self.assertEqual(
            compute_local_score({"volume_strength": 1.0, "change_5m": float("nan")}),
            0.0,
        )
        self.assertEqual(
            compute_local_score({"volume_strength": 1.0, "change_5m": float("inf")}),
            0.0,
        )

    def test_liquidity_tiers_and_spike_penalty(self) -> None:
        base = {"volume_strength": 1.0, "change_5m": 0.0}
        self.assertEqual(compute_local_score(base, liquidity_usd=1_000.0), -0.15)
        self.assertEqual(compute_local_score(base, liquidity_usd=20_000.0), 0.0)
        self.assertEqual(compute_local_score(base, liquidity_usd=50_000.0), 0.1)
        self.assertIsNone(compute_local_score({}, liquidity_usd=None))
        penalized = compute_local_score({**base, "spike_ratio": 21.0, "spike": 1})
        self.assertEqual(penalized, -0.25)
        flagged = compute_local_score({**base, "spike": 1})
        self.assertEqual(flagged, -0.15)

    def test_jev_strength_does_not_move_the_score(self) -> None:
        plain = compute_local_score({"volume_strength": 2.0, "change_5m": 10.0})
        with_jev = compute_local_score(
            {"volume_strength": 2.0, "change_5m": 10.0, "jev_strength": 99.0}
        )
        self.assertEqual(plain, with_jev)
        assert plain is not None
        self.assertGreaterEqual(plain, -1.0)
        self.assertLessEqual(plain, 1.0)


class LocalScoreWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ind.db"
        self.store = IndicatorStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_schema_has_local_score_columns(self) -> None:
        live = {
            row["name"]
            for row in self.store.conn.execute("PRAGMA table_info(token_indicators)")
        }
        hist = {
            row["name"]
            for row in self.store.conn.execute("PRAGMA table_info(indicator_history)")
        }
        self.assertIn("local_score", live)
        self.assertIn("local_score_updated_at", live)
        self.assertIn("local_score", hist)
        self.assertIn("jev_strength", live)

    def test_upsert_stores_score_and_funding_write_does_not_clobber_it(self) -> None:
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "tier": "hot",
                "volume_strength": 1.0,
                "change_5m": 0.0,
                "updated_at": "2026-11-05T00:00:00Z",
            }
        )
        row = self.store.get_token_indicators("solana", "TokenA", use_cache=False)
        assert row is not None
        self.assertEqual(row["local_score"], 0.0)
        self.assertEqual(row["local_score_updated_at"], "2026-11-05T00:00:00Z")

        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "jev_strength": 0.9,
                "updated_at": "2026-11-05T01:00:00Z",
            }
        )
        kept = self.store.get_token_indicators("solana", "TokenA", use_cache=False)
        assert kept is not None
        self.assertEqual(kept["local_score"], 0.0)
        self.assertEqual(kept["local_score_updated_at"], "2026-11-05T00:00:00Z")
        self.assertEqual(kept["jev_strength"], 0.9)
        self.assertEqual(kept["updated_at"], "2026-11-05T01:00:00Z")

    def test_clearing_inputs_clears_score_and_spike_uses_stored_tech(self) -> None:
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "volume_strength": 5.0,
                "change_5m": 0.0,
                "updated_at": "2026-11-05T00:00:00Z",
            }
        )
        hot = self.store.get_token_indicators("solana", "TokenA", use_cache=False)
        assert hot is not None
        self.assertEqual(hot["local_score"], 0.35)

        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "spike_ratio": 21.0,
                "spike": 1,
                "updated_at": "2026-11-05T00:05:00Z",
            }
        )
        spiked = self.store.get_token_indicators("solana", "TokenA", use_cache=False)
        assert spiked is not None
        self.assertEqual(spiked["local_score"], round(0.35 - 0.25, 6))
        self.assertEqual(spiked["volume_strength"], 5.0)

        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "volume_strength": None,
                "change_5m": None,
                "spike_ratio": None,
                "spike": None,
                "updated_at": "2026-11-05T00:10:00Z",
            }
        )
        cleared = self.store.get_token_indicators("solana", "TokenA", use_cache=False)
        assert cleared is not None
        self.assertIsNone(cleared["local_score"])

    def test_history_fill_copies_local_score(self) -> None:
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "tier": "hot",
                "volume_strength": 1.0,
                "change_5m": 20.0,
                "updated_at": "2026-11-05T00:00:00Z",
            }
        )
        self.store.insert_indicator_history(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "recorded_at": "2026-11-05T00:00:01Z",
                "tier": "hot",
            }
        )
        hist = self.store.conn.execute(
            "SELECT local_score, jev_strength FROM indicator_history"
        ).fetchone()
        expected = compute_local_score({"volume_strength": 1.0, "change_5m": 20.0})
        self.assertEqual(hist["local_score"], expected)
        self.assertIsNone(hist["jev_strength"])

    def test_top_sort_rejects_jev_and_orders_by_local_score(self) -> None:
        self.assertEqual(
            IndicatorStore.TOP_SORT_COLUMNS,
            ("local_score", "volume_strength", "change_5m"),
        )
        self.assertNotIn("jev_strength", IndicatorStore.TOP_SORT_COLUMNS)
        with self.assertRaises(ValueError):
            self.store.list_top_token_indicators(sort="jev_strength", limit=5)
        self.store.conn.executemany(
            """
            INSERT INTO token_indicators (
                chain_id, token_address, local_score, volume_strength, change_5m,
                jev_strength, updated_at
            ) VALUES ('solana', ?, ?, ?, ?, ?, '2026-11-05T00:00:00Z')
            """,
            [
                ("low", 0.1, 9.0, 50.0, 0.99),
                ("high", 0.8, 0.1, -10.0, 0.01),
                ("blank", None, 4.0, 80.0, 0.5),
            ],
        )
        self.store.conn.commit()
        rows = self.store.list_top_token_indicators(sort="local_score", limit=10)
        self.assertEqual([r["token_address"] for r in rows], ["high", "low", "blank"])
        default_rows = self.store.list_top_token_indicators(limit=10)
        self.assertEqual(
            [r["token_address"] for r in default_rows],
            ["high", "low", "blank"],
        )


class LocalScoreTechnicalPassTest(unittest.TestCase):
    def test_technical_upsert_refreshes_local_score_from_pair_liquidity(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind.db"
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
                    price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier
                ) VALUES ('solana', 'pair1', 'tokena', ?, ?, NULL, ?, ?, 1, 'hot')
                """,
                [
                    (_iso(now - timedelta(seconds=440)), 100.0, 10.0, 80_000.0),
                    (_iso(now), 110.0, 10.0, 80_000.0),
                ],
            )
            conn.commit()
            conn.close()
            store = IndicatorStore(path)
            try:
                tech = TechnicalIndicators(store, write_history_for_hot=True)
                written = tech.upsert_one("solana", "tokena", tier="hot")
                assert written is not None
                saved = store.get_token_indicators("solana", "tokena", use_cache=False)
                assert saved is not None
                expected = compute_local_score(
                    saved,
                    liquidity_usd=80_000.0,
                )
                self.assertIsNotNone(expected)
                assert expected is not None
                self.assertGreaterEqual(expected, -1.0)
                self.assertLessEqual(expected, 1.0)
                self.assertAlmostEqual(saved["change_5m"], 10.0)
                self.assertEqual(saved["local_score"], expected)
                self.assertGreater(saved["local_score"], 0.1)
                self.assertTrue(saved["local_score_updated_at"])
                self.assertIsNone(saved["jev_strength"])
                hist = store.conn.execute(
                    "SELECT local_score FROM indicator_history"
                ).fetchone()
                self.assertIsNotNone(hist)
                assert hist is not None
                self.assertEqual(hist["local_score"], expected)
            finally:
                store.close()


class LocalScoreMigrateTest(unittest.TestCase):
    def test_alter_adds_local_score_to_old_schema(self) -> None:
        sql_path = Path(__file__).resolve().parents[1] / "models" / "indicator_tables.sql"
        old_sql = sql_path.read_text(encoding="utf-8")
        old_sql = old_sql.replace(
            "    -- Local旁路 rank score (replaces jevs.fun for ordering). Legacy jev_* stay.\n",
            "",
        )
        old_sql = old_sql.replace("    local_score REAL,\n", "")
        old_sql = old_sql.replace("    local_score_updated_at TEXT,\n", "")
        self.assertNotIn("local_score", old_sql)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(old_sql)
            conn.execute(
                """
                INSERT INTO token_indicators (chain_id, token_address, updated_at)
                VALUES ('solana', 'tokena', '2020-01-01T00:00:00Z')
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
                hist = {
                    row["name"]
                    for row in store.conn.execute("PRAGMA table_info(indicator_history)")
                }
                self.assertIn("local_score", cols)
                self.assertIn("local_score_updated_at", cols)
                self.assertIn("local_score", hist)
                self.assertIn("jev_strength", cols)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
