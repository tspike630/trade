"""Commands for archive staging and calibration. Nothing runs unless named.

``probe`` fetches one public file of each Binance Vision kind and one CCXT
page. ``analyze`` reads a local news database and a stage database.
``upsert-ccxt`` is the only command that writes an existing ccxt database.
``activate`` writes a candidate JSON and refuses when the gates failed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from research.hist_calibration.canon import PERP_AND_SPOT, venue_markets
from research.hist_calibration.ccxt_public import (
    probe_exchange,
    rollback_upsert,
    upsert_into_ccxt_db,
)
from research.hist_calibration.study import book_depth_is_not_obi, decide, inventory_news, write_json
from research.hist_calibration.vision import (
    download_checked,
    parse_book_depth_csv,
    parse_metrics_csv,
    parse_spot_kline_csv,
    vision_url,
    _rows_from_zip,
)


def _describe_zip(kind: str, dest: Path, file_day) -> dict:
    rows, err = _rows_from_zip(dest.read_bytes())
    item = {"csv_error": err, "csv_rows": len(rows)}
    if err or not rows:
        return item
    if kind == "metrics":
        parsed = parse_metrics_csv(rows)
        item["header"] = parsed["header"]
        item["parsed_rows"] = len(parsed["rows"])
        item["account_and_position_separate"] = True
        item["obi_top5"] = None
    elif kind == "bookDepth":
        parsed = parse_book_depth_csv(rows)
        item["header"] = parsed["header"]
        item["parsed_rows"] = len(parsed["rows"])
        item["series"] = parsed["series"]
        item["obi_top5"] = None
    elif kind == "klines":
        parsed = parse_spot_kline_csv(rows, file_day=file_day)
        item["header"] = parsed["header"]
        item["parsed_rows"] = len(parsed["rows"])
        item["date_mismatches"] = len(parsed["date_mismatches"])
        item["gaps"] = len(parsed["gaps"])
        item["duplicate_opens"] = len(parsed["duplicate_opens"])
    return item


def _probe(args: argparse.Namespace) -> int:
    today = datetime.now(timezone.utc).date()
    closed_day = today.fromordinal(today.toordinal() - 1)
    symbol = args.symbol
    out_dir = Path(args.cache)
    out_dir.mkdir(parents=True, exist_ok=True)
    kinds = ("metrics", "bookDepth", "klines")
    files = []
    for kind in kinds:
        for day, fetch_zip in ((closed_day, True), (today, False)):
            url = vision_url(kind, symbol, day, monthly=False, interval="5m")
            dest = out_dir / Path(url).name
            if fetch_zip:
                item = download_checked(url, dest, max_bytes=args.max_bytes)
                if item.get("status") in {"downloaded", "cached"} and dest.is_file():
                    item.update(_describe_zip(kind, dest, day))
            else:
                from research.hist_calibration.vision import checksum_url, fetch_bytes

                status, body, err = fetch_bytes(checksum_url(url), timeout=30.0, max_bytes=4096)
                item = {
                    "url": url,
                    "status": "checksum_only",
                    "checksum_status": status,
                    "detail": err,
                    "released": status == 200 and bool(body),
                }
            item["kind"] = kind
            item["utc_day"] = day.isoformat()
            item["closed_day"] = day == closed_day
            files.append(item)
            time.sleep(0.3)
    markets = venue_markets(symbol)
    ccxt_pages = []
    now_ms = int(time.time() * 1000)
    for venue in ("okx", "bybit", "coinbase"):
        spec = markets[venue]
        page = probe_exchange(venue, spec["symbol"], now_ms=now_ms)
        page["quote"] = spec["quote"]
        page["comparable_to_usdt"] = spec.get("comparable_to_usdt", spec["quote"] == "USDT")
        ccxt_pages.append(page)
    body = {
        "kind": "public_schema_probe",
        "not_a_box_calibration": True,
        "symbol": symbol,
        "canon_not_downloaded": list(PERP_AND_SPOT),
        "vision": files,
        "ccxt": ccxt_pages,
        "book_depth": book_depth_is_not_obi([]),
    }
    write_json(Path(args.out), body)
    print(json.dumps({"out": args.out, "vision_status": [item.get("status") for item in files]}, indent=2))
    return 0


def _analyze(args: argparse.Namespace) -> int:
    from research.hist_calibration.stage_consumer import (
        attach_verified_returns,
        load_manifest,
        study_from_stage,
    )

    news = inventory_news(Path(args.news_db)) if args.news_db else {"availability": "missing_db", "rows": None}
    manifest = load_manifest(Path(args.manifest)) if args.manifest else None
    stage_report: dict[str, object] = {
        "present": bool(args.stage),
        "study_ready": False,
        "raw_stage_study_ready": False,
    }
    events = []
    for event in news.get("events") or []:
        events.append({**event, "symbol": args.benchmark_symbol})
    if args.stage:
        if manifest is None:
            stage_report["error"] = "missing_manifest"
        else:
            conn = sqlite3.connect(f"file:{Path(args.stage).resolve().as_posix()}?mode=ro", uri=True)
            try:
                stage_report = study_from_stage(conn, manifest)
            finally:
                conn.close()
            events = attach_verified_returns(events, stage_report["bars"])
    else:
        for event in events:
            event["forward_return"] = None
            event["obi_top5_asof"] = None
            event["taker_asof"] = None
            event["whale_asof"] = None
            event["heat_delta"] = None
    body = decide(
        {
            "events": events,
            "news_availability": news.get("availability"),
            "late_archive_rows": news.get("late_archive_rows"),
            "raw_rows": news.get("rows"),
            "book_depth_used_as_obi": False,
            "obi_from_book_depth": False,
            "x_watch_used_as_history": False,
            "scope": "verified_stage" if stage_report.get("study_ready") else "stage_not_study_ready",
        }
    )
    body["news_inventory"] = {key: value for key, value in news.items() if key != "events"}
    body["stage"] = {
        "study_ready": stage_report.get("study_ready", False),
        "raw_stage_study_ready": False,
        "conflicts": stage_report.get("conflicts", []),
        "verified_bars": len(stage_report.get("bars") or []),
        "ignored_rows": stage_report.get("ignored_rows"),
        "verification_ok": (stage_report.get("verification") or {}).get("ok") if isinstance(stage_report.get("verification"), dict) else False,
        "errors": (stage_report.get("verification") or {}).get("errors") if isinstance(stage_report.get("verification"), dict) else [stage_report.get("error")],
    }
    body["x_watch"] = "online_only"
    body["obi_top5_from_book_depth"] = False
    if not stage_report.get("study_ready"):
        body["calibration_status"] = "not_ready"
        body["gates_passed"] = False
        body["onchain_calibration"] = None
        if "stage_not_study_ready" not in body["gates_failed"]:
            body["gates_failed"] = [*body["gates_failed"], "stage_not_study_ready"]
    write_json(Path(args.out), body)
    print(json.dumps({
        "status": body["calibration_status"],
        "study_ready": body["stage"]["study_ready"],
        "gates_failed": body["gates_failed"],
        "rows": news.get("rows"),
    }))
    return 0 if body["calibration_status"] == "not_ready" or body["gates_passed"] else 3


def _upsert(args: argparse.Namespace) -> int:
    report = upsert_into_ccxt_db(Path(args.stage), Path(args.target))
    print(json.dumps(report))
    return 0


def _rollback(args: argparse.Namespace) -> int:
    report = rollback_upsert(Path(args.target), Path(args.manifest))
    print(json.dumps(report))
    return 0


def _activate(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    if not report.get("gates_passed"):
        write_json(Path(args.out), {"status": "not_ready", "activated": False, "source": args.report})
        print("not_ready")
        return 3
    write_json(
        Path(args.out),
        {
            "status": "candidate_not_activated",
            "activated": False,
            "live_router_unchanged": True,
            "calibration": report.get("onchain_calibration"),
        },
    )
    print("candidate_not_activated")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Historical calibration staging. Default does not download or write.")
    sub = parser.add_subparsers(dest="command")

    probe = sub.add_parser("probe", help="One public file per Vision kind and one CCXT page.")
    probe.add_argument("--symbol", default="BTCUSDT")
    probe.add_argument("--cache", default="data/hist_calibration/cache")
    probe.add_argument("--out", default="data/hist_calibration/probe.json")
    probe.add_argument("--max-bytes", type=int, default=40_000_000)
    probe.set_defaults(func=_probe)

    analyze = sub.add_parser("analyze", help="Read-only news inventory and gate decision.")
    analyze.add_argument("--news-db", default=None)
    analyze.add_argument("--stage", default=None)
    analyze.add_argument("--manifest", default=None)
    analyze.add_argument("--benchmark-symbol", default=None)
    analyze.add_argument("--out", required=True)
    analyze.set_defaults(func=_analyze)

    upsert = sub.add_parser("upsert-ccxt", help="Copy a validated stage into ccxt_ohlcv.")
    upsert.add_argument("--stage", required=True)
    upsert.add_argument("--target", required=True)
    upsert.set_defaults(func=_upsert)

    rollback = sub.add_parser("rollback-ccxt", help="Delete rows inserted by upsert-ccxt.")
    rollback.add_argument("--target", required=True)
    rollback.add_argument("--manifest", required=True)
    rollback.set_defaults(func=_rollback)

    activate = sub.add_parser("activate", help="Write a candidate file only when gates passed.")
    activate.add_argument("--report", required=True)
    activate.add_argument("--out", required=True)
    activate.set_defaults(func=_activate)

    stage = sub.add_parser("stage-init", help="Create an empty archive stage database.")
    stage.add_argument("--db", required=True)
    stage.set_defaults(func=lambda args: _stage_init(args))

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return int(args.func(args))


def _stage_init(args: argparse.Namespace) -> int:
    from research.hist_calibration.vision import ensure_stage

    conn = ensure_stage(Path(args.db))
    conn.close()
    print(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
