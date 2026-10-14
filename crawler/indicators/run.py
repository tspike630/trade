"""
CLI entry for the indicators 旁路 process (Step 1–3: technical + funding + sentiment).

  python -m indicators.run --full
  python -m indicators.run --loop --interval 300
  python -m indicators.run --once          # due-only pass (tier intervals)

Technical due-only passes refresh hot tokens whose tech_updated_at is NULL or
older than the hot interval, then warm tokens past the warm interval, before
the cold tail. The hot-then-warm queue is drained again each hot interval
while that tail is still running.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow `python indicators/run.py` from repo root
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import (  # noqa: E402
    DB_PATH,
    INDICATORS_COLD_INTERVAL_SEC,
    INDICATORS_ENABLED,
    INDICATORS_HISTORY_RETENTION_DAYS,
    INDICATORS_HOT_INTERVAL_SEC,
    INDICATORS_LOOP_INTERVAL_SEC,
    INDICATORS_VOLATILITY_WINDOW_SEC,
    INDICATORS_WARM_INTERVAL_SEC,
    INDICATORS_WRITE_HISTORY_HOT,
)
from logging_setup import get_logger, setup_logging  # noqa: E402

from indicators.base import IndicatorStore  # noqa: E402
from indicators.composite import CompositeIndicators  # noqa: E402
from indicators.funding import FundingIndicators  # noqa: E402
from indicators.sentiment import SentimentIndicators  # noqa: E402
from indicators.technical import TechnicalIndicators  # noqa: E402

log = get_logger("indicators.run")


def run_pass(
    store: IndicatorStore,
    *,
    force_full: bool = False,
    only_due: bool = True,
) -> dict[str, int]:
    tech = TechnicalIndicators(
        store,
        vol_window_sec=INDICATORS_VOLATILITY_WINDOW_SEC,
        write_history_for_hot=INDICATORS_WRITE_HISTORY_HOT,
    )
    # Due hot, then due warm, then the cold tail. While the tail runs, the
    # hot-then-warm queue is drained again each hot interval.
    stats = tech.run_batch(
        force_full=force_full,
        only_due=only_due and not force_full,
        hot_interval_sec=INDICATORS_HOT_INTERVAL_SEC,
        warm_interval_sec=INDICATORS_WARM_INTERVAL_SEC,
        cold_interval_sec=INDICATORS_COLD_INTERVAL_SEC,
    )
    # Funding (JEV) — READ jev_signals only; isolated from technical failures
    try:
        fstats = FundingIndicators(
            store, write_history_for_hot=INDICATORS_WRITE_HISTORY_HOT
        ).run_batch(force_full=force_full)
        log.info("funding pass %s", fstats)
    except Exception as exc:  # noqa: BLE001
        log.warning("funding error: %s", exc)
    # Sentiment (X) — READ x_watch / social alerts only; isolated failures
    try:
        sstats = SentimentIndicators(store).run_batch(force_full=force_full)
        log.info("sentiment pass %s", sstats)
    except Exception as exc:  # noqa: BLE001
        log.warning("sentiment error: %s", exc)
    # Composite — no-op while INDICATORS_WEIGHT_* are all 0 (default)
    try:
        cstats = CompositeIndicators(store).run_batch()
        log.info("composite pass %s", cstats)
    except Exception as exc:  # noqa: BLE001
        log.warning("composite error: %s", exc)
    # Optional on_indicator hook (default OFF) — never raises into crawl
    try:
        from config import INDICATORS_ON_UPDATE_HOOK
        if INDICATORS_ON_UPDATE_HOOK:
            from auto_hooks import maybe_on_indicator

            # Sample: only log that hook path is live; skip per-token fan-out
            maybe_on_indicator("_batch", "_pass", {"tier": "batch", "composite_score": None})
    except Exception as exc:  # noqa: BLE001
        log.debug("on_indicator hook skipped: %s", exc)
    return stats


def _purge_indicator_history(store: IndicatorStore) -> None:
    """Drop expired indicator_history. Never raises into the loop."""
    try:
        removed = store.purge_old_indicator_history(INDICATORS_HISTORY_RETENTION_DAYS)
        if removed:
            log.info(
                "indicator_history purge removed=%s retention_days=%s",
                removed,
                INDICATORS_HISTORY_RETENTION_DAYS,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("indicator_history purge skipped: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DexScreener indicators 旁路 (Step 1–3)")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Recompute full technical set for all tokens with history",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single due-only pass (respect tier intervals)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Loop forever, due-only passes",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=f"Loop sleep seconds (default {INDICATORS_LOOP_INTERVAL_SEC})",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"SQLite path (default {DB_PATH})",
    )
    parser.add_argument(
        "--force-enabled",
        action="store_true",
        help="Run even if INDICATORS_ENABLED=0",
    )
    args = parser.parse_args(argv)

    setup_logging()
    # --full is an explicit operator action (always allowed).
    if not INDICATORS_ENABLED and not args.force_enabled and not args.full:
        log.warning("INDICATORS_ENABLED=0; pass --force-enabled or --full to run")
        return 2

    if not args.full and not args.once and not args.loop:
        parser.error("specify --full, --once, or --loop")

    db_path = args.db or DB_PATH
    interval = args.interval if args.interval is not None else INDICATORS_LOOP_INTERVAL_SEC

    with IndicatorStore(db_path) as store:
        if args.full:
            log.info("indicators --full start db=%s", db_path)
            stats = run_pass(store, force_full=True, only_due=False)
            log.info(
                "indicators --full done ok=%s skip=%s err=%s seen=%s "
                "hot_due=%s warm_due=%s rows=%s",
                stats["ok"],
                stats["skip"],
                stats["err"],
                stats["seen"],
                stats.get("hot_due", 0),
                stats.get("warm_due", 0),
                store.count_token_indicators(),
            )
            return 0 if stats["err"] == 0 or stats["ok"] > 0 else 1

        if args.once:
            stats = run_pass(store, force_full=False, only_due=True)
            log.info("indicators --once %s", stats)
            return 0

        # loop
        log.info(
            "indicators --loop interval=%ss db=%s",
            interval,
            db_path,
        )
        while True:
            try:
                stats = run_pass(store, force_full=False, only_due=True)
                log.info(
                    "indicators pass ok=%s skip=%s err=%s seen=%s "
                    "hot_due=%s warm_due=%s",
                    stats["ok"],
                    stats["skip"],
                    stats["err"],
                    stats["seen"],
                    stats.get("hot_due", 0),
                    stats.get("warm_due", 0),
                )
                _purge_indicator_history(store)
            except Exception as exc:  # noqa: BLE001 — never kill outer process silently
                log.exception("indicators pass crashed: %s", exc)
            time.sleep(max(5, int(interval)))


if __name__ == "__main__":
    raise SystemExit(main())
