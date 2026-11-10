#!/usr/bin/env python3
"""Run the frozen news study against a read-only news database.

Missing news data writes ``unlock: false`` and does not invent a row count.
The engine is not modified.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from decision_engine.core.news_oos_calibration import (
    ARCHIVE_SERIES,
    SCHEMA_VERSION,
    audit_news_rows,
    build_effect_rows,
    dedup_events,
    evaluate_study,
    export_spec,
    hypotheses_from_rows,
    read_news_readonly,
    time_split,
)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _load_vision(path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        klines = [
            {
                "symbol": row["symbol"],
                "open_time": _dt(row["open_time_ms"]),
                "close_time": _dt(row["close_time_ms"]),
                "open": row["open"],
                "close": row["close"],
                "quote_volume": row["quote_volume"],
                "taker_buy_quote_volume": row["taker_buy_quote_volume"],
            }
            for row in conn.execute("SELECT * FROM vision_spot_klines")
        ]
        metrics = [
            {
                "symbol": row["symbol"],
                "create_time": _dt(row["create_time_ms"]),
                "count_toptrader_long_short_ratio": row["count_toptrader_long_short_ratio"],
            }
            for row in conn.execute("SELECT * FROM vision_metrics")
        ]
        depth = [
            {
                "symbol": row["symbol"],
                "timestamp": _dt(row["timestamp_ms"]),
                "percentage": row["percentage"],
                "notional": row["notional"],
            }
            for row in conn.execute(
                "SELECT * FROM vision_book_depth WHERE percentage IN (1.0, -1.0)"
            )
        ]
    finally:
        conn.close()
    return klines, metrics, depth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--news-db", default="")
    parser.add_argument("--vision-db", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--reps", type=int, default=1000)
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not args.news_db or not Path(args.news_db).is_file():
        report = evaluate_study(
            predictive_rows=0,
            test_clusters=0,
            hypotheses=[],
            series=ARCHIVE_SERIES,
            heat_available=False,
            news_db_sha256=None,
            split_times={"train_end": None, "validation_end": None, "test_end": None},
            synthetic=False,
        )
        report["status"] = "news_db_absent"
        report["export_spec"] = export_spec()
        report["rows_in_input"] = None
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "news_db_absent", "unlock": False, "schema": SCHEMA_VERSION}))
        return 0
    rows, digest = read_news_readonly(Path(args.news_db))
    audit = audit_news_rows(rows)
    events = dedup_events(rows)
    parts = time_split(events)
    klines: list[dict] = []
    metrics: list[dict] = []
    depth: list[dict] = []
    if args.vision_db and Path(args.vision_db).is_file():
        klines, metrics, depth = _load_vision(Path(args.vision_db))
    effects = build_effect_rows(parts["test"], klines, metrics, depth, lag_min=0)
    sensitivity = {
        str(lag): len(build_effect_rows(parts["test"], klines, metrics, depth, lag_min=lag))
        for lag in (0, 5, 15)
    }
    hypotheses = hypotheses_from_rows(effects, reps=args.reps)
    clusters = len({row["cluster"] for row in effects})
    split_times = {
        "train_end": _iso(parts["train"][-1]["fetched_at_dt"]) if parts["train"] else None,
        "validation_end": _iso(parts["validation"][-1]["fetched_at_dt"]) if parts["validation"] else None,
        "test_end": _iso(parts["test"][-1]["fetched_at_dt"]) if parts["test"] else None,
    }
    report = evaluate_study(
        predictive_rows=audit["predictive_candidates"],  # type: ignore[arg-type]
        test_clusters=clusters,
        hypotheses=hypotheses,
        series=ARCHIVE_SERIES,
        heat_available=False,
        news_db_sha256=digest,
        split_times=split_times,
        synthetic=False,
    )
    report["audit"] = audit
    report["deduped_events"] = len(events)
    report["lag_row_counts"] = sensitivity
    report["export_spec"] = export_spec()
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "unlock": report["unlock"], "rows": audit["rows_in_input"]}))
    return 0


def _iso(value: object) -> str:
    stamp = value if isinstance(value, datetime) else None
    if stamp is None:
        return ""
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
