#!/usr/bin/env python3
"""Download public Vision files and CCXT 5m bars into an isolated directory.

Does not read production databases, does not change environment variables,
and does not start a trader.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import sys
import time

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
from decision_engine.core.cross_venue_calibration import evaluate_cross_venue, usd_not_calibrated
from decision_engine.core.vision_backfill import (
    METRIC_AVAILABILITY_BIN_END,
    METRIC_AVAILABILITY_UNKNOWN,
    RateLimiter,
    cached_fetch,
    connect,
    coverage_report,
    core_usdt_symbols,
    fetch_bytes,
    import_planned_file,
    iter_plan,
    write_manifest,
    closed_utc_days,
)


def _end_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(timezone.utc).date() - timedelta(days=1)


def run_vision(
    args: argparse.Namespace,
    out: Path,
    fetcher: object = fetch_bytes,
) -> dict[str, object]:
    symbols = core_usdt_symbols(tuple(args.bases.split(",")))
    dates = closed_utc_days(_end_date(args.end), args.days)
    db = connect(out / "vision.db")
    limiter = RateLimiter(args.min_interval)
    mode = getattr(args, "metric_availability", METRIC_AVAILABILITY_UNKNOWN)
    cache_dir = out / "cache"
    for kind, symbol, url in iter_plan(symbols, dates):
        outcome = import_planned_file(
            db,
            kind,
            symbol,
            url,
            fetcher,
            limiter,
            availability_mode=mode,
            cache_dir=cache_dir,
        )
        print(f"{outcome} {kind} {symbol} {url}")
    report = coverage_report(db, symbols)
    report["dates"] = [item.isoformat() for item in dates]
    report["metric_availability_requested"] = mode
    write_manifest(out / "vision_manifest.json", report)
    db.close()
    return report


_VENUE_COMPLETE = {"ok", "skipped_covered"}


def overall_complete(vision: dict[str, object] | None, ccxt: dict[str, object] | None) -> bool:
    """False when any planned file or venue is short. A Bybit 403 stays a failure."""
    if vision is None and ccxt is None:
        return False
    if vision is not None and not vision.get("complete"):
        return False
    if ccxt is None:
        return vision is not None
    venues = list(ccxt.get("venues") or [])
    if not venues:
        return False
    for item in venues:
        if item.get("status") not in _VENUE_COMPLETE:
            return False
        if item.get("coverage") not in (None, "complete"):
            return False
        if item.get("missing_bars"):
            return False
        if item.get("missing_timestamps_ms"):
            return False
    return True


class OutputWriterLock:
    """One writer for an output directory. A busy lock is not waited out forever."""

    def __init__(self, directory: Path, wait_s: float = 2.0) -> None:
        self.path = directory / ".writer.lock"
        self.wait_s = wait_s
        self._handle: object | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        deadline = time.monotonic() + max(0.0, self.wait_s)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.05)
                continue
            self._handle = handle
            return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


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
            detail = f"{type(exc).__name__}:{str(exc)[:180]}"
            blocked = "403" in detail or "CloudFront" in detail or "Forbidden" in detail
            for base in args.bases.split(","):
                results.append(
                    {
                        "exchange": exchange_id,
                        "base": base,
                        "status": "error",
                        "coverage": "blocked" if blocked else "error",
                        "detail": detail,
                        "rows": 0,
                        "missing_bars": None,
                        "missing_timestamps_ms": [],
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
                        "status": "unsupported",
                        "coverage": "unsupported",
                        "rows": 0,
                        "missing_bars": None,
                        "missing_timestamps_ms": [],
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
    studies = []
    for base in args.bases.split(","):
        left = load_venue_bars(conn, "okx", f"{base}/USDT")
        right = load_venue_bars(conn, "bybit", f"{base}/USDT")
        if not left or not right:
            studies.append(
                {
                    "base": base,
                    "quote": "USDT",
                    "status": "insufficient",
                    "reason": "need both OKX and Bybit USDT bars",
                    "gaps": gap_counts(left, right),
                    "summary": summarize_pairs(pair_exact_bars(left, right)),
                    "unlock_news_gate": False,
                }
            )
            continue
        study = evaluate_cross_venue(left, right, quote="USDT")
        study["base"] = base
        study["summary"] = summarize_pairs(pair_exact_bars(left, right))
        studies.append(study)
    report = {
        "core_bases": list(core_bases()),
        "requested_bases": args.bases.split(","),
        "ccxt_days": args.ccxt_days,
        "venues": results,
        "usdt_studies": studies,
        "usd": usd_not_calibrated(
            "Coinbase Exchange is USD. This package has no second USD venue and no timestamped USDTUSD series."
        ),
        "usd_not_pooled_with_usdt": True,
        "interpolation": "none",
    }
    write_manifest(out / "ccxt_manifest.json", report)
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
    parser.add_argument(
        "--metric-availability",
        choices=(METRIC_AVAILABILITY_UNKNOWN, METRIC_AVAILABILITY_BIN_END),
        default=METRIC_AVAILABILITY_UNKNOWN,
        help=(
            "unknown leaves metric feature availability unset. "
            "explicit_conservative_bin_end stores label plus 5 minutes as a "
            "research clock and does not attest the CSV label."
        ),
    )
    parser.add_argument("--lock-wait", type=float, default=2.0)
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lock = OutputWriterLock(out, wait_s=args.lock_wait)
    if not lock.acquire():
        print(json.dumps({"wrote": str(out), "complete": False, "error": "writer_lock_busy"}))
        return 2
    vision = None
    ccxt_report = None
    try:
        if not args.skip_vision:
            vision = run_vision(args, out)
        if not args.skip_ccxt:
            ccxt_report = run_ccxt(args, out)
        complete = overall_complete(vision, ccxt_report)
    finally:
        lock.release()
    print(json.dumps({"wrote": str(out), "complete": complete}, indent=2))
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
