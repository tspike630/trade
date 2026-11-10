#!/usr/bin/env python3
"""Import and fixture check for a directory that has no live gate module."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import decision_engine.core.news_oos_calibration as news
import decision_engine.core.vision_backfill as vision
from decision_engine.core.ccxt_public_backfill import (
    PageResult,
    backfill_symbol,
)
from decision_engine.core.cross_venue_calibration import evaluate_cross_venue


def main() -> int:
    if "decision_engine.core.news_shock_gate" in sys.modules:
        print("live gate was imported", file=sys.stderr)
        return 1
    audit = news.audit_news_rows(
        [
            {
                "published_at": "2026-11-01T00:00:00Z",
                "fetched_at": "2026-11-01T00:10:00Z",
            },
            {
                "published_at": "2026-10-01T00:00:00Z",
                "fetched_at": "2026-11-01T00:00:00Z",
            },
        ]
    )
    if audit["rows_in_input"] != 2 or audit["late_archive_excluded"] != 1:
        print(audit, file=sys.stderr)
        return 1
    if audit["reference_counts_not_used"]["used_as_this_result"]:
        return 1
    report = news.evaluate_study(
        predictive_rows=audit["predictive_candidates"],
        test_clusters=1,
        hypotheses=[],
        series=news.ARCHIVE_SERIES,
        heat_available=False,
        news_db_sha256=None,
        split_times={"train_end": None, "validation_end": None, "test_end": None},
        synthetic=False,
    )
    if report["live_gate_unlock"] or report["will_not_fully_unlock_onchain_calibration"] is not True:
        print(report, file=sys.stderr)
        return 1
    payload_text = (
        "create_time,symbol,sum_open_interest,sum_open_interest_value,"
        "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
        "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        "2026-11-08 00:35:00,BTCUSDT,1,2,3,1,0.5,0.2\n"
    )
    rows = vision.parse_metrics_csv(payload_text)
    if "whale_retail_divergence" in rows[0] or rows[0]["count_toptrader_long_short_ratio"] == rows[0]["count_long_short_ratio"]:
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(Path(tmp) / "ccxt.db")
        calls: list[int] = []

        def fetch_page(timeframe: str, since: int, limit: int) -> PageResult:
            del timeframe, limit
            calls.append(since)
            return PageResult([[since, 1, 1, 1, 1, 1]], None)

        backfill_symbol(conn, "okx", "BTC", start_ms=0, end_ms=600_000, fetch_page=fetch_page, limit=1)
        calls.clear()
        second = backfill_symbol(
            conn, "okx", "BTC", start_ms=0, end_ms=600_000, fetch_page=fetch_page, limit=1
        )
        if calls or second["status"] != "skipped_covered":
            print(second, calls, file=sys.stderr)
            return 1
    left = {index * 300_000: {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0} for index in range(5)}
    study = evaluate_cross_venue(left, left, quote="USDT")
    if study["status"] != "insufficient" or study["unlock_news_gate"]:
        print(study, file=sys.stderr)
        return 1
    if "decision_engine.core.news_shock_gate" in sys.modules:
        return 1
    print("OFFLINE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
