"""
CLI entry for decision_engine Step 1 (dry-run by default).

  python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once
  python -m decision_engine.run --strategy jev_strength_v1 --dry-run --ticks 3 --interval 5
  python -m decision_engine.run --all --dry-run --once

Resident loop / --once write purpose=live_candidate (paper-eligible dry-run).
Weekly sampling (WEEKLY_CHECK, threshold 0.15) writes purpose=sample:

  python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once \\
      --min-jev-strength 0.15 --purpose sample

mode stays dry_run either way. Passing --min-jev-strength below the live
default also selects sample when --purpose is omitted.
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
from decision_engine.core.signal_bus import (  # noqa: E402
    PURPOSE_LIVE_CANDIDATE,
    PURPOSE_SAMPLE,
    SignalBus,
    resolve_run_purpose,
)
from decision_engine.strategies import get_strategy, list_strategies  # noqa: E402

log = get_logger("decision_engine.run")


# --- Opt-in shadow cohort path (default behavior unchanged) -----------------
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _read_budget_regime() -> str:
    """Read-only lookup of X_HOURLY_BUDGET_REQUESTS (process env, else crawler .env)."""
    import os

    val = os.environ.get("X_HOURLY_BUDGET_REQUESTS")
    if val is None:
        try:
            for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith("X_HOURLY_BUDGET_REQUESTS="):
                    val = line.split("=", 1)[1].split("#", 1)[0].strip().strip("'\"")
        except OSError:
            val = None
    if val in (None, "", "20"):
        return "20h"
    if val == "4":
        return "4h"
    return f"{val}h"


class ShadowCohortRisk(RiskManager):
    """Tag liq_bucket/budget_regime; let missing-liquidity buys through as control."""

    def check(self, signal):  # type: ignore[override]
        sig = dict(signal)
        if str(sig.get("action") or "").lower() == "buy":
            liq = sig.get("liquidity_usd")
            try:
                missing = liq is None or float(liq) != float(liq)
            except (TypeError, ValueError):
                missing = True
            sig["liq_bucket"] = "missing" if missing else "present"
            sig["budget_regime"] = _read_budget_regime()
            if missing:
                # bypass only the no_liquidity fail-closed check (cooldown still applies)
                sig["liquidity_usd"] = float("inf")
                out = super().check(sig)
                out["liquidity_usd"] = None
                return out
        return super().check(sig)


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
        # factor_* twins take the same CLI thresholds as the legacy id.
        # Default strategy stays jev_strength_v1; this only applies when the
        # caller names the strategy (or --all).
        if sid in ("jev_strength_v1", "factor_jev_strength_v1"):
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
        default=None,
        help=(
            f"JEV strategy threshold (default {de_config.JEV_MIN_STRENGTH}). "
            "A value below that default is the weekly sampling path and "
            f"sets purpose={PURPOSE_SAMPLE} unless --purpose is set."
        ),
    )
    parser.add_argument(
        "--purpose",
        choices=(PURPOSE_LIVE_CANDIDATE, PURPOSE_SAMPLE),
        default=None,
        help=(
            "Why this run emits decision_log rows. "
            f"{PURPOSE_LIVE_CANDIDATE} (default) is the resident dry-run / "
            "paper-eligible path. "
            f"{PURPOSE_SAMPLE} is the weekly Mon/Wed/Fri check and is not "
            "paper-eligible. Does not change mode (dry_run stays dry_run)."
        ),
    )
    parser.add_argument(
        "--output-db",
        default=None,
        help="opt-in: write decision_log to this SQLite file instead of --db "
        "(indicators still read from --db). Default: same as --db.",
    )
    parser.add_argument(
        "--shadow-liq-cohorts",
        action="store_true",
        help="opt-in shadow: tag buys with liq_bucket (present/missing) and "
        "budget_regime; missing-liquidity buys are kept as control cohort. "
        "Requires --output-db different from --db.",
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

    db_path = str(Path(args.db or de_config.DB_PATH).expanduser().resolve())
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

    explicit_min = args.min_jev_strength
    if args.min_jev_strength is None:
        args.min_jev_strength = float(de_config.JEV_MIN_STRENGTH)
    try:
        purpose = resolve_run_purpose(
            args.purpose,
            min_jev_strength=explicit_min,
            live_min_strength=float(de_config.JEV_MIN_STRENGTH),
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    try:
        strategies = build_strategies(args)
    except KeyError as exc:
        log.error("%s", exc)
        return 2

    log.info(
        "decision_engine start db=%s dry_run=%s purpose=%s strategies=%s "
        "interval=%s once=%s ticks=%s min_jev_strength=%s min_liq_usd=%s",
        db_path,
        dry_run,
        purpose,
        [s.strategy_id for s in strategies],
        interval,
        args.once,
        args.ticks,
        args.min_jev_strength,
        de_config.DECISION_MIN_LIQ_USD,
    )

    out_db = str(Path(args.output_db).expanduser().resolve()) if args.output_db else db_path
    if args.shadow_liq_cohorts and out_db == db_path:
        log.error("--shadow-liq-cohorts requires --output-db != --db (isolation)")
        return 2
    risk = ShadowCohortRisk() if args.shadow_liq_cohorts else RiskManager()
    log.info("output_db=%s shadow_liq_cohorts=%s", out_db, args.shadow_liq_cohorts)
    adapter = IndicatorDataAdapter(db_path)
    bus = SignalBus(out_db, dry_run=dry_run, purpose=purpose)
    try:
        engine = DecisionEngine(
            adapter,
            bus,
            strategies,
            risk=risk,
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
