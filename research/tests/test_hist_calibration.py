"""Synthetic checks for archive staging and the calibration gate.

No network and no production database.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from decision_engine.core.news_shock_audit import MIN_FEATURE_COVERAGE, MIN_TEST_EVENTS
from decision_engine.core.news_shock_gate import LATE_ARCHIVE_LAG_MIN
from research.hist_calibration.canon import venue_markets
from research.hist_calibration.ccxt_public import (
    candle_is_closed,
    coverage,
    ensure_stage as ccxt_stage,
    rollback_upsert,
    stage_candles,
    upsert_into_ccxt_db,
)
from research.hist_calibration.study import (
    FAMILY_SIZE,
    LATE_ARCHIVE_LAG_MIN as STUDY_LAG,
    MIN_FEATURE_COVERAGE as STUDY_COVERAGE,
    MIN_TEST_EVENTS as STUDY_MIN_TEST,
    asof_value,
    book_depth_is_not_obi,
    cross_venue_dislocation,
    decide,
    forward_return,
    inventory_news,
    permutation_effect,
    time_split,
    train_only_threshold,
)
from research.hist_calibration.vision import (
    closed_only,
    ensure_stage,
    parse_book_depth_csv,
    parse_checksum_text,
    parse_metrics_csv,
    parse_spot_kline_csv,
    sha256_bytes,
    stage_metrics,
)


def _zip_bytes(csv_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("rows.csv", csv_text)
    return buffer.getvalue()


class VisionSchemaTests(unittest.TestCase):
    def test_book_depth_is_not_top5_and_metrics_stay_split(self) -> None:
        depth = parse_book_depth_csv(
            [
                ["timestamp", "percentage", "depth", "notional"],
                ["1700000000000", "-5", "10", "100"],
                ["1700000000000", "5", "12", "80"],
            ]
        )
        self.assertIsNone(depth["obi_top5"])
        self.assertTrue(depth["not_obi_top5"])
        self.assertEqual(book_depth_is_not_obi(depth["rows"])["usable_as_obi_top5"], False)
        metrics = parse_metrics_csv(
            [
                ["create_time", "symbol", "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio"],
                ["1700000000000", "BTCUSDT", "1.2", "0.8"],
            ]
        )
        self.assertEqual(metrics["rows"][0]["account_long_short"], 1.2)
        self.assertEqual(metrics["rows"][0]["position_long_short"], 0.8)
        self.assertIsNone(metrics["obi_top5"])

    def test_daily_overlap_keeps_daily_and_records_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = ensure_stage(Path(tmp) / "stage.db")
            stage_metrics(
                conn,
                "BTCUSDT",
                [{"create_time_ms": 5, "account_long_short": 1.0, "position_long_short": 1.0}],
                "monthly.zip",
                prefer="monthly",
            )
            report = stage_metrics(
                conn,
                "BTCUSDT",
                [{"create_time_ms": 5, "account_long_short": 1.5, "position_long_short": 0.7}],
                "daily.zip",
                prefer="daily",
            )
            kept = conn.execute("SELECT account_long_short, source_url FROM um_metrics").fetchone()
            conflicts = conn.execute("SELECT COUNT(*) FROM stage_conflicts").fetchone()[0]
            conn.close()
            self.assertEqual(report["conflicts"], 1)
            self.assertEqual(kept[0], 1.5)
            self.assertEqual(kept[1], "daily.zip")
            self.assertEqual(conflicts, 1)

    def test_kline_date_gap_duplicate_and_open_bar(self) -> None:
        day = date(2026, 11, 1)
        open_ms = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)
        other = int(datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp() * 1000)
        header = ["open_time", "o", "h", "l", "c", "v", "close_time", "q", "n", "tb", "tq", "i"]
        row = lambda ts: [str(ts), "1", "2", "0.5", "1.5", "3", str(ts + 299_999), "4", "1", "1", "2", "0"]
        parsed = parse_spot_kline_csv(
            [header, row(open_ms), row(open_ms), row(other)],
            file_day=day,
        )
        self.assertEqual(parsed["duplicate_opens"], [open_ms])
        self.assertEqual(parsed["date_mismatches"], [other])
        self.assertTrue(parsed["gaps"])
        now_ms = open_ms + 1000
        self.assertEqual(closed_only(parsed["rows"], now_ms=now_ms), [])
        self.assertEqual(len(closed_only(parsed["rows"], now_ms=other + 300_000)), 2)

    def test_checksum_parser(self) -> None:
        digest = "a" * 64
        self.assertEqual(parse_checksum_text(f"{digest}  file.zip\n"), digest)
        self.assertEqual(len(sha256_bytes(_zip_bytes("a,b\n"))), 64)


class CausalityTests(unittest.TestCase):
    def test_gates_match_the_existing_audit_constants(self) -> None:
        self.assertEqual(STUDY_MIN_TEST, MIN_TEST_EVENTS)
        self.assertEqual(STUDY_COVERAGE, MIN_FEATURE_COVERAGE)
        self.assertEqual(STUDY_LAG, LATE_ARCHIVE_LAG_MIN)
        self.assertEqual(FAMILY_SIZE, 1)

    def test_late_archive_is_excluded_and_counts_are_not_hardcoded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "news.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE news_events (
                    source_tier TEXT, published_at TEXT, fetched_at TEXT,
                    title TEXT, url TEXT, onchain_status TEXT
                )
                """
            )
            fetched = datetime(2026, 11, 10, tzinfo=timezone.utc)
            late_published = fetched - timedelta(minutes=LATE_ARCHIVE_LAG_MIN + 5)
            fresh_published = fetched - timedelta(minutes=10)
            conn.executemany(
                "INSERT INTO news_events VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("whitelist", fresh_published.strftime("%Y-%m-%dT%H:%M:%SZ"), fetched.strftime("%Y-%m-%dT%H:%M:%SZ"), "fresh", "https://a", "pending"),
                    ("whitelist", late_published.strftime("%Y-%m-%dT%H:%M:%SZ"), fetched.strftime("%Y-%m-%dT%H:%M:%SZ"), "old", "https://b", "pending"),
                    ("rumor", fresh_published.strftime("%Y-%m-%dT%H:%M:%SZ"), fetched.strftime("%Y-%m-%dT%H:%M:%SZ"), "rumor", "https://c", "pending"),
                ],
            )
            conn.commit()
            conn.close()
            info = inventory_news(db)
            self.assertEqual(info["rows"], 3)
            self.assertEqual(info["late_archive_rows"], 1)
            self.assertEqual(info["tradable_rows"], 1)
            self.assertNotEqual(info["rows"], 1859)
            self.assertNotEqual(info["late_archive_rows"], 164)

    def test_features_cannot_lead_the_event_and_holdout_is_not_the_cut(self) -> None:
        event_ms = 1_000_000
        self.assertIsNone(asof_value([(event_ms + 1, 0.2), (event_ms - 10_000_000, 0.9)], event_ms, 90 * 60_000))
        self.assertEqual(asof_value([(event_ms - 1000, -0.1)], event_ms, 90 * 60_000), -0.1)
        bars = [(event_ms - 300_000, 10.0), (event_ms + 10, 11.0), (event_ms + 300_010, 12.0)]
        self.assertAlmostEqual(forward_return(bars, event_ms, 300_000), (12.0 - 11.0) / 11.0)
        events = [{"event_ms": index, "forward_return": float(index)} for index in range(10)]
        splits = time_split(events)
        cut = train_only_threshold([item["forward_return"] for item in splits["train"]])
        self.assertNotEqual(cut, train_only_threshold([item["forward_return"] for item in splits["test"]]))
        self.assertEqual(
            permutation_effect([1.0, 2.0, 3.0], [1, 0, 1], seed=1),
            permutation_effect([1.0, 2.0, 3.0], [1, 0, 1], seed=1),
        )
        self.assertNotEqual(
            permutation_effect([1.0, 2.0, 3.0], [1, 0, 1], seed=1),
            "searched",
        )

    def test_insufficient_overlap_does_not_unlock(self) -> None:
        events = [
            {"event_ms": index, "forward_return": 0.01, "obi_top5_asof": None, "taker_asof": None, "whale_asof": None, "heat_delta": None, "symbol": "BTCUSDT", "title": "a"}
            for index in range(40)
        ]
        body = decide(
            {
                "events": events,
                "news_availability": "ok",
                "late_archive_rows": 2,
                "raw_rows": 42,
                "obi_from_book_depth": True,
                "x_watch_used_as_history": False,
            }
        )
        self.assertFalse(body["gates_passed"])
        self.assertIsNone(body["onchain_calibration"])
        self.assertIn("cannot_unlock", body)
        self.assertFalse(body["holdout_used_for_threshold"])

    def test_coinbase_usd_is_not_pooled_with_usdt(self) -> None:
        markets = venue_markets("BTCUSDT")
        self.assertEqual(markets["coinbase"]["quote"], "USD")
        self.assertFalse(markets["coinbase"]["comparable_to_usdt"])
        left = [(0, 100.0, 110.0, 90.0)]
        right = [(0, 101.0, 111.0, 95.0)]
        report = cross_venue_dislocation(left, right, left_quote="USDT", right_quote="USD")
        self.assertFalse(report["quotes_pooled"])
        self.assertFalse(report["comparable_without_basis"])
        self.assertEqual(report["aligned_bars"], 1)
        missing = cross_venue_dislocation(left, [], left_quote="USDT", right_quote="USDT")
        self.assertEqual(missing["aligned_bars"], 0)
        self.assertEqual(missing["missing_right"], 1)


