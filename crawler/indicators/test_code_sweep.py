"""Focused checks for the indicators CODE SWEEP fixes. Uses a temp SQLite file."""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import HOT_SEEN_WINDOW_SEC
from decision_engine.backtest.replay import coalesce_indicator_rows
from history import history_tier_for_token
from indicators.base import (
    INDICATOR_BUSY_TIMEOUT_MS,
    IndicatorStore,
    utc_now_iso,
)
from indicators.library_apply import apply_library_fields, technical_refresh_due
from indicators.technical import (
    TechnicalIndicators,
    order_technical_tokens,
    tier_from_token_row,
)
from indicators.technical_lib import compute_pandas_ta

_ROOT = Path(__file__).resolve().parents[1]
_PRIOR_PATH = _ROOT / "scripts" / "prior_ingest.py"
_prior_spec = importlib.util.spec_from_file_location("prior_ingest", _PRIOR_PATH)
assert _prior_spec is not None and _prior_spec.loader is not None
prior_ingest = importlib.util.module_from_spec(_prior_spec)
_prior_spec.loader.exec_module(prior_ingest)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_market(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE tokens (
            chain_id TEXT,
            token_address TEXT,
            has_pairs INTEGER,
            pair_count INTEGER,
            last_seen_at TEXT,
            last_enriched_at TEXT,
            updated_at TEXT
        );
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
        );
        """
    )


class LibraryRulesTest(unittest.TestCase):
    def test_macd_partial_keeps_previous_set_and_is_not_fresh(self) -> None:
        prev = {"macd": 1.0, "macd_signal": 2.0, "macd_hist": 3.0, "rsi_14": 40.0}
        fields, fresh = apply_library_fields(
            prev, {"macd": 9.0, "macd_signal": None, "macd_hist": 1.0, "rsi_14": 55.0}
        )
        self.assertFalse(fresh)
        self.assertEqual(fields["macd"], 1.0)
        self.assertEqual(fields["macd_signal"], 2.0)
        self.assertEqual(fields["macd_hist"], 3.0)
        self.assertEqual(fields["rsi_14"], 55.0)

    def test_complete_library_values_are_fresh(self) -> None:
        lib = {
            "rsi_14": 51.0,
            "macd": 1.0,
            "macd_signal": 0.5,
            "macd_hist": 0.5,
            "boll_upper": 3.0,
            "boll_mid": 2.0,
            "boll_lower": 1.0,
        }
        fields, fresh = apply_library_fields(None, lib)
        self.assertTrue(fresh)
        self.assertEqual(fields["macd_signal"], 0.5)
        self.assertEqual(fields["boll_lower"], 1.0)

    def test_honest_nulls_are_fresh(self) -> None:
        fields, fresh = apply_library_fields(None, {})
        self.assertTrue(fresh)
        self.assertIsNone(fields["rsi_14"])
        self.assertIsNone(fields["macd"])
        self.assertIsNone(fields["macd_signal"])
        self.assertIsNone(fields["macd_hist"])

    def test_technical_due_ignores_shared_updated_at(self) -> None:
        now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(
            technical_refresh_due(
                None,
                tier="hot",
                now=now,
                hot_interval_sec=300,
                warm_interval_sec=1800,
                cold_interval_sec=14400,
            )
        )
        recent = _iso(now - timedelta(seconds=30))
        self.assertFalse(
            technical_refresh_due(
                recent,
                tier="hot",
                now=now,
                hot_interval_sec=300,
                warm_interval_sec=1800,
                cold_interval_sec=14400,
            )
        )
        stale = _iso(now - timedelta(seconds=3000))
        self.assertTrue(
            technical_refresh_due(
                stale,
                tier="warm",
                now=now,
                hot_interval_sec=300,
                warm_interval_sec=1800,
                cold_interval_sec=14400,
            )
        )

    def test_compute_pandas_ta_macd_is_all_or_nothing(self) -> None:
        short = compute_pandas_ta([float(i) for i in range(1, 16)])
        macd = [short["macd"], short["macd_signal"], short["macd_hist"]]
        self.assertTrue(all(v is None for v in macd) or all(v is not None for v in macd))
        long = compute_pandas_ta([100.0 + (i % 5) * 0.1 for i in range(80)])
        macd_long = [long["macd"], long["macd_signal"], long["macd_hist"]]
        self.assertTrue(all(v is None for v in macd_long) or all(v is not None for v in macd_long))


class StoreBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ind.db"
        conn = sqlite3.connect(self.db_path)
        _seed_market(conn)
        conn.commit()
        conn.close()
        self.store = IndicatorStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_busy_timeout_matches_main_store(self) -> None:
        row = self.store.conn.execute("PRAGMA busy_timeout").fetchone()
        self.assertEqual(int(row[0]), INDICATOR_BUSY_TIMEOUT_MS)
        self.assertEqual(INDICATOR_BUSY_TIMEOUT_MS, 30000)

    def test_schema_has_tech_updated_at(self) -> None:
        cols = {
            r["name"]
            for r in self.store.conn.execute("PRAGMA table_info(token_indicators)")
        }
        self.assertIn("tech_updated_at", cols)

    def test_funding_upsert_does_not_bump_tech_updated_at(self) -> None:
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "tier": "hot",
                "rsi_14": 80.0,
                "macd": 1.0,
                "macd_signal": 0.4,
                "macd_hist": 0.6,
                "tech_updated_at": "2026-10-13T00:00:00Z",
                "updated_at": "2026-10-13T00:00:00Z",
            }
        )
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "jev_strength": 0.48,
                "updated_at": "2026-10-13T12:00:00Z",
            }
        )
        row = self.store.get_token_indicators("solana", "TokenA", use_cache=False)
        assert row is not None
        self.assertEqual(row["tech_updated_at"], "2026-10-13T00:00:00Z")
        self.assertEqual(row["rsi_14"], 80.0)
        self.assertEqual(row["updated_at"], "2026-10-13T12:00:00Z")
        self.assertEqual(row["jev_strength"], 0.48)

    def test_history_merge_fills_omitted_technicals(self) -> None:
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "rsi_14": 80.0,
                "macd": 1.0,
                "macd_signal": 0.4,
                "macd_hist": 0.6,
                "boll_upper": 3.0,
                "boll_mid": 2.0,
                "boll_lower": 1.0,
                "tech_updated_at": "2026-10-13T00:00:00Z",
                "updated_at": "2026-10-13T00:00:00Z",
            }
        )
        self.store.insert_indicator_history(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "recorded_at": "2026-10-13T00:00:01Z",
                "jev_strength": 0.48,
                "tier": "hot",
            }
        )
        hist = self.store.conn.execute(
            "SELECT rsi_14, macd, macd_signal, macd_hist, boll_mid, jev_strength "
            "FROM indicator_history"
        ).fetchone()
        self.assertEqual(hist["rsi_14"], 80.0)
        self.assertEqual(hist["macd"], 1.0)
        self.assertEqual(hist["macd_signal"], 0.4)
        self.assertEqual(hist["macd_hist"], 0.6)
        self.assertEqual(hist["boll_mid"], 2.0)
        self.assertEqual(hist["jev_strength"], 0.48)

    def test_explicit_null_rsi_is_not_backfilled(self) -> None:
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "rsi_14": 80.0,
                "updated_at": utc_now_iso(),
            }
        )
        self.store.insert_indicator_history(
            {
                "chain_id": "solana",
                "token_address": "TokenA",
                "recorded_at": "2026-10-13T00:00:01Z",
                "rsi_14": None,
            }
        )
        hist = self.store.conn.execute(
            "SELECT rsi_14 FROM indicator_history"
        ).fetchone()
        self.assertIsNone(hist["rsi_14"])

    def test_history_near_prefers_non_null_rsi_on_tie(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        target = _iso(now - timedelta(seconds=3600))
        self.store.conn.execute(
            """
            INSERT INTO indicator_history (
                chain_id, token_address, recorded_at, rsi_14, jev_strength
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("solana", "tokena", target, 80.0, None),
        )
        self.store.conn.execute(
            """
            INSERT INTO indicator_history (
                chain_id, token_address, recorded_at, rsi_14, jev_strength
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("solana", "tokena", target, None, 0.48),
        )
        self.store.conn.commit()
        row = self.store.get_indicator_history_near(
            "solana", "tokena", lookback_sec=3600
        )
        assert row is not None
        self.assertEqual(row["rsi_14"], 80.0)

    def test_pick_primary_pair_uses_latest_liquidity(self) -> None:
        t0 = "2026-10-13T00:00:00Z"
        t1 = "2026-10-13T01:00:00Z"
        rows = [
            ("solana", "drained", "tokena", t0, 1.0, 1_000_000.0),
            ("solana", "drained", "tokena", t1, 1.0, 10.0),
            ("solana", "active", "tokena", t0, 1.0, 100.0),
            ("solana", "active", "tokena", t1, 1.0, 500.0),
        ]
        for chain, pair, token, ts, price, liq in rows:
            self.store.conn.execute(
                """
                INSERT INTO pair_history (
                    chain_id, pair_address, token_address, recorded_at,
                    price_usd, liquidity_usd
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chain, pair, token, ts, price, liq),
            )
        self.store.conn.commit()
        self.assertEqual(self.store.pick_primary_pair("solana", "tokena"), "active")

    def test_purge_keeps_recent_history(self) -> None:
        old = _iso(datetime.now(timezone.utc) - timedelta(days=40))
        recent = utc_now_iso()
        for ts in (old, recent):
            self.store.insert_indicator_history(
                {
                    "chain_id": "solana",
                    "token_address": "TokenA",
                    "recorded_at": ts,
                    "rsi_14": 10.0,
                }
            )
        removed = self.store.purge_old_indicator_history(14)
        self.assertEqual(removed, 1)
        left = self.store.conn.execute(
            "SELECT recorded_at FROM indicator_history"
        ).fetchall()
        self.assertEqual([r["recorded_at"] for r in left], [recent])


class TechnicalPassTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ind.db"
        conn = sqlite3.connect(self.db_path)
        _seed_market(conn)
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO tokens (
                chain_id, token_address, has_pairs, pair_count,
                last_seen_at, last_enriched_at, updated_at
            ) VALUES ('solana', 'tokena', 1, 1, ?, NULL, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, token_address, recorded_at,
                price_usd, volume_h1, liquidity_usd, tier
            ) VALUES
                ('solana', 'pair1', 'tokena', '2026-10-13T00:00:00Z', 1.0, 10, 100, 'hot'),
                ('solana', 'pair1', 'tokena', '2026-10-13T00:05:00Z', 1.1, 12, 110, 'hot')
            """
        )
        conn.commit()
        conn.close()
        self.store = IndicatorStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_only_due_uses_tech_updated_at_only(self) -> None:
        now = utc_now_iso()
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "tokena",
                "tier": "hot",
                "rsi_14": 77.0,
                "tech_updated_at": now,
                "updated_at": "2020-01-01T00:00:00Z",
            }
        )
        tech = TechnicalIndicators(self.store, write_history_for_hot=False)
        stats = tech.run_batch(only_due=True, hot_interval_sec=300)
        self.assertEqual(stats["skip"], 1)
        self.assertEqual(stats["ok"], 0)

        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "tokena",
                "tech_updated_at": "2020-01-01T00:00:00Z",
                "updated_at": now,
            }
        )
        stats = tech.run_batch(only_due=True, hot_interval_sec=300)
        self.assertEqual(stats["ok"], 1)
        self.assertEqual(stats["skip"], 0)

    def test_preserved_library_values_do_not_bump_tech_clock(self) -> None:
        self.store.upsert_token_indicators(
            {
                "chain_id": "solana",
                "token_address": "tokena",
                "tier": "hot",
                "rsi_14": 77.0,
                "macd": 1.0,
                "macd_signal": 2.0,
                "macd_hist": 3.0,
                "boll_upper": 4.0,
                "boll_mid": 3.0,
                "boll_lower": 2.0,
                "tech_updated_at": "2020-01-01T00:00:00Z",
                "updated_at": "2020-01-01T00:00:00Z",
            }
        )
        tech = TechnicalIndicators(self.store, write_history_for_hot=True)
        row = tech.upsert_one("solana", "tokena", tier="hot")
        assert row is not None
        saved = self.store.get_token_indicators("solana", "tokena", use_cache=False)
        assert saved is not None
        self.assertEqual(saved["rsi_14"], 77.0)
        self.assertEqual(saved["macd"], 1.0)
        self.assertEqual(saved["macd_signal"], 2.0)
        self.assertEqual(saved["macd_hist"], 3.0)
        self.assertEqual(saved["tech_updated_at"], "2020-01-01T00:00:00Z")
        self.assertEqual(saved["updated_at"], "2020-01-01T00:00:00Z")
        n_hist = self.store.conn.execute(
            "SELECT COUNT(*) FROM indicator_history"
        ).fetchone()[0]
        self.assertEqual(int(n_hist), 0)


class MigrationTest(unittest.TestCase):
    def test_adds_tech_updated_at_to_existing_table(self) -> None:
        sql = (_ROOT / "models" / "indicator_tables.sql").read_text(encoding="utf-8")
        old = sql.replace(
            "    -- Set only when technical recomputes RSI/MACD/BOLL. Not bumped by funding/sentiment.\n"
            "    tech_updated_at TEXT,\n",
            "",
        )
        self.assertNotIn("tech_updated_at", old)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(old)
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
                    r["name"]
                    for r in store.conn.execute("PRAGMA table_info(token_indicators)")
                }
                self.assertIn("tech_updated_at", cols)
                row = store.get_token_indicators("solana", "tokena", use_cache=False)
                assert row is not None
                self.assertIsNone(row["tech_updated_at"])
                self.assertEqual(row["updated_at"], "2020-01-01T00:00:00Z")
            finally:
                store.close()


class ReplayCoalesceTest(unittest.TestCase):
    def test_later_null_does_not_wipe_rsi(self) -> None:
        merged = coalesce_indicator_rows(
            {"rsi_14": 100.0, "macd": 1.0, "jev_strength": None, "tier": "hot"},
            {"rsi_14": None, "macd": None, "jev_strength": 0.48, "tier": "hot"},
        )
        self.assertEqual(merged["rsi_14"], 100.0)
        self.assertEqual(merged["macd"], 1.0)
        self.assertEqual(merged["jev_strength"], 0.48)


class PriorIngestTest(unittest.TestCase):
    def test_incremental_keeps_other_tables_and_rebuild_backs_up(self) -> None:
        schema = _ROOT / "models" / "prior_tables.sql"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "prior.db")
            conn = prior_ingest.prepare_prior_connection(
                db_path, str(schema), rebuild=False
            )
            conn.execute(
                "CREATE TABLE cex_scope_snapshots (id INTEGER PRIMARY KEY, note TEXT)"
            )
            conn.execute("INSERT INTO cex_scope_snapshots (note) VALUES ('keep')")
            conn.execute(
                """
                INSERT INTO token_outcomes (token_address, chain, source, outcome)
                VALUES ('mint1', 'solana', 'solrpds', 'inactive')
                """
            )
            conn.execute(
                """
                INSERT INTO token_outcomes (token_address, chain, source, outcome)
                VALUES ('mint2', 'solana', 'other', 'kept')
                """
            )
            conn.commit()
            conn.close()

            conn = prior_ingest.prepare_prior_connection(
                db_path, str(schema), rebuild=False
            )
            notes = conn.execute("SELECT note FROM cex_scope_snapshots").fetchall()
            self.assertEqual([r[0] for r in notes], ["keep"])
            sources = conn.execute(
                "SELECT source FROM token_outcomes ORDER BY source"
            ).fetchall()
            self.assertEqual([r[0] for r in sources], ["other"])
            conn.close()
            self.assertTrue(Path(db_path).is_file())

            conn = prior_ingest.prepare_prior_connection(
                db_path, str(schema), rebuild=True
            )
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("cex_scope_snapshots", tables)
            self.assertIn("token_outcomes", tables)
            conn.close()
            backups = list(Path(tmp).glob("prior.db.bak-*"))
            self.assertEqual(len(backups), 1)
            bak = sqlite3.connect(backups[0])
            kept = bak.execute("SELECT note FROM cex_scope_snapshots").fetchone()
            self.assertEqual(kept[0], "keep")
            bak.close()


def _order_ids(
    rows: list[dict[str, Any]], *, now: datetime, force_full: bool = False
) -> list[Any]:
    ordered = order_technical_tokens(
        rows,
        tier_of=lambda row: str(row["tier"]),
        tech_updated_at_of=lambda row: row.get("tech_updated_at"),
        now=now,
        hot_interval_sec=300,
        warm_interval_sec=1800,
        cold_interval_sec=14400,
        force_full=force_full,
    )
    return [row["id"] for row in ordered]


class TechnicalPriorityTest(unittest.TestCase):
    def test_priority_order_puts_hot_due_before_cold(self) -> None:
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        rows: list[dict[str, Any]] = [
            {"id": "cold", "tier": "cold", "tech_updated_at": None},
            {
                "id": "hot-fresh",
                "tier": "hot",
                "tech_updated_at": _iso(now - timedelta(seconds=30)),
            },
            {"id": "warm-due", "tier": "warm", "tech_updated_at": None},
            {"id": "hot-due", "tier": "hot", "tech_updated_at": None},
            {
                "id": "hot-stale",
                "tier": "hot",
                "tech_updated_at": _iso(now - timedelta(seconds=301)),
            },
            {
                "id": "warm-fresh",
                "tier": "warm",
                "tech_updated_at": _iso(now - timedelta(seconds=400)),
            },
        ]
        ids = _order_ids(rows, now=now)
        self.assertEqual(ids[:3], ["hot-due", "hot-stale", "warm-due"])
        self.assertLess(ids.index("hot-due"), ids.index("cold"))
        self.assertLess(ids.index("hot-stale"), ids.index("cold"))
        self.assertLess(ids.index("warm-due"), ids.index("cold"))
        # Inside the interval: stay with the tail, after due hot/warm.
        self.assertLess(ids.index("cold"), ids.index("hot-fresh"))
        self.assertLess(ids.index("cold"), ids.index("warm-fresh"))

    def test_force_full_still_puts_hot_before_cold(self) -> None:
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        rows: list[dict[str, Any]] = [
            {"id": "cold", "tier": "cold", "tech_updated_at": None},
            {
                "id": "hot-fresh",
                "tier": "hot",
                "tech_updated_at": _iso(now),
            },
        ]
        ids = _order_ids(rows, now=now, force_full=True)
        self.assertEqual(ids, ["hot-fresh", "cold"])

    def test_tier_from_token_row_hot_warm_cold(self) -> None:
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        window = 3600
        self.assertEqual(
            tier_from_token_row(
                {"last_seen_at": _iso(now), "has_pairs": 1},
                now=now,
                hot_seen_window_sec=window,
            ),
            "hot",
        )
        self.assertEqual(
            tier_from_token_row(
                {
                    "last_seen_at": _iso(now - timedelta(seconds=window + 60)),
                    "has_pairs": 1,
                },
                now=now,
                hot_seen_window_sec=window,
            ),
            "warm",
        )
        self.assertEqual(
            tier_from_token_row(
                {
                    "last_seen_at": _iso(now - timedelta(seconds=window * 6 + 60)),
                    "has_pairs": 0,
                },
                now=now,
                hot_seen_window_sec=window,
            ),
            "cold",
        )
        self.assertEqual(
            tier_from_token_row(
                {"last_seen_at": None, "updated_at": None, "has_pairs": 1},
                now=now,
                hot_seen_window_sec=window,
            ),
            "cold",
        )


class TechnicalPriorityPassTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "ind.db"
        self.conn = sqlite3.connect(self.db_path)
        _seed_market(self.conn)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        if hasattr(self, "store"):
            self.store.close()
        self._tmp.cleanup()

    def _add(self, address: str, last_seen: str) -> None:
        self.conn.execute(
            """
            INSERT INTO tokens (
                chain_id, token_address, has_pairs, pair_count,
                last_seen_at, last_enriched_at, updated_at
            ) VALUES ('solana', ?, 1, 1, ?, NULL, ?)
            """,
            (address, last_seen, last_seen),
        )
        self.conn.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, token_address, recorded_at,
                price_usd, volume_h1, liquidity_usd, tier
            ) VALUES ('solana', ?, ?, ?, 1.0, 10, 100, 'hot')
            """,
            (address + "pair", address, last_seen),
        )
        self.conn.commit()

    def _open(self) -> TechnicalIndicators:
        self.store = IndicatorStore(self.db_path)
        return TechnicalIndicators(self.store, write_history_for_hot=False)

    def test_run_batch_puts_hot_due_before_cold(self) -> None:
        now = datetime.now(timezone.utc)
        window = HOT_SEEN_WINDOW_SEC
        self._add("colddue", _iso(now - timedelta(seconds=window * 6 + 3600)))
        self._add("warmdue", _iso(now - timedelta(seconds=window + 60)))
        self._add("hotdue", _iso(now))
        tech = self._open()
        self.assertEqual(history_tier_for_token(self.store, "solana", "hotdue"), "hot")
        self.assertEqual(history_tier_for_token(self.store, "solana", "warmdue"), "warm")
        self.assertEqual(history_tier_for_token(self.store, "solana", "colddue"), "cold")
        listed = {row["token_address"]: row for row in self.store.list_tokens_with_history()}
        # Raw scan order is cold first. The pass must not follow that.
        self.store.list_tokens_with_history = lambda: [  # type: ignore[method-assign]
            listed["colddue"],
            listed["warmdue"],
            listed["hotdue"],
        ]
        calls: list[tuple[str, str | None]] = []

        def _upsert(
            chain_id: str,
            token_address: str,
            *,
            tier: str | None = None,
            force_full: bool = False,
        ) -> dict[str, str | None]:
            del force_full
            calls.append((token_address, tier))
            self.store.upsert_token_indicators(
                {
                    "chain_id": chain_id,
                    "token_address": token_address,
                    "tier": tier,
                    "tech_updated_at": utc_now_iso(),
                }
            )
            return {"tier": tier}

        tech.upsert_one = _upsert  # type: ignore[method-assign]
        stats = tech.run_batch(
            only_due=True,
            hot_interval_sec=300,
            warm_interval_sec=1800,
            cold_interval_sec=14400,
        )
        self.assertEqual(
            calls,
            [("hotdue", "hot"), ("warmdue", "warm"), ("colddue", "cold")],
        )
        self.assertEqual(stats["hot_due"], 1)
        self.assertEqual(stats["warm_due"], 1)
        self.assertEqual(stats["ok"], 3)
        saved = self.store.get_token_indicators("solana", "hotdue", use_cache=False)
        assert saved is not None
        self.assertEqual(saved["tier"], "hot")
        self.assertIsNotNone(saved["tech_updated_at"])

    def test_tail_refreshes_hot_due_before_the_next_cold(self) -> None:
        now = datetime.now(timezone.utc)
        window = HOT_SEEN_WINDOW_SEC
        self._add("cold1", _iso(now - timedelta(seconds=window * 6 + 3600)))
        self._add("cold2", _iso(now - timedelta(seconds=window * 6 + 7200)))
        self._add("hotdue", _iso(now))
        tech = self._open()
        listed = {row["token_address"]: row for row in self.store.list_tokens_with_history()}
        self.store.list_tokens_with_history = lambda: [  # type: ignore[method-assign]
            listed["cold1"],
            listed["cold2"],
            listed["hotdue"],
        ]
        clock = {"t": 0.0}
        calls: list[str] = []

        def _upsert(
            chain_id: str,
            token_address: str,
            *,
            tier: str | None = None,
            force_full: bool = False,
        ) -> dict[str, str | None]:
            del force_full
            calls.append(token_address)
            if token_address == "hotdue":
                self.store.upsert_token_indicators(
                    {
                        "chain_id": chain_id,
                        "token_address": token_address,
                        "tier": tier,
                        "tech_updated_at": utc_now_iso(),
                    }
                )
            elif calls.count("cold1") == 1 and token_address == "cold1":
                self.store.upsert_token_indicators(
                    {
                        "chain_id": "solana",
                        "token_address": "hotdue",
                        "tier": "hot",
                        "tech_updated_at": "2020-01-01T00:00:00Z",
                    }
                )
                clock["t"] = 10_000.0
            return {"tier": tier}

        tech.upsert_one = _upsert  # type: ignore[method-assign]
        tech.run_batch(
            only_due=True,
            hot_interval_sec=300,
            _monotonic=lambda: clock["t"],
        )
        self.assertEqual(calls, ["hotdue", "cold1", "hotdue", "cold2"])

    def test_warm_backlog_refreshes_hot_before_two_intervals(self) -> None:
        """A long due-warm queue must not hold hot past the decision window.

        Each warm upsert advances the scheduler clock. The hot token is
        marked stale once that clock crosses one hot interval, and must be
        refreshed again before two intervals while warm tokens remain.
        """
        now = datetime.now(timezone.utc)
        window = HOT_SEEN_WINDOW_SEC
        warm_ids = [f"warm{i}" for i in range(6)]
        for address in warm_ids:
            self._add(address, _iso(now - timedelta(seconds=window + 60)))
        self._add("hotdue", _iso(now))
        tech = self._open()
        listed = {row["token_address"]: row for row in self.store.list_tokens_with_history()}
        self.store.list_tokens_with_history = lambda: [  # type: ignore[method-assign]
            *[listed[address] for address in warm_ids],
            listed["hotdue"],
        ]
        clock = {"t": 0.0}
        calls: list[str] = []
        hot_at: list[float] = []
        stale = {"on": False}
        interval = 300

        def _upsert(
            chain_id: str,
            token_address: str,
            *,
            tier: str | None = None,
            force_full: bool = False,
        ) -> dict[str, str | None]:
            del force_full
            calls.append(token_address)
            if token_address == "hotdue":
                hot_at.append(clock["t"])
            self.store.upsert_token_indicators(
                {
                    "chain_id": chain_id,
                    "token_address": token_address,
                    "tier": tier,
                    "tech_updated_at": utc_now_iso(),
                }
            )
            clock["t"] += 120
            if token_address != "hotdue" and clock["t"] >= interval and not stale["on"]:
                stale["on"] = True
                self.store.upsert_token_indicators(
                    {
                        "chain_id": "solana",
                        "token_address": "hotdue",
                        "tier": "hot",
                        "tech_updated_at": "2020-01-01T00:00:00Z",
                    }
                )
            return {"tier": tier}

        tech.upsert_one = _upsert  # type: ignore[method-assign]
        tech.run_batch(
            only_due=True,
            hot_interval_sec=interval,
            warm_interval_sec=1800,
            cold_interval_sec=14400,
            _monotonic=lambda: clock["t"],
        )
        self.assertGreaterEqual(len(hot_at), 2)
        self.assertLess(hot_at[1] - hot_at[0], 2 * interval)
        second = calls.index("hotdue", calls.index("hotdue") + 1)
        warm_before = [c for c in calls[:second] if c in warm_ids]
        warm_after = [c for c in calls[second + 1 :] if c in warm_ids]
        self.assertTrue(warm_before)
        self.assertTrue(warm_after)
        self.assertLess(len(warm_before), len(warm_ids))
        self.assertEqual(calls[0], "hotdue")

    def test_hot_backlog_continues_while_warm_advances(self) -> None:
        """Due hot is sliced, not restarted, when each token fills the interval."""
        now = datetime.now(timezone.utc)
        window = HOT_SEEN_WINDOW_SEC
        hot_ids = ["hot1", "hot2", "hot3"]
        warm_ids = ["warm1", "warm2", "warm3"]
        for address in warm_ids:
            self._add(address, _iso(now - timedelta(seconds=window + 60)))
        for address in hot_ids:
            self._add(address, _iso(now))
        tech = self._open()
        listed = {row["token_address"]: row for row in self.store.list_tokens_with_history()}
        self.store.list_tokens_with_history = lambda: [  # type: ignore[method-assign]
            *[listed[address] for address in warm_ids],
            *[listed[address] for address in hot_ids],
        ]
        clock = {"t": 0.0}
        calls: list[str] = []
        interval = 10

        def _upsert(
            chain_id: str,
            token_address: str,
            *,
            tier: str | None = None,
            force_full: bool = False,
        ) -> dict[str, str | None]:
            del force_full
            calls.append(token_address)
            self.store.upsert_token_indicators(
                {
                    "chain_id": chain_id,
                    "token_address": token_address,
                    "tier": tier,
                    "tech_updated_at": utc_now_iso(),
                }
            )
            clock["t"] += interval
            return {"tier": tier}

        tech.upsert_one = _upsert  # type: ignore[method-assign]
        tech.run_batch(
            only_due=True,
            hot_interval_sec=interval,
            _monotonic=lambda: clock["t"],
        )
        self.assertEqual(
            calls,
            ["hot1", "warm1", "hot2", "warm2", "hot3", "warm3"],
        )


if __name__ == "__main__":
    unittest.main()
