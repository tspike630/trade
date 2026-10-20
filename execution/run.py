"""
CLI for execution management Step A/B/C.

  python -m execution.run --once
  python -m execution.run --loop
  python -m execution.run --once --paper-enabled --paper-db /tmp/paper_smoke.db
  python -m execution.run --loop --paper-enabled --paper-tick
  # Step C PM_* env; default PM_ENABLED=0 (pass-through)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

from execution.gateway import ExecutionGateway, _env_bool  # noqa: E402
from execution.ledger import resolve_ledger_path  # noqa: E402

log = get_logger("execution.run")

DEFAULT_POLL_SEC = float(os.environ.get("EXECUTION_POLL_SEC", "5"))


def _default_paper_db() -> str | None:
    return (
        os.environ.get("EXECUTION_PAPER_DB", "").strip()
        or os.environ.get("PAPER_DB_PATH", "").strip()
        or None
    )


def _shadow_isolation_guard(explicit_db) -> str | None:
    """Main book must read dexscreener.db and never a shadow_* signal store.

    Set EXECUTION_ALLOW_NONSTANDARD_DECISION_DB=1 to skip the filename check
    (tests / smoke runs on temp DBs). The shadow_* purpose check always applies.
    """
    import os
    import sqlite3

    from execution.gateway import resolve_decision_db

    path = resolve_decision_db(explicit_db)
    allow = os.environ.get("EXECUTION_ALLOW_NONSTANDARD_DECISION_DB", "").strip() in ("1", "true", "yes")
    if path.name != "dexscreener.db" and not allow:
        return f"decision db {path} is not dexscreener.db"
    if path.exists():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                hit = con.execute(
                    "SELECT 1 FROM decision_log WHERE purpose LIKE 'shadow\\_%' ESCAPE '\\' LIMIT 1"
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            hit = None
        if hit:
            return f"decision db {path} contains shadow_* purpose rows"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execution gateway: consume decision_log → execution_ledger; "
            "optional PaperExecutor when --paper-enabled"
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="process pending once then exit (default if neither --once nor --loop)",
    )
    mode.add_argument(
        "--loop",
        action="store_true",
        help="poll forever",
    )
    parser.add_argument(
        "--since-id",
        type=int,
        default=None,
        help=(
            "this run only: read decision_log id > N. "
            "Does not rewind the persisted cursor (cursor advances with MAX)."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"loop poll seconds (default {DEFAULT_POLL_SEC}; env EXECUTION_POLL_SEC)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="decision/main SQLite path (default decision_engine.config.DB_PATH)",
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help="execution_ledger.db path (default EXECUTION_LEDGER_PATH or next to main DB)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max rows per tick",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON summary line per tick",
    )
    parser.add_argument(
        "--paper-enabled",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("EXECUTION_PAPER_ENABLED", False),
        help=(
            "enable PaperExecutor on validated buy/sell "
            "(env EXECUTION_PAPER_ENABLED, default 0). "
            "Do NOT enable against production paper_trading.db without intent."
        ),
    )
    parser.add_argument(
        "--paper-db",
        default=_default_paper_db(),
        help="PaperLedger sqlite path (env EXECUTION_PAPER_DB or PAPER_DB_PATH)",
    )
    parser.add_argument(
        "--paper-tick",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("EXECUTION_PAPER_TICK", False),
        help=(
            "in --loop, also run PaperLedger TP/SL/max_hold tick each poll "
            "(env EXECUTION_PAPER_TICK, default 0; requires --paper-enabled)"
        ),
    )
    parser.add_argument(
        "--pm-enabled",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("PM_ENABLED", False),
        help=(
            "enable PositionManager buy gate (env PM_ENABLED, default 0). "
            "Rules PM_TOKEN_DEDUP / PM_STRATEGY_MUTEX / PM_MAX_* default off."
        ),
    )
    args = parser.parse_args(argv)

    setup_logging()

    run_once = args.once or not args.loop
    interval = float(args.interval) if args.interval is not None else DEFAULT_POLL_SEC
    ledger_path = resolve_ledger_path(args.ledger)
    since_override: int | None = args.since_id
    paper_enabled = bool(args.paper_enabled)
    paper_tick = bool(args.paper_tick) and paper_enabled
    pm_enabled = bool(args.pm_enabled)

    if paper_enabled and not args.paper_db:
        log.error("--paper-enabled requires --paper-db or EXECUTION_PAPER_DB / PAPER_DB_PATH")
        return 2

    log.info(
        "execution start once=%s interval=%s ledger=%s since_id=%s paper_enabled=%s paper_db=%s paper_tick=%s pm_enabled=%s",
        run_once,
        interval,
        ledger_path,
        since_override,
        paper_enabled,
        args.paper_db if paper_enabled else None,
        paper_tick,
        pm_enabled,
    )

    guard_err = _shadow_isolation_guard(args.db)
    if guard_err:
        log.error("refusing to start: %s", guard_err)
        return 2

    with ExecutionGateway(
        decision_db=args.db,
        ledger_path=args.ledger,
        paper_enabled=paper_enabled,
        paper_db=args.paper_db if (paper_enabled or pm_enabled) else None,
        pm_enabled=pm_enabled,
    ) as gw:

        def tick(since_id: int | None) -> dict:
            stats = gw.process_pending(since_id=since_id, limit=args.limit)
            tick_actions: list = []
            if paper_tick:
                paper = gw._get_paper()
                if paper is not None:
                    tick_actions = paper.tick()
            summary = {
                "processed": stats.processed,
                "validation_ok": stats.validation_ok,
                "validation_fail": stats.validation_fail,
                "pm_rejected": stats.pm_rejected,
                "executed_ok": stats.executed_ok,
                "executed_fail": stats.executed_fail,
                "last_decision_id": stats.last_decision_id,
                "ledger_path": str(gw.ledger.path),
                "cursor": gw.ledger.get_last_processed_id(),
                "paper_enabled": paper_enabled,
                "pm_enabled": pm_enabled,
                "paper_tick_closed": tick_actions,
                "sample": stats.receipts[:3] if stats.receipts else [],
            }
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, default=str))
            else:
                log.info(
                    "tick processed=%s ok=%s fail=%s pm_rej=%s executed_ok=%s cursor=%s paper=%s pm=%s tick_closed=%s",
                    stats.processed,
                    stats.validation_ok,
                    stats.validation_fail,
                    stats.pm_rejected,
                    stats.executed_ok,
                    summary["cursor"],
                    paper_enabled,
                    pm_enabled,
                    len(tick_actions),
                )
            return summary

        if run_once:
            summary = tick(since_override)
            if not args.json:
                print(
                    json.dumps(
                        {
                            "processed": summary["processed"],
                            "validation_ok": summary["validation_ok"],
                            "validation_fail": summary["validation_fail"],
                            "pm_rejected": summary["pm_rejected"],
                            "executed_ok": summary["executed_ok"],
                            "executed_fail": summary["executed_fail"],
                            "ledger_path": summary["ledger_path"],
                            "cursor": summary["cursor"],
                            "paper_enabled": summary["paper_enabled"],
                            "pm_enabled": summary["pm_enabled"],
                            "sample_receipts": summary["sample"],
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    )
                )
            return 0

        # --loop: --since-id applies to the first read only; cursor still uses MAX.
        if paper_tick:
            log.info(
                "paper tick writer: this process is the only tick source for %s "
                "(do not also run paper_tick_loop.sh)",
                args.paper_db,
            )
        first = True
        backoff = interval
        while True:
            sid = since_override if first else None
            try:
                tick(sid)
            except Exception:
                log.exception("execution loop tick failed; backing off")
                time.sleep(min(backoff, 60.0))
                backoff = min(max(backoff * 2.0, interval), 60.0)
                continue
            first = False
            backoff = interval
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
