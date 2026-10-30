"""
CLI for execution management Step A/B/C.

  python -m execution.run --once
  python -m execution.run --loop
  python -m execution.run --once --paper-enabled --paper-db /tmp/paper_smoke.db
  python -m execution.run --loop --paper-enabled --paper-tick
  # Step C PM_* env; default PM_ENABLED=0 (pass-through)
  # Peer paper (isolated ledger + accept-purpose; never share main execution_ledger.db):
  #   python -m execution.run --loop --paper-enabled --paper-tick \
  #     --paper-db paper_eq_rsi_bounce.db \
  #     --ledger execution_ledger_race_rsi_bounce.db \
  #     --accept-purpose race_rsi_bounce
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


def _parse_accept_purposes(
    cli_values: list[str] | None,
) -> frozenset[str] | None:
    """CLI ``--accept-purpose`` (repeatable) or env ``EXECUTION_ACCEPT_PURPOSE``.

    Returns None when unset → main-book default (live_candidate only).
    A non-empty frozenset enables peer allowlist mode for this process.
    """
    values: list[str] = []
    if cli_values:
        values.extend(cli_values)
    else:
        env = os.environ.get("EXECUTION_ACCEPT_PURPOSE", "").strip()
        if env:
            values.extend(env.split(","))
    cleaned = [v.strip() for v in values if v and str(v).strip()]
    if not cleaned:
        return None
    return frozenset(cleaned)


def _default_main_ledger_path() -> Path:
    """Path of the shared main execution_ledger.db (no env / explicit override)."""
    try:
        from decision_engine.config import DB_PATH as _DE_DB

        root = Path(_DE_DB).resolve().parent
    except Exception:  # noqa: BLE001
        root = _ROOT
    return (root / "execution_ledger.db").resolve()


def _peer_isolation_guard(
    *,
    accept_purposes: frozenset[str] | None,
    ledger_path: Path,
    paper_db: str | Path | None,
    paper_enabled: bool,
) -> str | None:
    """When accept-purpose is set, require isolated ledger + non-main paper DB.

    Prevents race_*/shadow_* fills from colliding on execution_ledger.db or
    polluting paper_trading.db (the baseline peer book).
    """
    if accept_purposes is None:
        return None
    if not accept_purposes:
        return "EXECUTION_ACCEPT_PURPOSE / --accept-purpose resolved empty"
    main_ledger = _default_main_ledger_path()
    if ledger_path.resolve() == main_ledger:
        return (
            "peer mode requires isolated --ledger / EXECUTION_LEDGER_PATH "
            f"(not shared {main_ledger.name})"
        )
    if not paper_enabled:
        return "peer mode (--accept-purpose) requires --paper-enabled"
    if not paper_db or not str(paper_db).strip():
        return "peer mode requires --paper-db / EXECUTION_PAPER_DB"
    paper_path = Path(str(paper_db)).expanduser()
    if not paper_path.is_absolute():
        paper_path = (Path.cwd() / paper_path).resolve()
    else:
        paper_path = paper_path.resolve()
    if paper_path.name == "paper_trading.db":
        return (
            "peer mode must not target paper_trading.db "
            "(use paper_eq_*.db); race_*/shadow_* must not pollute main book"
        )
    return None


def _shadow_isolation_guard(explicit_db, *, peer_mode: bool = False) -> str | None:
    """Main book must read dexscreener.db and never a shadow_* signal store.

    Set EXECUTION_ALLOW_NONSTANDARD_DECISION_DB=1 to skip the filename check
    (tests / smoke runs on temp DBs). The shadow_* purpose *row* check applies
    to the main book; peer mode (accept-purpose set) skips it because the
    purpose gate filters per process and peers may consume shadow_entry_*.
    """
    import sqlite3

    from execution.gateway import resolve_decision_db

    path = resolve_decision_db(explicit_db)
    allow = os.environ.get("EXECUTION_ALLOW_NONSTANDARD_DECISION_DB", "").strip() in (
        "1",
        "true",
        "yes",
    )
    if path.name != "dexscreener.db" and not allow:
        return f"decision db {path} is not dexscreener.db"
    if peer_mode:
        return None
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
        "--ledger-db",
        dest="ledger",
        default=None,
        help=(
            "execution ledger sqlite path "
            "(default EXECUTION_LEDGER_PATH / EXECUTION_LEDGER_DB or next to main DB). "
            "Peer mode must pass an isolated path, not execution_ledger.db."
        ),
    )
    parser.add_argument(
        "--accept-purpose",
        action="append",
        default=None,
        dest="accept_purpose",
        help=(
            "peer mode: decision_log.purpose allowlist for THIS process only "
            "(repeatable). Env EXECUTION_ACCEPT_PURPOSE (comma-separated) when "
            "flag omitted. Unset → main book live_candidate-only gate. "
            "Requires isolated --ledger and --paper-db not paper_trading.db."
        ),
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
    accept_purposes = _parse_accept_purposes(args.accept_purpose)
    peer_mode = accept_purposes is not None

    if paper_enabled and not args.paper_db:
        log.error("--paper-enabled requires --paper-db or EXECUTION_PAPER_DB / PAPER_DB_PATH")
        return 2

    peer_err = _peer_isolation_guard(
        accept_purposes=accept_purposes,
        ledger_path=ledger_path,
        paper_db=args.paper_db,
        paper_enabled=paper_enabled,
    )
    if peer_err:
        log.error("refusing to start: %s", peer_err)
        return 2

    log.info(
        "execution start once=%s interval=%s ledger=%s since_id=%s paper_enabled=%s paper_db=%s paper_tick=%s pm_enabled=%s accept_purposes=%s",
        run_once,
        interval,
        ledger_path,
        since_override,
        paper_enabled,
        args.paper_db if paper_enabled else None,
        paper_tick,
        pm_enabled,
        sorted(accept_purposes) if accept_purposes is not None else None,
    )

    guard_err = _shadow_isolation_guard(args.db, peer_mode=peer_mode)
    if guard_err:
        log.error("refusing to start: %s", guard_err)
        return 2

    with ExecutionGateway(
        decision_db=args.db,
        ledger_path=args.ledger,
        paper_enabled=paper_enabled,
        paper_db=args.paper_db if (paper_enabled or pm_enabled) else None,
        pm_enabled=pm_enabled,
        accept_purposes=accept_purposes,
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
                "accept_purposes": (
                    sorted(accept_purposes) if accept_purposes is not None else None
                ),
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
