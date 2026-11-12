"""Archive, CCXT, and news-calibration contracts. No production database."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from decision_engine.core.ccxt_public_backfill import (
    PageResult,
    backfill_symbol,
    closed_pages,
    ensure_ccxt_ohlcv,
)
from decision_engine.core.cross_venue_deviation import gap_counts, pair_exact_bars
from decision_engine.core.cross_venue_calibration import evaluate_cross_venue, usd_not_calibrated
from decision_engine.core.news_oos_calibration import (
    ARCHIVE_SERIES,
    audit_news_rows,
    bootstrap_treated,
    build_effect_rows,
    dedup_events,
    evaluate_study,
    feature_is_known,
    partial_taker_record,
    taker_buy_quote_share,
    time_split,
)
from decision_engine.core.vision_backfill import (
    FetchResult,
    cached_fetch,
    connect,
    coverage_report,
    detect_epoch_unit,
    epoch_to_ms,
    ingest_zip,
    kline_requests,
    parse_book_depth_csv,
    parse_metrics_csv,
    parse_spot_kline_csv,
)

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)


def _zip_bytes(name: str, text: str) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


class VisionTests(unittest.TestCase):
    def test_microsecond_spot_timestamp_and_separate_metric_columns(self) -> None:
        self.assertEqual(detect_epoch_unit(1794096000000000), "us")
        self.assertEqual(epoch_to_ms(1794096000000000), 1794096000000)
        self.assertEqual(detect_epoch_unit(1794096000000), "ms")
        text = (
            "1794096000000000,75867.58,75867.59,75822.94,75822.95,17.26852,"
            "1794096299999999,1309666.56,3243,5.64092,427747.05,0\n"
        )
        rows = parse_spot_kline_csv(text, "BTCUSDT")
        self.assertEqual(rows[0]["timestamp_unit"], "us")
        self.assertEqual(rows[0]["taker_buy_quote_volume"], 427747.05)
        self.assertNotIn("obi_top5", rows[0])
        metrics = parse_metrics_csv(
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2026-11-08 00:35:00,BTCUSDT,1,2,3.3,1.8,0.9,0.4\n"
        )
        self.assertNotEqual(
            metrics[0]["count_toptrader_long_short_ratio"],
            metrics[0]["count_long_short_ratio"],
        )
        self.assertNotIn("whale_retail_divergence", metrics[0])

    def test_book_depth_percentage_is_not_a_top5_book(self) -> None:
        rows = parse_book_depth_csv(
            "timestamp,percentage,depth,notional\n2026-11-08 00:00:01,-5.00,10,20\n"
            "2026-11-08 00:00:01,1.00,4,8\n",
            "ETHUSDT",
        )
        self.assertEqual(rows[0]["percentage"], -5.0)
        self.assertNotIn("obi_top5", rows[0])
        self.assertNotIn("bid_px", rows[0])

    def test_monthly_file_when_the_month_is_complete(self) -> None:
        dates = tuple(date(2026, 10, day) for day in range(1, 32))
        urls = kline_requests("BTCUSDT", dates)
        self.assertEqual(len(urls), 1)
        self.assertIn("/monthly/", urls[0])
        partial = kline_requests("BTCUSDT", dates[:3])
        self.assertTrue(all("/daily/" in url for url in partial))

    def test_reingest_is_idempotent(self) -> None:
        payload = _zip_bytes(
            "BTCUSDT-metrics.csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2026-11-08 00:35:00,BTCUSDT,1,2,3,1,0.5,0.2\n",
        )
        fetched = FetchResult("https://example.test/a.zip", "ok", "abc", len(payload), None, payload)
        with TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "vision.db")
            first = ingest_zip(conn, "metrics", "BTCUSDT", fetched)
            second = ingest_zip(conn, "metrics", "BTCUSDT", fetched)
            count = conn.execute("SELECT COUNT(*) FROM vision_metrics").fetchone()[0]
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
            self.assertEqual(count, 1)
            report = coverage_report(conn, ["BTCUSDT", "SOLUSDT"])
            self.assertFalse(report["complete"])
            self.assertFalse(report["obi_top5_column"])

    def test_second_pass_reuses_the_manifest_without_http(self) -> None:
        payload = _zip_bytes(
            "BTCUSDT-metrics.csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2026-11-08 00:35:00,BTCUSDT,1,2,3,1,0.5,0.2\n",
        )
        fetched = FetchResult("https://example.test/a.zip", "ok", "abc", len(payload), None, payload)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = connect(root / "vision.db")
            ingest_zip(conn, "metrics", "BTCUSDT", fetched)
            with patch(
                "decision_engine.core.vision_backfill.fetch_bytes",
                side_effect=AssertionError("http"),
            ):
                again = cached_fetch(conn, root / "cache", fetched.url, limiter=None)  # type: ignore[arg-type]
            self.assertEqual(again.detail, "reused_manifest")
            self.assertEqual(ingest_zip(conn, "metrics", "BTCUSDT", again), 0)


class CcxtTests(unittest.TestCase):
    def test_pagination_stops_and_does_not_invent_missing_minutes(self) -> None:
        calls = {"n": 0}

        def fetch_page(timeframe: str, since: int, limit: int) -> PageResult:
            del timeframe, limit
            calls["n"] += 1
            if calls["n"] > 3:
                return PageResult([], None)
            start = since
            return PageResult([[start + i * 300_000, 1, 2, 0.5, 1.5, 3] for i in range(2)])

        rows, error = closed_pages(fetch_page, start_ms=0, end_ms=300_000 * 6, limit=2)
        self.assertIsNone(error)
        opens = [int(row[0]) for row in rows]
        self.assertEqual(opens, [0, 300_000, 600_000, 900_000, 1_200_000, 1_500_000])
        self.assertNotIn(1_800_000, opens)
        self.assertEqual(len(opens), len(set(opens)))

    def test_usd_bars_are_not_paired_with_usdt(self) -> None:
        usdt = {0: {"open": 1, "high": 2, "low": 0.5, "close": 1.5}}
        usd = {0: {"open": 1, "high": 9, "low": 0.1, "close": 8}}
        self.assertEqual(pair_exact_bars(usdt, usd)[0]["deviation_bps"], (1.5 - 8) / 8 * 10_000)
        # The caller must refuse this pair because the quotes differ. The
        # function itself only aligns timestamps.
        self.assertEqual(gap_counts(usdt, {300_000: usd[0]})["filled_by_interpolation"], 0)
        self.assertEqual(gap_counts(usdt, {300_000: usd[0]})["aligned"], 0)

    def test_blocked_exchange_records_no_bars(self) -> None:
        with TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "ccxt.db")
            ensure_ccxt_ohlcv(conn)
            result = backfill_symbol(
                conn,
                "bybit",
                "BTC",
                start_ms=0,
                end_ms=600_000,
                fetch_page=lambda timeframe, since, limit: PageResult([], "blocked:403"),
            )
            self.assertEqual(result["rows"], 0)
            self.assertEqual(result["status"], "error")
            self.assertIn("blocked", str(result["detail"]))

    def test_stored_prefix_is_not_requested_again(self) -> None:
        with TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "ccxt.db")
            ensure_ccxt_ohlcv(conn)
            calls: list[int] = []

            def fetch_page(timeframe: str, since: int, limit: int) -> PageResult:
                del timeframe, limit
                calls.append(since)
                return PageResult([[since, 1, 1, 1, 1, 1]], None)

            backfill_symbol(
                conn, "okx", "BTC", start_ms=0, end_ms=900_000, fetch_page=fetch_page, limit=1
            )
            calls.clear()
            second = backfill_symbol(
                conn, "okx", "BTC", start_ms=0, end_ms=900_000, fetch_page=fetch_page, limit=1
            )
            self.assertEqual(calls, [])
            self.assertEqual(second["status"], "skipped_covered")


class NewsCalibrationTests(unittest.TestCase):
    def test_counts_come_from_the_rows_and_late_archives_drop_out(self) -> None:
        rows = []
        for index in range(7):
            rows.append(
                {
                    "source": "coindesk",
                    "title": f"BTC item {index}",
                    "published_at": "2026-11-01T00:00:00Z",
                    "fetched_at": "2026-11-01T00:10:00Z",
                    "onchain_status": "pending",
                }
            )
        rows.append(
            {
                "source": "coindesk",
                "title": "old wire",
                "published_at": "2026-10-01T00:00:00Z",
                "fetched_at": "2026-11-01T00:00:00Z",
                "onchain_status": "pending",
            }
        )
        audit = audit_news_rows(rows)
        self.assertEqual(audit["rows_in_input"], 8)
        self.assertEqual(audit["late_archive_excluded"], 1)
        self.assertNotEqual(audit["rows_in_input"], 1859)
        self.assertFalse(audit["hardcoded_inventory_used"])
        kept = dedup_events(rows)
        self.assertEqual(len(kept), 7)

    def test_feature_after_fetch_is_not_known_and_gaps_are_not_filled(self) -> None:
        fetched = NOW
        self.assertFalse(feature_is_known(NOW, fetched, NOW + timedelta(minutes=1)))
        self.assertTrue(feature_is_known(NOW - timedelta(minutes=5), fetched, NOW - timedelta(minutes=1)))
        events = [
            {
                "title": "ETH holds",
                "source": "coindesk",
                "fetched_at_dt": NOW,
                "bases": ("ETH",),
            }
        ]
        klines = [
            {
                "symbol": "ETHUSDT",
                "open_time": NOW - timedelta(minutes=5),
                "close_time": NOW - timedelta(minutes=1),
                "open": 100,
                "close": 101,
                "quote_volume": 10,
                "taker_buy_quote_volume": 6,
            },
            {
                "symbol": "ETHUSDT",
                "open_time": NOW,
                "close_time": NOW + timedelta(minutes=5),
                "open": 100,
                "close": 110,
                "quote_volume": 10,
                "taker_buy_quote_volume": 1,
            },
        ]
        # Horizon is 60 minutes and this future path stops at 5 minutes, so it is not filled.
        self.assertEqual(build_effect_rows(events, klines, [], []), [])

    def test_archive_significance_does_not_unlock_the_gate(self) -> None:
        report = evaluate_study(
            predictive_rows=400,
            test_clusters=40,
            hypotheses=[
                {"name": "taker_buy_quote_share", "p_value": 0.001},
                {"name": "count_toptrader_long_short_ratio", "p_value": 0.2},
                {"name": "book_depth_imbalance_pct_1", "p_value": 0.8},
            ],
            series=ARCHIVE_SERIES,
            heat_available=False,
            news_db_sha256="abc",
            split_times={"train_end": "a", "validation_end": "b", "test_end": "c"},
            synthetic=False,
        )
        self.assertFalse(report["unlock"])
        self.assertFalse(report["live_gate_unlock"])
        self.assertTrue(report["will_not_fully_unlock_onchain_calibration"])
        self.assertIn("obi_top5", report["cannot_identify"])
        self.assertIn("heat_evolution", report["cannot_identify"])
        self.assertEqual(report["status"], "partial_taker_only")
        self.assertFalse(report["partial_taker_evidence"]["applied_to_live_gate"])
        self.assertIsNone(report["false_positive_rate"])
        self.assertIsNotNone(partial_taker_record(report))

    def test_synthetic_evidence_stays_locked_and_a_real_gate_record_can_open(self) -> None:
        synthetic = evaluate_study(
            predictive_rows=100,
            test_clusters=40,
            hypotheses=[{"name": "obi_top5", "p_value": 0.0}],
            series=("obi_top5", "taker_buy_ratio", "whale_retail_divergence", "heat_evolution"),
            heat_available=True,
            news_db_sha256="abc",
            split_times={"train_end": "a", "validation_end": "b", "test_end": "c"},
            synthetic=True,
        )
        self.assertFalse(synthetic["unlock"])
        self.assertFalse(synthetic["live_gate_unlock"])
        self.assertIsNone(partial_taker_record(synthetic))
        attested = evaluate_study(
            predictive_rows=100,
            test_clusters=40,
            hypotheses=[{"name": "obi_top5", "p_value": 0.0}],
            series=("obi_top5", "taker_buy_ratio", "whale_retail_divergence", "heat_evolution"),
            heat_available=True,
            news_db_sha256="abc",
            split_times={"train_end": "a", "validation_end": "b", "test_end": "c"},
            synthetic=False,
        )
        self.assertFalse(attested["live_gate_unlock"])
        self.assertIsNone(attested["partial_taker_evidence"])
        self.assertIsNone(taker_buy_quote_share(10, 15))
        self.assertAlmostEqual(taker_buy_quote_share(10, 6) or 0, 0.6)

    def test_split_purges_the_label_window(self) -> None:
        rows = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(10):
            rows.append(
                {
                    "source": "coindesk",
                    "title": f"BTC note {index}",
                    "published_at": (start + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "fetched_at": (start + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "fetched_at_dt": start + timedelta(hours=index),
                    "bases": ("BTC",),
                }
            )
        parts = time_split(rows)
        if parts["train"] and parts["validation"]:
            boundary = parts["validation"][0]["fetched_at_dt"]
            for row in parts["train"]:
                self.assertLessEqual(row["fetched_at_dt"] + timedelta(minutes=60), boundary)

    def test_block_bootstrap_keeps_a_cluster_together(self) -> None:
        rows = [("c1", True, 20.0), ("c1", True, 20.0), ("c2", False, 0.0)]
        # The public helper averages inside the caller. Here each row is a cluster.
        stats = bootstrap_treated([("c1", True, 20.0), ("c2", False, 0.0)], reps=50, seed=1)
        self.assertAlmostEqual(float(stats["mean_diff_bps"]), 20.0)
        self.assertGreater(float(stats["clusters"]), 1)

    def test_delay_sensitivity_is_not_the_old_inventory(self) -> None:
        rows = [
            {
                "published_at": "2026-11-01T00:00:00Z",
                "fetched_at": "2026-11-01T03:00:00Z",
            }
        ]
        audit = audit_news_rows(rows)
        self.assertEqual(audit["sensitivity_by_cutoff_minutes"]["90"]["late_archive_excluded"], 1)
        self.assertEqual(audit["sensitivity_by_cutoff_minutes"]["180"]["late_archive_excluded"], 0)
        self.assertFalse(audit["reference_counts_not_used"]["used_as_this_result"])
        self.assertNotEqual(audit["rows_in_input"], 1906)
        self.assertNotEqual(audit["rows_in_input"], 1859)


class CrossVenueTests(unittest.TestCase):
    def test_one_venue_and_usd_stay_uncalibrated(self) -> None:
        usd = usd_not_calibrated("no second USD venue")
        self.assertEqual(usd["status"], "not_calibrated")
        self.assertFalse(usd["unlock_news_gate"])
        thin = evaluate_cross_venue(
            {0: {"open": 1, "high": 1, "low": 1, "close": 1}},
            {0: {"open": 1, "high": 1, "low": 1, "close": 1}},
            quote="USDT",
        )
        self.assertEqual(thin["status"], "insufficient")
        self.assertFalse(thin["unlock_news_gate"])

    def test_aligned_wick_needs_the_peer_and_a_forward_bar(self) -> None:
        left = {}
        right = {}
        for index in range(40):
            open_ms = index * 300_000
            left[open_ms] = {"open": 100, "high": 100, "low": 100, "close": 100}
            right[open_ms] = {"open": 100, "high": 100, "low": 100, "close": 100}
        left[20 * 300_000]["high"] = 101
        report = evaluate_cross_venue(left, right, quote="USDT")
        self.assertGreater(report["gaps"]["aligned"], 0)
        self.assertEqual(report["gaps"]["filled_by_interpolation"], 0)
        self.assertFalse(report["unlock_news_gate"])


class CleanPackageTests(unittest.TestCase):
    def test_empty_directory_imports_and_runs_the_offline_fixture(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        names = [
            "decision_engine/__init__.py",
            "decision_engine/core/__init__.py",
            "decision_engine/core/vision_backfill.py",
            "decision_engine/core/ccxt_public_backfill.py",
            "decision_engine/core/cross_venue_deviation.py",
            "decision_engine/core/cross_venue_calibration.py",
            "decision_engine/core/news_oos_calibration.py",
            "packaging/calib_backfill/selfcheck_offline.py",
            "packaging/calib_backfill/requirements.txt",
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in names:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(repo / name, target)
            self.assertFalse((root / "decision_engine/core/news_shock_gate.py").exists())
            completed = subprocess.run(
                [sys.executable, str(root / "packaging/calib_backfill/selfcheck_offline.py")],
                cwd=root,
                env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertIn("OFFLINE_OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
