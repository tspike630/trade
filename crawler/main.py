"""CLI entry for DexScreener crawler (Phase 1–5)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from config import (
    ALERT_COOLDOWN_SEC,
    ALERT_LIQUIDITY_DROP_PCT,
    ALERT_PRICE_CHANGE_PCT,
    ALERT_VOLUME_H24_CHANGE_PCT,
    ALERT_WEBHOOK_URL,
    ASYNC_MAX_CONCURRENCY,
    BATCH_SIZE,
    DB_PATH,
    HEARTBEAT_STALE_SEC,
    POLL_INTERVAL_SECONDS,
    PROFILE_POLL_INTERVAL_SEC,
    RECHECK_ZERO_PAIR_EVERY_CYCLES,
    RECHECK_ZERO_PAIR_SECONDS,
    WARM_ENRICH_EVERY_CYCLES,
)
from heartbeat import check_health
from logging_setup import get_logger, setup_logging
from pipeline import StopController, run_once, should_fetch_lists
from store import Store


def _print_summary(summary: dict) -> None:
    print(
        f"cycle_id: {summary.get('cycle_id')}  cycle_number={summary.get('cycle_number')}  "
        f"fetch_lists={summary.get('fetch_lists')}  resumed={summary.get('resumed')}  "
        f"interrupted={summary.get('interrupted')}"
    )
    print("List endpoints:")
    print(f"  token-profiles/latest:         {summary['profiles_latest']}")
    print(f"  token-profiles/recent-updates: {summary['profiles_recent']}")
    print(f"  community-takeovers/latest:    {summary['cto_latest']}")
    print(f"Unique tokens (deduped):         {summary['unique_tokens']}")
    print("Token upserts:")
    print(f"  inserted: {summary['tokens_inserted']}")
    print(f"  updated:  {summary['tokens_updated']}")
    print(f"  skipped:  {summary['token_upsert_skipped']}")
    print(
        f"Validation errors: total={summary.get('validation_errors', 0)} "
        f"token={summary.get('validation_token_errors', 0)} "
        f"pair={summary.get('validation_pair_errors', 0)}"
    )
    print(
        f"Alerts: emitted={summary.get('alerts_emitted', 0)} "
        f"suppressed={summary.get('alerts_suppressed', 0)}"
    )
    if summary.get("sources_extra"):
        print(f"Extra sources: {summary.get('sources_extra')} tokens_upserted={summary.get('sources_tokens_upserted', 0)}")
    if summary.get("history_snapshots") or summary.get("tokens_scored"):
        print(
            f"P2: history_snapshots={summary.get('history_snapshots', 0)} "
            f"tokens_scored={summary.get('tokens_scored', 0)} "
            f"skipped_chain={summary.get('tokens_skipped_chain', 0)}"
        )
    print("Tiers queued:")
    print(
        f"  new={summary.get('tier_new', 0)} hot={summary.get('tier_hot', 0)} "
        f"warm={summary.get('tier_warm', 0)} cold={summary.get('tier_cold', 0)}"
    )
    print("Enrichment:")
    print(f"  candidates (queue):     {summary['enrich_candidates']}")
    print(f"  skipped (has_pairs=0):  {summary['enrich_skipped_no_pairs']}")
    print(f"  skipped (not due):      {summary.get('enrich_skipped_not_due', 0)}")
    print(f"  batches sent:           {summary['enrich_batches']}")
    print(f"  batch sizes:            {summary['enrich_batch_sizes']}")
    print(f"  async concurrency:      {summary.get('enrich_async_concurrency')}")
    print(f"  async peak in-flight:   {summary.get('enrich_async_peak_in_flight', 0)}")
    print(f"  pairs fetched:          {summary['pairs_fetched']}")
    print(f"  pairs inserted/updated: {summary['pairs_inserted']}/{summary['pairs_updated']}")
    print(f"  marked has_pairs=1:     {summary['tokens_marked_has_pairs']}")
    print(f"  marked has_pairs=0:     {summary['tokens_marked_no_pairs']}")
    print(f"  pending remaining:      {summary['enrich_pending_remaining']}")
    print("DB totals:")
    print(
        f"  tokens: {summary['tokens_total']}  (has_pairs=0: {summary['tokens_has_pairs_0']}, "
        f"has_pairs=1: {summary['tokens_has_pairs_1']}, unenriched: {summary['tokens_unenriched']})"
    )
    print(f"  pairs:  {summary['pairs_total']}")
    if summary.get("counters"):
        print(f"Counters: {summary['counters']}")
    if summary["enrich_batch_errors"]:
        print("Batch errors (cycle continued):")
        for err in summary["enrich_batch_errors"]:
            print(f"  - {err}")
    if summary["errors"]:
        print("Errors:")
        for err in summary["errors"]:
            print(f"  - {err}")


def cmd_health(stale_sec: int | None = None) -> int:
    ok, msg, hb = check_health(stale_sec=stale_sec)
    print(msg)
    if hb:
        print(
            f"  timestamp={hb.get('timestamp')} status={hb.get('status')} "
            f"pid={hb.get('pid')} cycle_id={hb.get('cycle_id')}"
        )
        if hb.get("counters"):
            print(f"  counters={hb['counters']}")
        if hb.get("last_error"):
            print(f"  last_error={hb['last_error']}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DexScreener crawler Phase 5 + P1.2/P1.3 — API / proxies / multi-source"
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Check logs/heartbeat.json freshness and exit 0/1",
    )
    parser.add_argument(
        "--health-stale-sec",
        type=int,
        default=None,
        help=f"Override stale window for --health (default {HEARTBEAT_STALE_SEC})",
    )
    parser.add_argument("--flush-pending", action="store_true")
    parser.add_argument(
        "--loop",
        action="store_true",
        help=f"Run cycles repeatedly; list poll gated by PROFILE_POLL_INTERVAL_SEC "
        f"({PROFILE_POLL_INTERVAL_SEC}s)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f"Seconds between --loop ticks (default {POLL_INTERVAL_SECONDS})",
    )
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--force-new", action="store_true")
    parser.add_argument("--force-lists", action="store_true")
    parser.add_argument("--skip-lists", action="store_true")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument(
        "--sources",
        default="",
        help="Comma extra sources after primary (e.g. helius,x,solana_stub). dexscreener is always primary in pipeline.",
    )
    parser.add_argument(
        "--helius-listen",
        action="store_true",
        help=(
            "Start Helius logsSubscribe background listener (requires HELIUS_API_KEY). "
            "Enqueues solana mints into pending_enrich; use with --loop for enrich cycles."
        ),
    )
    args = parser.parse_args(argv)

    setup_logging()
    log = get_logger("main")

    if args.health:
        stale = args.health_stale_sec
        if stale is None and args.interval:
            # Prefer max(config, 2× interval) when interval known
            stale = max(HEARTBEAT_STALE_SEC, int(args.interval) * 2)
        return cmd_health(stale)

    interval = max(1, int(args.interval))
    if args.loop and interval < PROFILE_POLL_INTERVAL_SEC:
        log.warning(
            "tick interval=%ss < profile poll %ss; lists will not run every tick",
            interval,
            PROFILE_POLL_INTERVAL_SEC,
        )

    if args.force_lists and args.skip_lists:
        log.error("cannot combine --force-lists and --skip-lists")
        return 2

    stopper = StopController()

    def _on_signal(signum: int, _frame: object) -> None:
        log.warning("signal %s received — will stop gracefully", signum)
        stopper.request_stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    print("DexScreener crawler Phase 5")
    print(f"DB: {args.db}")
    print(
        f"Batch={BATCH_SIZE} async_concurrency={ASYNC_MAX_CONCURRENCY} "
        f"flush_pending={args.flush_pending} loop={args.loop}"
    )
    print(
        f"Alerts: price>={ALERT_PRICE_CHANGE_PCT}% vol>={ALERT_VOLUME_H24_CHANGE_PCT}% "
        f"liq_drop>={ALERT_LIQUIDITY_DROP_PCT}% cooldown={ALERT_COOLDOWN_SEC}s "
        f"webhook={'set' if ALERT_WEBHOOK_URL else 'off'}"
    )
    print(
        f"Tiers: warm_every={WARM_ENRICH_EVERY_CYCLES}; "
        f"zero_recheck_every={RECHECK_ZERO_PAIR_EVERY_CYCLES}/{RECHECK_ZERO_PAIR_SECONDS}s; "
        f"profile_poll>={PROFILE_POLL_INTERVAL_SEC}s"
    )
    print("---")

    helius_listener = None
    if args.helius_listen:
        from sources.solana_helius import helius_enabled, start_helius_listener_background

        print(
            f"Helius listen: requested=yes api_key={'set' if helius_enabled() else 'MISSING'}"
        )
        helius_listener = start_helius_listener_background(
            db_path=args.db,
            stop_flag=stopper.should_stop,
        )

    exit_code = 0
    cycles_done = 0
    while True:
        if stopper.should_stop():
            log.info("stop before cycle start")
            break

        if args.skip_lists:
            fetch_lists: bool | None = False
        elif args.force_lists:
            fetch_lists = True
        else:
            fetch_lists = None

        with Store(args.db) as st:
            will_lists = (
                True
                if fetch_lists is True
                else False
                if fetch_lists is False
                else should_fetch_lists(st)
            )
            log.info(
                "tick cycle_number=%s will_fetch_lists=%s pending=%s",
                st.get_cycle_number() + 1,
                will_lists,
                st.count_pending_enrich(),
            )

        extra = [s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else []
        summary = run_once(
            db_path=args.db,
            flush_pending=args.flush_pending,
            stop_flag=stopper.should_stop,
            force_new=args.force_new,
            fetch_lists=fetch_lists,
            extra_sources=extra or None,
        )
        _print_summary(summary)
        cycles_done += 1

        if summary.get("interrupted"):
            print("---")
            print("Cycle interrupted; checkpoint + heartbeat saved.")
            exit_code = 130
            break

        if summary["errors"] and summary["tokens_total"] == 0:
            print("---")
            print("Cycle finished with fatal-ish errors (heartbeat status=error/degraded).")
            exit_code = 1
            if not args.loop:
                break
        else:
            print("---")
            print("Cycle completed successfully.")
            exit_code = 0

        if not args.loop:
            break

        args.force_new = False
        args.force_lists = False

        if args.max_cycles and cycles_done >= args.max_cycles:
            log.info("reached --max-cycles=%s", args.max_cycles)
            break

        log.info("sleeping %ss before next tick", interval)
        end = time.monotonic() + interval
        while time.monotonic() < end:
            if stopper.should_stop():
                break
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

        if stopper.should_stop():
            log.info("stop during interval sleep")
            break

    # Keep process alive for listener when --helius-listen without --loop
    if helius_listener is not None and not args.loop and not stopper.should_stop():
        log.info("helius-listen keep-alive until SIGINT/SIGTERM")
        while not stopper.should_stop():
            time.sleep(0.5)

    if helius_listener is not None:
        helius_listener.stop()
        log.info("helius listener stopped")

    log.info("exit code=%s cycles_done=%s", exit_code, cycles_done)
    logging.shutdown()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
