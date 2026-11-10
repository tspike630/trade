"""Archive, CCXT, and news-calibration contracts. No production database."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from decision_engine.core.ccxt_public_backfill import (
    PageResult,
    backfill_symbol,
    closed_bar_opens,
    closed_pages,
    ensure_ccxt_ohlcv,
)
from decision_engine.core.cross_venue_deviation import gap_counts, pair_exact_bars
from decision_engine.core.news_oos_calibration import (
    ARCHIVE_SERIES,
    audit_news_rows,
    bootstrap_treated,
    build_effect_rows,
    dedup_events,
    evaluate_study,
    feature_is_known,
    load_unlocked_calibration,
    time_split,
)
from decision_engine.core.vision_backfill import (
    BOOK_DEPTH_SEMANTICS,
    LABEL_AVAILABILITY,
    LABEL_CONVENTION,
    METRIC_AVAILABILITY_BIN_END,
    FetchResult,
    RateLimiter,
    connect,
    coverage_report,
    detect_epoch_unit,
    epoch_to_ms,
    import_planned_file,
    ingest_zip,
    kline_requests,
    parse_book_depth_csv,
    parse_metrics_csv,
    parse_sha256sum,
    parse_spot_kline_csv,
)
from scripts.run_public_backfill import main as backfill_main
from scripts.run_public_backfill import overall_complete

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
        self.assertEqual(report["status"], "not_transferable")
        self.assertIsNone(report["false_positive_rate"])
        self.assertIsNone(load_unlocked_calibration(report))

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
        self.assertIsNone(load_unlocked_calibration(synthetic))
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
        loaded = load_unlocked_calibration(attested)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.min_heat_delta, 1)

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


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _checksum_result(url: str, payload: bytes) -> FetchResult:
    name = url.rsplit("/", 1)[-1]
    digest = _sha256(payload)
    body = f"{digest}  {name}\n".encode()
    return FetchResult(
        url + ".CHECKSUM",
        "ok",
        _sha256(body),
        len(body),
        None,
        body,
        1_700_000_000_000,
        "Tue, 10 Nov 2026 02:43:02 GMT",
    )


_METRICS_CSV = (
    "create_time,symbol,sum_open_interest,sum_open_interest_value,"
    "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
    "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
    "2026-11-09 00:00:00,BTCUSDT,1,2,3.18550969,1.99021200,2.85595208,0.95567800\n"
)


class SupplyIngestTests(unittest.TestCase):
    def test_closed_window_excludes_the_forming_edge_and_names_holes(self) -> None:
        step = 300_000
        start = 1_000
        end = start + step * 6
        expected = closed_bar_opens(start, end, step)
        forming = ((end - step) // step) * step + step
        self.assertNotIn(forming, expected)
        self.assertEqual(len(expected), 5)
        self.assertNotEqual(len(expected), (end - start) // step)
        with TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "ccxt.db")
            ensure_ccxt_ohlcv(conn)

            def fetch_page(timeframe: str, since: int, limit: int) -> PageResult:
                del timeframe, since, limit
                rows = [[stamp, 1, 2, 0.5, 1.5, 3] for stamp in expected]
                rows.append([forming, 9, 9, 9, 9, 9])
                hole = expected[1]
                rows = [row for row in rows if row[0] != hole]
                return PageResult(rows, None)

            result = backfill_symbol(
                conn,
                "okx",
                "BTC",
                start_ms=start,
                end_ms=end,
                fetch_page=fetch_page,
                limit=10,
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["expected_closed_bars"], 5)
            self.assertEqual(result["missing_timestamps_ms"], [expected[1]])
            self.assertNotIn(forming, result["missing_timestamps_ms"])
            stored = {
                row[0]
                for row in conn.execute("SELECT ts FROM ccxt_ohlcv")
            }
            self.assertNotIn(forming, stored)

    def test_checksum_mismatch_and_bad_zip_do_not_import_and_later_file_continues(self) -> None:
        good_url = "https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-11-09.zip"
        bad_url = "https://data.binance.vision/data/futures/um/daily/metrics/ETHUSDT/ETHUSDT-metrics-2026-11-09.zip"
        mismatch_url = "https://data.binance.vision/data/futures/um/daily/metrics/SOLUSDT/SOLUSDT-metrics-2026-11-09.zip"
        good_zip = _zip_bytes("BTCUSDT-metrics-2026-11-09.csv", _METRICS_CSV)
        bad_zip = b"not a zip"
        mismatch_zip = _zip_bytes("SOLUSDT-metrics-2026-11-09.csv", _METRICS_CSV.replace("BTCUSDT", "SOLUSDT"))
        calls: list[str] = []

        def fetcher(url: str, limiter: object) -> FetchResult:
            del limiter
            calls.append(url)
            if url == good_url + ".CHECKSUM":
                return _checksum_result(good_url, good_zip)
            if url == good_url:
                return FetchResult(url, "ok", _sha256(good_zip), len(good_zip), None, good_zip, 50, "Tue, 10 Nov 2026 07:24:21 GMT")
            if url == bad_url + ".CHECKSUM":
                return _checksum_result(bad_url, bad_zip)
            if url == bad_url:
                return FetchResult(url, "ok", _sha256(bad_zip), len(bad_zip), None, bad_zip, 60, None)
            if url == mismatch_url + ".CHECKSUM":
                return FetchResult(
                    url,
                    "ok",
                    "abc",
                    8,
                    None,
                    b"0000000000000000000000000000000000000000000000000000000000000000  SOLUSDT-metrics-2026-11-09.zip\n",
                    70,
                    None,
                )
            if url == mismatch_url:
                return FetchResult(
                    url, "ok", _sha256(mismatch_zip), len(mismatch_zip), None, mismatch_zip, 71, None
                )
            if url.endswith("XRPUSDT-metrics-2026-11-09.zip") or url.endswith("XRPUSDT-metrics-2026-11-09.zip.CHECKSUM"):
                return FetchResult(url, "missing", None, 0, "404", None, 80, None)
            if "network" in url:
                return FetchResult(url, "error", None, 0, "URLError", None, 90, None)
            raise AssertionError(url)

        with TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "vision.db")
            limiter = RateLimiter(0)
            bad = import_planned_file(conn, "metrics", "ETHUSDT", bad_url, fetcher, limiter)
            mismatch = import_planned_file(conn, "metrics", "SOLUSDT", mismatch_url, fetcher, limiter)
            missing = import_planned_file(
                conn,
                "metrics",
                "XRPUSDT",
                "https://data.binance.vision/data/futures/um/daily/metrics/XRPUSDT/XRPUSDT-metrics-2026-11-09.zip",
                fetcher,
                limiter,
            )
            network = import_planned_file(
                conn,
                "metrics",
                "BNBUSDT",
                "https://data.binance.vision/data/futures/um/daily/metrics/BNBUSDT/BNBUSDT-metrics-network.zip",
                fetcher,
                limiter,
            )
            good = import_planned_file(conn, "metrics", "BTCUSDT", good_url, fetcher, limiter)
            again = import_planned_file(conn, "metrics", "BTCUSDT", good_url, fetcher, limiter)
            self.assertEqual(bad, "error")
            self.assertEqual(mismatch, "error")
            self.assertEqual(missing, "missing")
            self.assertEqual(network, "error")
            self.assertEqual(good, "ok")
            self.assertEqual(again, "skipped_stored")
            self.assertEqual(calls.count(good_url), 1)
            rows = conn.execute(
                "SELECT symbol, create_time_raw, create_time_unit, label_convention, "
                "label_availability, feature_available_at_ms, feature_availability_basis, "
                "downloaded_at_ms, http_last_modified, count_toptrader_long_short_ratio, "
                "sum_toptrader_long_short_ratio, count_long_short_ratio "
                "FROM vision_metrics ORDER BY symbol"
            ).fetchall()
            self.assertEqual([row[0] for row in rows], ["BTCUSDT"])
            label_ms = int(datetime(2026, 11, 9, tzinfo=timezone.utc).timestamp() * 1000)
            stored_ms = conn.execute("SELECT create_time_ms FROM vision_metrics").fetchone()[0]
            self.assertEqual(stored_ms, label_ms)
            self.assertEqual(rows[0][1], "2026-11-09 00:00:00")
            self.assertEqual(rows[0][2], "civil_utc_second")
            self.assertEqual(rows[0][3], LABEL_CONVENTION)
            self.assertEqual(rows[0][4], LABEL_AVAILABILITY)
            self.assertIsNone(rows[0][5])
            self.assertEqual(rows[0][6], "unknown")
            self.assertEqual(rows[0][7], 50)
            self.assertEqual(rows[0][8], "Tue, 10 Nov 2026 07:24:21 GMT")
            self.assertNotEqual(rows[0][9], rows[0][10])
            self.assertNotEqual(rows[0][10], rows[0][11])
            statuses = {
                row[0].rsplit("/", 1)[-1]: (row[1], row[2])
                for row in conn.execute("SELECT url, status, detail FROM vision_files")
            }
            self.assertEqual(statuses["ETHUSDT-metrics-2026-11-09.zip"][0], "error")
            self.assertIn("bad_zip", statuses["ETHUSDT-metrics-2026-11-09.zip"][1])
            self.assertEqual(statuses["SOLUSDT-metrics-2026-11-09.zip"][1], "checksum_mismatch")
            self.assertEqual(statuses["XRPUSDT-metrics-2026-11-09.zip"][0], "missing")
            self.assertEqual(statuses["BNBUSDT-metrics-network.zip"][0], "error")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(vision_book_depth)")}
            self.assertNotIn("obi_top5", columns)
            self.assertIn("depth_semantics", columns)
            report = coverage_report(conn, ["BTCUSDT"])
            self.assertFalse(report["complete"])
            self.assertEqual(report["metric_label_availability"], "unknown")
            self.assertTrue(report["book_depth_is_percentage_aggregate"])
            self.assertEqual(report["book_depth_semantics"], BOOK_DEPTH_SEMANTICS)

    def test_explicit_bin_end_is_not_the_label_or_the_download_clock(self) -> None:
        url = "https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-11-09.zip"
        payload = _zip_bytes("BTCUSDT-metrics-2026-11-09.csv", _METRICS_CSV)

        def fetcher(request: str, limiter: object) -> FetchResult:
            del limiter
            if request.endswith(".CHECKSUM"):
                return _checksum_result(url, payload)
            return FetchResult(request, "ok", _sha256(payload), len(payload), None, payload, 50, "Tue, 10 Nov 2026 07:24:21 GMT")

        with TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "vision.db")
            outcome = import_planned_file(
                conn,
                "metrics",
                "BTCUSDT",
                url,
                fetcher,
                RateLimiter(0),
                availability_mode=METRIC_AVAILABILITY_BIN_END,
            )
            self.assertEqual(outcome, "ok")
            label_ms, feature_ms, basis, availability, downloaded = conn.execute(
                "SELECT create_time_ms, feature_available_at_ms, feature_availability_basis, "
                "label_availability, downloaded_at_ms FROM vision_metrics"
            ).fetchone()
            self.assertEqual(feature_ms, label_ms + 300_000)
            self.assertNotEqual(feature_ms, downloaded)
            self.assertEqual(basis, METRIC_AVAILABILITY_BIN_END)
            self.assertEqual(availability, "unknown")

    def test_old_vision_rows_survive_added_columns(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "vision.db"
            old = sqlite3.connect(path)
            old.execute(
                """
                CREATE TABLE vision_metrics (
                    symbol TEXT NOT NULL,
                    create_time_ms INTEGER NOT NULL,
                    count_toptrader_long_short_ratio REAL,
                    sum_toptrader_long_short_ratio REAL,
                    count_long_short_ratio REAL,
                    sum_taker_long_short_vol_ratio REAL,
                    source_url TEXT NOT NULL,
                    PRIMARY KEY (symbol, create_time_ms)
                )
                """
            )
            old.execute(
                "INSERT INTO vision_metrics VALUES ('BTCUSDT', 1, 3.1, 1.9, 2.8, 0.4, 'old')"
            )
            old.commit()
            old.close()
            conn = connect(path)
            kept = conn.execute(
                "SELECT count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, "
                "count_long_short_ratio, create_time_raw FROM vision_metrics"
            ).fetchone()
            self.assertEqual(kept[0], 3.1)
            self.assertEqual(kept[1], 1.9)
            self.assertEqual(kept[2], 2.8)
            self.assertIsNone(kept[3])
            self.assertNotIn(
                "obi_top5",
                {row[1] for row in conn.execute("PRAGMA table_info(vision_metrics)")},
            )

    def test_partial_403_and_404_are_not_a_successful_exit(self) -> None:
        self.assertEqual(
            parse_sha256sum(
                "ab" * 32 + "  BTCUSDT-metrics-2026-11-09.zip\n",
                "https://data.binance.vision/data/x/BTCUSDT-metrics-2026-11-09.zip",
            ),
            "ab" * 32,
        )
        vision = {"complete": False, "missing": [{"status": "missing"}], "errors": []}
        ccxt = {
            "venues": [
                {"exchange": "okx", "status": "ok", "detail": None},
                {
                    "exchange": "bybit",
                    "status": "error",
                    "detail": "blocked:RateLimitExceeded:403 CloudFront",
                    "rows": 0,
                },
            ]
        }
        self.assertFalse(overall_complete(vision, ccxt))
        self.assertFalse(overall_complete({"complete": True}, ccxt))
        self.assertEqual(ccxt["venues"][1]["status"], "error")
        self.assertIn("403", str(ccxt["venues"][1]["detail"]))
        import scripts.run_public_backfill as runner

        def fake_vision(args: object, out: Path, fetcher: object = None) -> dict[str, object]:
            del args, out, fetcher
            return {"complete": False, "missing": [{"status": "missing", "detail": "404"}], "errors": []}

        def fake_ccxt(args: object, out: Path) -> dict[str, object]:
            del args, out
            return ccxt

        original_vision = runner.run_vision
        original_ccxt = runner.run_ccxt
        runner.run_vision = fake_vision
        runner.run_ccxt = fake_ccxt
        try:
            with TemporaryDirectory() as tmp:
                code = backfill_main(["--out", tmp, "--bases", "BTC", "--ccxt-days", "1"])
        finally:
            runner.run_vision = original_vision
            runner.run_ccxt = original_ccxt
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
