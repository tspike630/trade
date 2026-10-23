"""
CLI: decision_engine Step 2 backtest framework.

  python -m decision_engine.backtest.run --strategy all --days 7 --dry-run
  python -m decision_engine.backtest.run --strategy a --days 1 --bar-minutes 10
  python -m decision_engine.backtest.run --strategy jev_strength_v1,tech_momentum_v1 --no-filter
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

from decision_engine import config as de_config  # noqa: E402
from decision_engine.backtest.engine import BacktestEngine  # noqa: E402
from decision_engine.backtest.filters import FilterConfig  # noqa: E402
from decision_engine.backtest.ledger import BacktestLedger  # noqa: E402
from decision_engine.backtest.metrics import format_metrics_table  # noqa: E402
from decision_engine.backtest.replay import HistoryReplay  # noqa: E402
from decision_engine.strategies import (  # noqa: E402
    STRATEGY_ALIASES,
    get_strategy,
    list_strategies,
    resolve_strategy_id,
)

log = get_logger("decision_engine.backtest.run")

DEFAULT_LEDGER = _ROOT / "backtest_ledger.db"


def _parse_strategy_list(raw: str | None, use_all: bool) -> list[str]:
    if use_all or (raw or "").strip().lower() in ("all", "*"):
        return list_strategies()
    if not raw:
        return ["jev_strength_v1", "tech_momentum_v1", "combined_jev_tech_v1"]
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    out: list[str] = []
    for p in parts:
        if p.lower() in ("all", "*"):
            return list_strategies()
        out.append(resolve_strategy_id(p))
    # dedupe preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def build_strategies(ids: list[str], args: argparse.Namespace) -> list:
    out = []
    for sid in ids:
        kwargs: dict = {"position_pct": args.position_pct}
        # factor_* twins receive the same CLI thresholds as the legacy id.
        if sid in ("jev_strength_v1", "factor_jev_strength_v1"):
            kwargs["min_jev_strength"] = args.min_jev_strength
        elif sid in ("tech_momentum_v1", "factor_tech_momentum_v1"):
            kwargs["min_change_5m"] = args.min_change_5m
            kwargs["min_volume_strength"] = args.min_volume_strength
        elif sid in ("combined_jev_tech_v1", "factor_combined_jev_tech_v1"):
            kwargs["min_jev_strength"] = args.min_jev_strength
            kwargs["min_change_5m"] = args.min_change_5m
            kwargs["min_volume_strength"] = args.min_volume_strength
        out.append(get_strategy(sid, **kwargs))
    return out


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    # Quiet per-bar strategy INFO spam during replay (engine still logs summary)
    for name in (
        "dexscreener.decision_engine.jev_strategy",
        "dexscreener.decision_engine.tech_strategy",
        "dexscreener.decision_engine.combined_strategy",
        "dexscreener.decision_engine.entry_sizing",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
    known = ", ".join(list_strategies())
    aliases = ", ".join(f"{k}→{v}" for k, v in sorted(STRATEGY_ALIASES.items()))
    parser = argparse.ArgumentParser(
        description="Decision engine Step 2 backtest (replay + ledger + metrics)"
    )
    parser.add_argument(
        "--strategy",
        default="all",
        help=f"strategy id(s), comma-separated, or 'all'. Known: {known}. Aliases: {aliases}",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="same as --strategy all",
    )
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="lookback days from latest indicator_history (default 7)",
    )
    parser.add_argument(
        "--bar-minutes",
        type=int,
        default=5,
        help="snapshot bar size in minutes (default 5)",
    )
    parser.add_argument(
        "--db",
        default=de_config.DB_PATH,
        help="read-only source DB (indicator_history / pair_history)",
    )
    parser.add_argument(
        "--ledger",
        default=str(DEFAULT_LEDGER),
        help="independent backtest ledger sqlite path",
    )
    parser.add_argument(
        "--start-cash",
        type=float,
        default=10_000.0,
        help="starting cash per strategy (default 10000)",
    )
    parser.add_argument(
        "--position-usd",
        type=float,
        default=None,
        help="fixed notional per entry (default: size_pct * cash)",
    )
    parser.add_argument("--position-pct", type=float, default=0.05)
    parser.add_argument("--tp", type=float, default=10.0, help="take-profit %%")
    parser.add_argument("--sl", type=float, default=5.0, help="stop-loss %%")
    parser.add_argument("--max-hold-min", type=float, default=60.0)
    parser.add_argument("--max-open", type=int, default=5)
    parser.add_argument("--hot-limit", type=int, default=50)
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="framework dry-run flag (always offline; default on)",
    )
    # filters (weekly-check aligned; default ON)
    parser.add_argument(
        "--filter",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="entry filters liquidity/coverage/min_price (default on)",
    )
    parser.add_argument("--min-liq", type=float, default=50_000.0)
    parser.add_argument("--min-coverage", type=float, default=10.0)
    parser.add_argument("--min-price", type=float, default=1e-6)
    parser.add_argument(
        "--strict-coverage",
        action="store_true",
        help="fail closed when coverage missing (default: fail-open)",
    )
    # strategy thresholds — default matches live JEV_MIN_STRENGTH (not the 0.15 sample).
    # Weekly / exploratory sampling must pass --min-jev-strength 0.15 explicitly.
    parser.add_argument(
        "--min-jev-strength",
        type=float,
        default=de_config.JEV_MIN_STRENGTH,
        help=(
            "JEV buy threshold for jev/combined strategies "
            f"(default {de_config.JEV_MIN_STRENGTH} from JEV_MIN_STRENGTH / "
            "DECISION_JEV_MIN_STRENGTH, same as live). "
            "Weekly sampling that wants 0.15 must pass --min-jev-strength 0.15 explicitly."
        ),
    )
    parser.add_argument("--min-change-5m", type=float, default=2.0)
    parser.add_argument("--min-volume-strength", type=float, default=0.5)
    parser.add_argument(
        "--json-out",
        default=None,
        help="optional path to write full result JSON",
    )
    parser.add_argument(
        "--min-bars",
        type=int,
        default=3,
        help="minimum approx bars required (else graceful exit)",
    )
    parser.add_argument(
        "--min-indicator-rows",
        type=int,
        default=10,
        help="minimum indicator_history rows in window",
    )

    args = parser.parse_args(argv)
    ids = _parse_strategy_list(args.strategy, args.all)
    if not ids:
        log.error("no strategies selected")
        return 2

    log.info(
        "backtest start strategies=%s days=%s bar=%sm filter=%s dry_run=%s db=%s",
        ids,
        args.days,
        args.bar_minutes,
        args.filter,
        args.dry_run,
        args.db,
    )

    fcfg = FilterConfig(
        min_liq=args.min_liq,
        min_coverage=args.min_coverage,
        min_price=args.min_price,
        enabled=bool(args.filter),
        coverage_fail_open=not args.strict_coverage,
    )

    strategies = build_strategies(ids, args)

    with HistoryReplay(
        args.db,
        days=args.days,
        bar_minutes=args.bar_minutes,
        hot_limit=args.hot_limit,
        min_bars=args.min_bars,
        min_indicator_rows=args.min_indicator_rows,
    ) as replay, BacktestLedger(
        args.ledger, start_cash=args.start_cash
    ) as ledger:
        engine = BacktestEngine(
            replay,
            ledger,
            strategies,
            filters=fcfg,
            start_cash=args.start_cash,
            position_usd=args.position_usd,
            tp_pct=args.tp,
            sl_pct=args.sl,
            max_hold_min=args.max_hold_min,
            max_open_per_strategy=args.max_open,
            dry_run=args.dry_run,
        )
        result = engine.run()

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        log.info("wrote json_out=%s", out_path)

    if not result.get("ok"):
        print("BACKTEST EXIT: insufficient data (graceful)")
        print(f"  reason: {result.get('reason')}")
        print(f"  message: {result.get('message')}")
        w = result.get("window") or {}
        if w:
            print(
                f"  window: {w.get('start')} → {w.get('end')} "
                f"ind={w.get('indicator_rows')} pairs={w.get('pair_rows')} "
                f"tokens={w.get('unique_tokens')} bars≈{w.get('unique_bars')}"
            )
        print(
            "  hint: wait for more indicator_history / pair_history, "
            "or lower --min-bars / --min-indicator-rows / --days"
        )
        return 0  # graceful success for smoke

    print("BACKTEST OK")
    print(f"  run_id: {result.get('run_id')}")
    print(f"  ledger: {result.get('ledger_path')}")
    print(f"  message: {result.get('message')}")
    w = result.get("window") or {}
    print(
        f"  window: {w.get('start')} → {w.get('end')} "
        f"ind={w.get('indicator_rows')} pairs={w.get('pair_rows')} "
        f"tokens={w.get('unique_tokens')}"
    )
    st = result.get("stats") or {}
    print(
        f"  stats: bars={st.get('bars')} signals={st.get('signals')} "
        f"filtered={st.get('filtered_out')} opens={st.get('opens')} "
        f"closes={st.get('closes')}"
    )
    by = result.get("by_strategy") or {}
    print()
    print(format_metrics_table(by))
    print()
    print(
        "Note: Step 2 delivers the FRAMEWORK. Strategy edge / JEV validity "
        "conclusions remain deferred to weekly check — do not treat these "
        "numbers as production readiness."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
