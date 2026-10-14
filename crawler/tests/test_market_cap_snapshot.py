"""Persist DexScreener marketCap / fdv for G-5. No network or production DB.

Present fields are stored. Omitted or non-numeric fields stay NULL.
fdv is not copied into market_cap. Old rows are not backfilled from raw_json.
The turnover ratio is not computed here.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daily_check import HOT_LIQ_CHECK_NAME, CheckReport, check_hot_liquidity_coverage
from history import (
    extract_metrics,
    market_cap_null_coverage,
    maybe_snapshot_pair,
    pair_history_column_sql,
)
from indicators.base import IndicatorStore
from store import Store


# Root-level shape of a DexScreener pair object (fdv and marketCap optional).
PAIR_WITH_CAPS = {
    "chainId": "solana",
    "dexId": "raydium",
    "pairAddress": "pairwithmcap111111111111111111111111111111",
    "baseToken": {
        "address": "mintwithmcap11111111111111111111111111111",
        "name": "Cap Coin",
        "symbol": "CAP",
    },
    "quoteToken": {
        "address": "so11111111111111111111111111111111111111112",
        "symbol": "SOL",
    },
    "priceNative": "0.000021",
    "priceUsd": "0.0042",
    "txns": {"h24": {"buys": 40, "sells": 15}, "h1": {"buys": 4, "sells": 1}},
    "volume": {"h24": 250000.5, "h6": 80000, "h1": 12000, "m5": 400},
    "priceChange": {"h24": 12.5},
    "liquidity": {"usd": 80000, "base": 1000, "quote": 20},
    "fdv": 5_000_000,
    "marketCap": 1_250_000.5,
    "pairCreatedAt": 1_700_000_000_000,
}

PAIR_OMITTED = {
    "chainId": "solana",
    "dexId": "raydium",
    "pairAddress": "pairomittedmcap111111111111111111111111111",
    "baseToken": {
        "address": "mintomittedmcap111111111111111111111111111",
        "name": "No Cap",
        "symbol": "NONE",
    },
    "priceUsd": "0.5",
    "volume": {"h24": 900, "h1": 30},
    "liquidity": {"usd": 60000},
    "txns": {"h24": {"buys": 2, "sells": 1}},
}


def _iso(age_sec: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pair_cols(store: Store, pair_address: str) -> sqlite3.Row:
    return store._conn.execute(
        "SELECT market_cap, fdv FROM pairs WHERE chain_id = ? AND pair_address = ?",
        ("solana", pair_address),
    ).fetchone()


class ExtractMarketCapTests(unittest.TestCase):
    def test_present_root_fields(self) -> None:
        metrics = extract_metrics(PAIR_WITH_CAPS)
        self.assertEqual(metrics["market_cap"], 1_250_000.5)
        self.assertEqual(metrics["fdv"], 5_000_000.0)
        self.assertEqual(metrics["volume_h24"], 250000.5)
        self.assertEqual(metrics["liquidity_usd"], 80000.0)
        self.assertNotIn("turnover", metrics)

    def test_string_numbers_and_snake_alias(self) -> None:
        metrics = extract_metrics(
            {
                "priceUsd": "1",
                "market_cap": "1250.5",
                "fdv": "9000",
                "volume": {"h24": "10"},
            }
        )
        self.assertEqual(metrics["market_cap"], 1250.5)
        self.assertEqual(metrics["fdv"], 9000.0)

    def test_omitted_invalid_and_no_fdv_fallback(self) -> None:
        omitted = extract_metrics({"priceUsd": "1.5", "volume": {"h24": 9}})
        self.assertIsNone(omitted["market_cap"])
        self.assertIsNone(omitted["fdv"])
        self.assertIsNone(omitted["liquidity_usd"])

        fdv_only = extract_metrics({"fdv": 42, "volume": {"h24": 3}})
        self.assertIsNone(fdv_only["market_cap"])
        self.assertEqual(fdv_only["fdv"], 42.0)

        present_null = extract_metrics({"marketCap": None, "market_cap": 5, "fdv": ""})
        self.assertIsNone(present_null["market_cap"])
        self.assertIsNone(present_null["fdv"])

        bad = extract_metrics(
            {"marketCap": "n/a", "fdv": True, "liquidity": {"usd": 10}}
        )
        self.assertIsNone(bad["market_cap"])
        self.assertIsNone(bad["fdv"])
        self.assertEqual(bad["liquidity_usd"], 10.0)

        non_finite = extract_metrics({"marketCap": float("nan"), "fdv": float("inf")})
        self.assertIsNone(non_finite["market_cap"])
        self.assertIsNone(non_finite["fdv"])

        zero = extract_metrics({"marketCap": 0, "fdv": 0})
        self.assertEqual(zero["market_cap"], 0.0)
        self.assertEqual(zero["fdv"], 0.0)


class PersistMarketCapTests(unittest.TestCase):
    def test_snapshot_and_upsert_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.upsert_token(
                "solana",
                PAIR_WITH_CAPS["baseToken"]["address"],
                {"chainId": "solana", "tokenAddress": PAIR_WITH_CAPS["baseToken"]["address"]},
                "test",
            )
            store.upsert_pair(
                "solana",
                PAIR_WITH_CAPS["pairAddress"],
                PAIR_WITH_CAPS,
                "tokens/v1",
                token_address=PAIR_WITH_CAPS["baseToken"]["address"],
            )
            wrote = maybe_snapshot_pair(
                store,
                "solana",
                PAIR_WITH_CAPS["pairAddress"],
                PAIR_WITH_CAPS,
                token_address=PAIR_WITH_CAPS["baseToken"]["address"],
            )
            self.assertTrue(wrote)
            pair_row = _pair_cols(store, PAIR_WITH_CAPS["pairAddress"])
            self.assertEqual(pair_row["market_cap"], 1_250_000.5)
            self.assertEqual(pair_row["fdv"], 5_000_000.0)
            hist = store.list_pair_history("solana", PAIR_WITH_CAPS["pairAddress"])
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["market_cap"], 1_250_000.5)
            self.assertEqual(hist[0]["fdv"], 5_000_000.0)
            self.assertEqual(hist[0]["volume_h24"], 250000.5)

            store.upsert_pair(
                "solana",
                PAIR_OMITTED["pairAddress"],
                PAIR_OMITTED,
                "tokens/v1",
                token_address=PAIR_OMITTED["baseToken"]["address"],
            )
            wrote_omit = maybe_snapshot_pair(
                store,
                "solana",
                PAIR_OMITTED["pairAddress"],
                PAIR_OMITTED,
                token_address=PAIR_OMITTED["baseToken"]["address"],
            )
            self.assertTrue(wrote_omit)
            omitted_pair = _pair_cols(store, PAIR_OMITTED["pairAddress"])
            self.assertIsNone(omitted_pair["market_cap"])
            self.assertIsNone(omitted_pair["fdv"])
            omitted_hist = store.list_pair_history("solana", PAIR_OMITTED["pairAddress"])
            self.assertIsNone(omitted_hist[0]["market_cap"])
            self.assertIsNone(omitted_hist[0]["fdv"])
            self.assertEqual(omitted_hist[0]["liquidity_usd"], 60000.0)

            # A later payload that drops the fields clears the latest pair
            # columns and does not rewrite the earlier history row.
            stripped = dict(PAIR_WITH_CAPS)
            stripped.pop("marketCap")
            stripped.pop("fdv")
            store.upsert_pair(
                "solana",
                PAIR_WITH_CAPS["pairAddress"],
                stripped,
                "tokens/v1",
                token_address=PAIR_WITH_CAPS["baseToken"]["address"],
            )
            cleared = _pair_cols(store, PAIR_WITH_CAPS["pairAddress"])
            self.assertIsNone(cleared["market_cap"])
            self.assertIsNone(cleared["fdv"])
            kept = store.list_pair_history("solana", PAIR_WITH_CAPS["pairAddress"])
            self.assertEqual(kept[0]["market_cap"], 1_250_000.5)
            self.assertEqual(kept[0]["fdv"], 5_000_000.0)

            db_path = store.db_path
            store.close()
            reader = IndicatorStore(db_path)
            loaded = reader.load_pair_history("solana", PAIR_WITH_CAPS["pairAddress"])
            by_token = reader.load_token_history(
                "solana", PAIR_WITH_CAPS["baseToken"]["address"]
            )
            reader.close()
            self.assertEqual(loaded[0]["market_cap"], 1_250_000.5)
            self.assertEqual(loaded[0]["fdv"], 5_000_000.0)
            self.assertEqual(by_token[0]["market_cap"], 1_250_000.5)

    def test_alter_does_not_backfill_raw_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE pairs (
                    chain_id TEXT NOT NULL,
                    pair_address TEXT NOT NULL,
                    token_address TEXT,
                    raw_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (chain_id, pair_address)
                );
                CREATE TABLE pair_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chain_id TEXT NOT NULL,
                    pair_address TEXT NOT NULL,
                    token_address TEXT,
                    recorded_at TEXT NOT NULL,
                    price_usd REAL,
                    volume_h24 REAL,
                    volume_h1 REAL,
                    liquidity_usd REAL,
                    txns_h24 INTEGER,
                    tier TEXT
                );
                CREATE TABLE schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('addr_lower_v1', 'test')"
            )
            raw = json.dumps({"marketCap": 999, "fdv": 1000, "priceUsd": "1"})
            conn.execute(
                """
                INSERT INTO pairs (
                    chain_id, pair_address, token_address, raw_json, source, updated_at
                ) VALUES ('solana', 'oldpair', 'oldmint', ?, 'dex', '2026-01-01T00:00:00Z')
                """,
                (raw,),
            )
            conn.execute(
                """
                INSERT INTO pair_history (
                    chain_id, pair_address, token_address, recorded_at,
                    price_usd, volume_h24, tier
                ) VALUES (
                    'solana', 'oldpair', 'oldmint', '2026-01-01T00:00:00Z', 1.0, 10, 'hot'
                )
                """
            )
            conn.commit()
            conn.close()

            store = Store(path)
            pair = store._conn.execute(
                "SELECT market_cap, fdv FROM pairs WHERE pair_address = 'oldpair'"
            ).fetchone()
            hist = store._conn.execute(
                "SELECT market_cap, fdv, volume_h24 FROM pair_history WHERE pair_address = 'oldpair'"
            ).fetchone()
            cols = {row[1] for row in store._conn.execute("PRAGMA table_info(pair_history)")}
            store.close()
            self.assertIn("market_cap", cols)
            self.assertIn("fdv", cols)
            self.assertIsNone(pair["market_cap"])
            self.assertIsNone(pair["fdv"])
            self.assertIsNone(hist["market_cap"])
            self.assertIsNone(hist["fdv"])
            self.assertEqual(hist["volume_h24"], 10.0)

    def test_reader_projects_null_before_columns_exist(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE pair_history (
                id INTEGER PRIMARY KEY,
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
        conn.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, recorded_at, price_usd, volume_h24, tier
            ) VALUES ('solana', 'p', '2026-10-14T00:00:00Z', 1.0, 8, 'hot')
            """
        )
        cov = market_cap_null_coverage(
            conn, since_iso="2020-01-01T00:00:00Z", tier="hot"
        )
        names = {row["name"] for row in conn.execute("PRAGMA table_info(pair_history)")}
        selected = conn.execute(
            f"SELECT {pair_history_column_sql(names)} FROM pair_history"
        ).fetchone()
        self.assertFalse(cov["available"])
        self.assertEqual(cov["reason"], "columns_missing")
        self.assertFalse(cov["counts_toward_strategy_validity"])
        self.assertIsNone(selected["market_cap"])
        self.assertIsNone(selected["fdv"])
        self.assertEqual(selected["volume_h24"], 8.0)
        conn.close()

    def test_indicator_loader_without_columns_returns_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE pair_history (
                    id INTEGER PRIMARY KEY,
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
            conn.execute(
                """
                INSERT INTO pair_history (
                    chain_id, pair_address, token_address, recorded_at,
                    price_usd, volume_h24, volume_h1, liquidity_usd, tier
                ) VALUES (
                    'solana', 'p', 'mint', '2026-10-14T00:00:00Z', 1.25, 8, 1, 50, 'hot'
                )
                """
            )
            conn.commit()
            conn.close()
            reader = IndicatorStore(path)
            rows = reader.load_pair_history("solana", "p")
            reader.close()
            self.assertEqual(rows[0]["price_usd"], 1.25)
            self.assertEqual(rows[0]["volume_h24"], 8.0)
            self.assertIsNone(rows[0]["market_cap"])
            self.assertIsNone(rows[0]["fdv"])


class MarketCapObserveTests(unittest.TestCase):
    def test_null_count_does_not_change_liquidity_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            for i in range(20):
                store.insert_pair_history(
                    "solana",
                    f"ok{i}",
                    price_usd=1.0,
                    volume_h24=100.0,
                    liquidity_usd=80_000,
                    market_cap=None,
                    fdv=None,
                    tier="hot",
                    recorded_at=_iso(30 + i),
                )
            report = CheckReport()
            check_hot_liquidity_coverage(store, report)
            row = report.checks[0]
            mcap = row["metrics"]["mcap_hot_1h"]
            store.close()
            self.assertEqual(row["name"], HOT_LIQ_CHECK_NAME)
            self.assertEqual(row["level"], "PASS")
            self.assertEqual(mcap["n"], 20)
            self.assertEqual(mcap["market_cap_null_n"], 20)
            self.assertEqual(mcap["fdv_null_n"], 20)
            self.assertEqual(mcap["market_cap_null_rate"], 1.0)
            self.assertFalse(mcap["counts_toward_strategy_validity"])
            self.assertIn("不计有效性", row["detail"])

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            for i in range(20):
                store.insert_pair_history(
                    "solana",
                    f"gone{i}",
                    price_usd=1.0,
                    volume_h24=100.0,
                    liquidity_usd=None,
                    market_cap=50_000,
                    fdv=80_000,
                    tier="hot",
                    recorded_at=_iso(20 + i),
                )
            report = CheckReport()
            check_hot_liquidity_coverage(store, report)
            row = report.checks[0]
            store.close()
            self.assertEqual(row["level"], "FAIL")
            self.assertEqual(row["metrics"]["mcap_hot_1h"]["market_cap_null_n"], 0)
            self.assertEqual(row["metrics"]["mcap_hot_1h"]["fdv_present_n"], 20)


if __name__ == "__main__":
    unittest.main()
