#!/usr/bin/env python3
"""Download public Vision files and CCXT 5m bars into an isolated directory.

Does not read production databases, does not change environment variables,
and does not start a trader.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import ccxt
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from decision_engine.core.ccxt_public_backfill import (
    VENUES,
    backfill_symbol,
    core_bases,
    make_ccxt_fetcher,
    sleep_limiter,
    symbol_for,
)
from decision_engine.core.cross_venue_deviation import (
    gap_counts,
    load_venue_bars,
    pair_exact_bars,
    summarize_pairs,
)
from decision_engine.core.vision_backfill import (
    RateLimiter,
    connect,
    coverage_report,
    core_usdt_symbols,
    fetch_bytes,
    ingest_zip,
    iter_plan,
    write_manifest,
    closed_utc_days,
)


def _end_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(timezone.utc).date() - timedelta(days=1)


def run_vision(args: argparse.Namespace, out: Path) -> dict[str, object]:
    symbols = core_usdt_symbols(tuple(args.bases.split(",")))
    dates = closed_utc_days(_end_date(args.end), args.days)
    db = connect(out / "vision.db")
    limiter = RateLimiter(args.min_interval)
    for kind, symbol, url in iter_plan(symbols, dates):
        fetched = fetch_bytes(url, limiter)
        ingest_zip(db, kind, symbol, fetched)
        print(f"{fetched.status} {kind} {symbol} {url} {fetched.detail or fetched.byte_count}")
    report = coverage_report(db, symbols)
    report["dates"] = [item.isoformat() for item in dates]
    write_manifest(out / "vision_manifest.json", report)
    db.close()
    return report


def run_ccxt(args: argparse.Namespace, out: Path) -> dict[str, object]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - args.ccxt_days * 24 * 3600 * 1000
    conn = sqlite3.connect(out / "ccxt.db")
    limiter = sleep_limiter(args.min_interval)
    results = []
    for exchange_id in VENUES:
        exchange_cls = getattr(ccxt, exchange_id)
        exchange = exchange_cls({"enableRateLimit": True, "timeout": 20000})
        try:
            exchange.load_markets()
        except Exception as exc:  # noqa: BLE001
            for base in args.bases.split(","):
                results.append(
                    {
                        "exchange": exchange_id,
                        "base": base,
                        "status": "error",
                        "detail": f"{type(exc).__name__}:{str(exc)[:180]}",
                        "rows": 0,
                    }
                )
            continue
        for base in args.bases.split(","):
            symbol = symbol_for(exchange_id, base)
            if symbol not in exchange.markets:
                results.append(
                    {
                        "exchange": exchange_id,
                        "base": base,
                        "symbol": symbol,
                        "quote": VENUES[exchange_id]["quote"],
                        "status": "not_listed",
                        "rows": 0,
                        "detail": "absent from public markets",
                    }
                )
                continue
            fetcher = make_ccxt_fetcher(exchange, symbol, limiter)
            results.append(
                backfill_symbol(
                    conn,
                    exchange_id,
                    base,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    fetch_page=fetcher,
                    limit=args.ccxt_limit,
                )
            )
        try:
            exchange.close()
        except Exception:
            pass
    pairs = []
    for base in args.bases.split(","):
        left = load_venue_bars(conn, "okx", f"{base}/USDT")
        right = load_venue_bars(conn, "bybit", f"{base}/USDT")
        paired = pair_exact_bars(left, right)
        pairs.append(
            {
                "base": base,
                "quote": "USDT",
                "left": "okx",
                "right": "bybit",
                **gap_counts(left, right),
                **summarize_pairs(paired),
            }
        )
    report = {
        "core_bases": list(core_bases()),
        "requested_bases": args.bases.split(","),
        "ccxt_days": args.ccxt_days,
        "venues": results,
        "usdt_pairs": pairs,
        "usd_not_pooled_with_usdt": True,
        "interpolation": "none",
    }
    (out / "ccxt_manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    conn.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--ccxt-days", type=int, default=30)
    parser.add_argument("--end", default=None)
    parser.add_argument("--bases", default=",".join(core_bases()))
    parser.add_argument("--min-interval", type=float, default=0.4)
    parser.add_argument("--ccxt-limit", type=int, default=100)
    parser.add_argument("--skip-vision", action="store_true")
    parser.add_argument("--skip-ccxt", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {"bases": args.bases}
    if not args.skip_vision:
        summary["vision"] = run_vision(args, out)
    if not args.skip_ccxt:
        summary["ccxt"] = run_ccxt(args, out)
    print(json.dumps({"wrote": str(out), "complete": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
