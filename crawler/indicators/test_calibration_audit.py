"""Calibration audit on synthetic rows. No network and no production database."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from indicators.calibration_audit import (
    EMBARGO_MIN,
    audit_ccxt_rows,
    bar_return,
    build_report,
    coverage,
    dedup_bars,
    holm,
    load_vision_dir,
    main,
    parse_vision_bookdepth_csv,
    parse_vision_metrics_csv,
    prepare_news_rows,
    split_and_purge,
)


T0 = 1_700_000_000_000
STEP = 480 * 60_000


def _iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _news(index: int, fetched_ms: int, *, published_ms: int | None = None, tier: str = "whitelist") -> dict:
    published = fetched_ms if published_ms is None else published_ms
    return {
        "id": index,
        "source": "coindesk",
        "source_tier": tier,
        "title": f"headline {index}",
        "url": f"https://example.test/{index}",
        "published_at": _iso(published),
        "fetched_at": _iso(fetched_ms),
        "onchain_status": "aligned" if index == 0 else "pending",
    }


def _feature(index: int, fetched_ms: int, *, high: bool, source: str = "live_top5") -> dict:
    ts = fetched_ms - 60_000
    return {
        "event_id": index,
        "symbol": "BTCUSDT",
        "obi_top5": 0.8 if high else -0.8,
        "obi_top5_source": source,
        "obi_source_ts_ms": ts,
        "taker_buy_ratio": 0.70 if high else 0.20,
        "taker_source_ts_ms": ts,
        "whale_retail_divergence": 2.0 if high else 0.2,
        "whale_source_ts_ms": ts,
        "heat_prev": 1 if high else 5,
        "heat_last": 6 if high else 4,
        "heat_observed_at_ms": ts,
        "book_depth_proxy": 0.99,
    }


def _bars_for(fetched_ms: int, move: float) -> list[dict]:
    bars = []
    for offset in (0, 5, 10, 15, 20, 25, 60, 65, 70, 240):
        open_ms = fetched_ms + offset * 60_000
        price = 100.0 if offset < 15 else 100.0 * (1.0 + move)
        bars.append({"open_ms": open_ms, "open": price, "high": price, "low": price, "close": price})
    return bars


def _population(count: int, move: float) -> tuple[list[dict], list[dict], dict[str, list[dict]]]:
    news = []
    features = []
    bars: list[dict] = []
    for index in range(count):
        fetched = T0 + index * STEP
        news.append(_news(index, fetched))
        high = index % 2 == 0
        features.append(_feature(index, fetched, high=high))
        signed = move if high else -move
        wobble = 0.001 if index % 4 in (0, 1) else -0.001
        bars.extend(_bars_for(fetched, signed + wobble))
    return news, features, {"BTCUSDT": bars}


class CalibrationAuditTests(unittest.TestCase):
    def test_late_archive_uses_lag_not_row_order(self) -> None:
        rows = [
            _news(index, T0, published_ms=T0 - (91 + index) * 60_000)
            for index in range(5)
        ]
        rows.append(_news(99, T0 + 86_400_000))
        prepared = prepare_news_rows(rows)
        self.assertEqual(prepared["rows_counted_here"], 6)
        self.assertEqual(prepared["late_archive_rows"], 5)
        self.assertEqual([item["id"] for item in prepared["eligible"]], [99])
        self.assertNotEqual(prepared["rows_counted_here"], 1859)
        self.assertNotEqual(prepared["late_archive_rows"], 164)
        self.assertFalse(prepared["stored_labels_trusted"])

    def test_rumor_and_duplicate_title_stay_out(self) -> None:
        rows = [
            _news(1, T0, tier="rumor"),
            _news(2, T0),
            _news(3, T0 + 60_000),
        ]
        rows[2]["title"] = rows[1]["title"]
        prepared = prepare_news_rows(rows)
        self.assertEqual(prepared["rumor_or_non_whitelist_rows"], 1)
        self.assertEqual(prepared["title_dedup_dropped"], 1)
        self.assertEqual([item["id"] for item in prepared["eligible"]], [2])

    def test_entry_ignores_the_bar_that_opened_before_the_fetch(self) -> None:
        bars = [
            {"open_ms": 1_000, "open": 50.0},
            {"open_ms": 1_000 + 300_000, "open": 100.0},
            {"open_ms": 1_000 + 300_000 + 3_600_000, "open": 110.0},
        ]
        ret = bar_return(bars, event_ms=1_000 + 1, horizon_min=60)
        self.assertAlmostEqual(ret, 0.10)

    def test_overlap_and_embargo_drop_crossing_windows(self) -> None:
        events = [{"fetched_ms": T0 + index * 60_000, "id": index} for index in range(10)]
        purged = split_and_purge(events, embargo_ms=EMBARGO_MIN * 60_000)
        self.assertLess(len(purged["train"]) + len(purged["validation"]) + len(purged["test"]), 10)
        self.assertGreater(purged["embargo_dropped"] + purged["train_overlap_dropped"], 0)

    def test_bookdepth_proxy_is_not_top5_and_does_not_unlock(self) -> None:
        text = "timestamp,percentage,depth,notional\n1000,-1,2,30\n1000,1,3,10\n1000,-5,9,99\n"
        parsed = parse_vision_bookdepth_csv(text, "BTCUSDT")
        self.assertEqual(parsed[0]["book_depth_proxy_kind"], "bookdepth_percentage_bucket_1pct_notional")
        self.assertFalse(parsed[0]["is_obi_top5"])
        self.assertAlmostEqual(parsed[0]["book_depth_proxy"], 0.5)
        metrics = parse_vision_metrics_csv(
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2026-11-01 00:00:00,BTCUSDT,1,2,1,1,1,1.4\n"
        )
        self.assertFalse(metrics[0]["used_as_taker_buy_ratio"])
        self.assertEqual(metrics[0]["futures_taker_long_short_vol_ratio"], 1.4)
        news, features, bars = _population(40, 0.02)
        for feature in features:
            feature["obi_top5_source"] = "bookdepth_percentage_bucket"
        report = build_report(
            news_rows=news,
            feature_rows=features,
            bars_by_symbol=bars,
            benchmark_symbol="BTCUSDT",
            vision={"availability": "in_memory", "book_depth_is_obi_top5": False},
        )
        self.assertIsNone(report["news"]["onchain"]["onchain_calibration"])
        self.assertEqual(report["news"]["onchain"]["calibration_status"], "not_calibrated")
        self.assertFalse(report["live_enabled"])

    def test_no_symbol_map_does_not_guess_a_coin(self) -> None:
        news, features, bars = _population(80, 0.02)
        report = build_report(news_rows=news, feature_rows=features, bars_by_symbol=bars)
        self.assertEqual(report["news"]["onchain"]["reason"], "no_symbol_map_or_benchmark")
        self.assertIsNone(report["news"]["onchain"]["onchain_calibration"])

    def test_significant_oos_emits_the_gate_fields_without_going_live(self) -> None:
        news, features, bars = _population(400, 0.02)
        report = build_report(
            news_rows=news,
            feature_rows=features,
            bars_by_symbol=bars,
            benchmark_symbol="BTCUSDT",
            fee_bps=10,
        )
        onchain = report["news"]["onchain"]
        self.assertEqual(onchain["calibration_status"], "oos_significant", onchain.get("reason"))
        emitted = onchain["onchain_calibration"]
        self.assertEqual(
            set(emitted),
            {
                "min_heat_delta",
                "min_obi_aligned",
                "min_taker_aligned",
                "min_whale_aligned",
                "max_obi_divergent",
                "max_taker_divergent",
                "max_whale_divergent",
                "max_feature_age_min",
            },
        )
        self.assertGreater(emitted["min_obi_aligned"], emitted["max_obi_divergent"])
        self.assertFalse(report["live_enabled"])
        self.assertFalse(report["writes_news_events"])
        self.assertFalse(report["thresholds_changed"])

    def test_fee_larger_than_the_move_blocks_unlock(self) -> None:
        news, features, bars = _population(400, 0.0002)
        report = build_report(
            news_rows=news,
            feature_rows=features,
            bars_by_symbol=bars,
            benchmark_symbol="BTCUSDT",
            fee_bps=10,
        )
        self.assertIsNone(report["news"]["onchain"]["onchain_calibration"])
        self.assertIn("fee_sensitivity", report["news"]["onchain"]["reason"])

    def test_usd_and_usdt_are_not_pooled_and_gaps_are_not_filled(self) -> None:
        rows = []
        for index in range(3):
            ts = T0 + index * 300_000
            rows.append(
                {"exchange": "okx", "symbol": "BTC/USDT", "timeframe": "5m", "ts": ts,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1, "quote_volume": None}
            )
            rows.append(
                {"exchange": "coinbase", "symbol": "BTC/USD", "timeframe": "5m", "ts": ts,
                 "open": 100, "high": 101, "low": 99, "close": 101, "volume": 1, "quote_volume": 100}
            )
        rows.append(
            {"exchange": "okx", "symbol": "BTC/USDT", "timeframe": "5m", "ts": T0 + 600_000,
             "open": 100, "high": 130, "low": 99, "close": 120, "volume": 1, "quote_volume": None}
        )
        report = audit_ccxt_rows(rows, fee_bps=10)
        self.assertGreater(report["usd_usdt_pairs_not_pooled"], 0)
        self.assertEqual(report["same_quote_comparisons"], 0)
        self.assertEqual(report["calibration_status"], "not_calibrated")
        okx = next(item for item in report["series"] if item["exchange"] == "okx")
        coinbase = next(item for item in report["series"] if item["exchange"] == "coinbase")
        self.assertEqual(okx["volume_unit"], "base_volume_only")
        self.assertEqual(coinbase["quote"], "USD")
        self.assertFalse(coinbase["claim_30d"])
        self.assertIn("300", coinbase["coinbase_limit_note"])
        self.assertEqual(okx["coverage"]["missing_bars"], 0)
        gapped = coverage(
            [{"open_ms": T0}, {"open_ms": T0 + 600_000}],
        )
        self.assertEqual(gapped["missing_bars"], 1)
        self.assertFalse(gapped["claim_30d"])
        conflict = dedup_bars(
            [
                {"open_ms": T0, "open": 1, "high": 1, "low": 1, "close": 1},
                {"open_ms": T0, "open": 2, "high": 2, "low": 2, "close": 2},
            ]
        )
        self.assertEqual(conflict["conflicts"], 1)
        self.assertEqual(conflict["bars"], [])

    def test_holm_keeps_order_and_can_reject_a_raw_pass(self) -> None:
        adjusted = holm([0.01, 0.04, 0.03])
        self.assertEqual(adjusted, [0.03, 0.06, 0.06])

    def test_missing_database_writes_queries_and_no_invented_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            code = main(["--out-dir", str(out), "--news-db", str(Path(tmp) / "missing.db")])
            self.assertEqual(code, 0)
            payload = json.loads((out / "calibration_audit.json").read_text(encoding="utf-8"))
            text = (out / "calibration_audit.md").read_text(encoding="utf-8")
        self.assertEqual(payload["news"]["availability"], "missing_db")
        self.assertEqual(payload["news"]["rows_counted_here"], 0)
        self.assertNotIn("1859", json.dumps(payload["news"]["rows_counted_here"]))
        self.assertIsNone(payload["news"]["onchain"]["onchain_calibration"])
        self.assertFalse(payload["live_enabled"])
        self.assertIn("PRAGMA table_info(news_events)", text)
        self.assertIn("PRAGMA table_info(ccxt_ohlcv)", text)
        self.assertIn("PRAGMA table_info(x_watch)", text)

    def test_sqlite_loader_reports_real_columns_and_x_watch_is_not_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            news = Path(tmp) / "news.db"
            watch = Path(tmp) / "watch.db"
            conn = sqlite3.connect(news)
            conn.execute(
                """
                CREATE TABLE news_events (
                    id INTEGER PRIMARY KEY, source TEXT, source_tier TEXT, title TEXT,
                    url TEXT UNIQUE, published_at TEXT, fetched_at TEXT, summary TEXT,
                    raw_json TEXT, onchain_status TEXT, notes TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO news_events VALUES (1,'coindesk','whitelist','t','https://example.test/a',?,?, '', '', 'pending','')",
                (_iso(T0 - 120 * 60_000), _iso(T0)),
            )
            conn.commit()
            conn.close()
            wconn = sqlite3.connect(watch)
            wconn.execute(
                """
                CREATE TABLE x_watch (
                    chain_id TEXT, token_address TEXT, symbol TEXT, query_key TEXT,
                    mode TEXT, mention_count_total INTEGER, mention_count_prev INTEGER,
                    mention_count_last INTEGER, last_searched_at TEXT, created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            wconn.execute(
                "INSERT INTO x_watch VALUES ('solana','addr','BTC','q','early',1,1,1,'2026-10-31T00:00:00Z','2026-10-01T00:00:00Z','2026-10-31T00:00:00Z')"
            )
            wconn.commit()
            wconn.close()
            vision = Path(tmp) / "vision" / "spot" / "klines"
            vision.mkdir(parents=True)
            (vision / "BTCUSDT-5m-2026-11-01.csv").write_text(
                "1700000000000,100,101,99,100,5,1700000299999,500,10,1,50,0\n",
                encoding="utf-8",
            )
            book = Path(tmp) / "vision" / "futures" / "um" / "bookDepth"
            book.mkdir(parents=True)
            (book / "BTCUSDT-bookDepth-2026-11-01.csv").write_text(
                "timestamp,percentage,depth,notional\n1700000000000,-1,1,4\n1700000000000,1,1,2\n",
                encoding="utf-8",
            )
            out = Path(tmp) / "out"
            code = main(
                [
                    "--out-dir",
                    str(out),
                    "--news-db",
                    str(news),
                    "--x-watch-db",
                    str(watch),
                    "--vision-dir",
                    str(Path(tmp) / "vision"),
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads((out / "calibration_audit.json").read_text(encoding="utf-8"))
            loaded = load_vision_dir(Path(tmp) / "vision")
        self.assertEqual(payload["news"]["rows_counted_here"], 1)
        self.assertEqual(payload["news"]["late_archive_rows"], 1)
        self.assertEqual(payload["news"]["eligible_after_lag_and_dedup"], 0)
        self.assertEqual(payload["x_watch"]["rows"], 1)
        self.assertFalse(payload["x_watch"]["joined_to_past_news"])
        self.assertFalse(payload["x_watch"]["history_snapshots"])
        self.assertEqual(payload["vision"]["spot_kline_rows"], 1)
        self.assertEqual(payload["vision"]["book_depth_rows"], 1)
        self.assertFalse(payload["vision"]["book_depth_is_obi_top5"])
        self.assertIn("fetched_at", payload["pragma"]["news"]["tables"]["news_events"]["columns"])
        self.assertFalse(loaded["online_at_bar"])


if __name__ == "__main__":
    unittest.main()
