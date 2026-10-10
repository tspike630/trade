"""
CLI entry for decision_engine Step 1 (dry-run by default).

  python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once
  python -m decision_engine.run --strategy jev_strength_v1 --dry-run --ticks 3 --interval 5
  python -m decision_engine.run --all --dry-run --once
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

from decision_engine import config as de_config  # noqa: E402
from decision_engine.core.data_adapter import IndicatorDataAdapter  # noqa: E402
from decision_engine.core.engine import DecisionEngine  # noqa: E402
from decision_engine.core.risk_manager import RiskManager  # noqa: E402
from decision_engine.core.signal_bus import SignalBus  # noqa: E402
from decision_engine.strategies import get_strategy, list_strategies  # noqa: E402

log = get_logger("decision_engine.run")


def build_strategies(args: argparse.Namespace) -> list:
    ids: list[str]
    if args.all:
        ids = list_strategies()
    elif args.strategy:
        ids = [args.strategy]
    else:
        ids = [de_config.DEFAULT_STRATEGY]

    out = []
    for sid in ids:
        if sid == "jev_strength_v1":
            out.append(
                get_strategy(
                    sid,
                    min_jev_strength=args.min_jev_strength,
                    position_pct=args.position_pct,
                )
            )
        else:
            out.append(get_strategy(sid))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decision engine Step 1 (dry-run signals; no execution)"
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help=f"strategy_id (default {de_config.DEFAULT_STRATEGY}); known: {', '.join(list_strategies())}",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="run all registered strategies",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="dry-run mode (default: on). --no-dry-run still does not place live orders in Step1",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="single tick then exit",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="run N ticks then exit",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"seconds between ticks (default {de_config.TICK_INTERVAL_SEC})",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"SQLite path (default {de_config.DB_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"hot token limit (default {de_config.HOT_TOKEN_LIMIT})",
    )
    parser.add_argument(
        "--min-jev-strength",
        type=float,
        default=de_config.JEV_MIN_STRENGTH,
        help=f"JEV strategy threshold (default {de_config.JEV_MIN_STRENGTH})",
    )
    parser.add_argument(
        "--position-pct",
        type=float,
        default=de_config.JEV_POSITION_PCT,
        help=f"JEV buy size_pct (default {de_config.JEV_POSITION_PCT})",
    )
    args = parser.parse_args(argv)

    setup_logging()

    if not args.once and args.ticks is None:
        # Continuous loop is allowed; document that --once/--ticks preferred for verify
        log.info("no --once/--ticks: will loop until interrupted")

    db_path = args.db or de_config.DB_PATH
    interval = (
        args.interval if args.interval is not None else float(de_config.TICK_INTERVAL_SEC)
    )
    hot_limit = args.limit if args.limit is not None else de_config.HOT_TOKEN_LIMIT
    dry_run = bool(args.dry_run)

    if not dry_run:
        log.warning(
            "--no-dry-run requested but Step1 has no execution layer; "
            "signals still only logged to decision_log (mode=live_stub)"
        )

    try:
        strategies = build_strategies(args)
    except KeyError as exc:
        log.error("%s", exc)
        return 2

    log.info(
        "decision_engine start db=%s dry_run=%s strategies=%s interval=%s once=%s ticks=%s",
        db_path,
        dry_run,
        [s.strategy_id for s in strategies],
        interval,
        args.once,
        args.ticks,
    )

    adapter = IndicatorDataAdapter(db_path)
    bus = SignalBus(db_path, dry_run=dry_run)
    try:
        engine = DecisionEngine(
            adapter,
            bus,
            strategies,
            risk=RiskManager(),
            hot_limit=hot_limit,
        )
        results = engine.run(
            once=args.once,
            ticks=args.ticks,
            interval_sec=interval,
        )
        total_emitted = sum(int(r.get("emitted") or 0) for r in results)
        total_hot = results[-1].get("hot_loaded") if results else 0
        total_ind = results[-1].get("indicators_total") if results else 0
        log.info(
            "decision_engine done ticks=%s indicators_total=%s last_hot=%s total_emitted=%s",
            len(results),
            total_ind,
            total_hot,
            total_emitted,
        )
        return 0
    finally:
        bus.close()
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