class CcxtStageTests(unittest.TestCase):
    def test_open_bar_is_skipped_and_upsert_can_roll_back_inserts(self) -> None:
        now_ms = 10_000_000
        self.assertFalse(candle_is_closed(now_ms - 1000, now_ms))
        with tempfile.TemporaryDirectory() as tmp:
            stage_path = Path(tmp) / "stage.db"
            target = Path(tmp) / "ccxt.db"
            conn = ccxt_stage(stage_path)
            report = stage_candles(
                conn,
                exchange="coinbase",
                symbol="BTC/USD",
                quote="USD",
                comparable_to_usdt=False,
                candles=[[now_ms - 300_000, 1, 2, 0.5, 1.5, 3], [now_ms - 1000, 1, 2, 0.5, 1.5, 3]],
                now_ms=now_ms,
            )
            self.assertEqual(report["written"], 1)
            self.assertEqual(report["skipped_open"], 1)
            cov = coverage(conn, exchange="coinbase", symbol="BTC/USD", now_ms=now_ms)
            self.assertFalse(cov["complete"])
            conn.close()
            upsert_into_ccxt_db(stage_path, target)
            conn = sqlite3.connect(target)
            count = conn.execute("SELECT COUNT(*) FROM ccxt_ohlcv").fetchone()[0]
            conn.close()
            self.assertEqual(count, 1)
            manifest = target.with_suffix(target.suffix + ".upsert-manifest.json")
            # The rollback reads the manifest table, not only the json summary.
            removed = rollback_upsert(target, manifest)
            self.assertEqual(removed["removed_inserts"], 1)


if __name__ == "__main__":
    unittest.main()
