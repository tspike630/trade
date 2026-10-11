"""CLI: python -m review.run --period 7d --report [--optimize]"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from review.data import (
    DbPaths,
    SignalChainRow,
    annotate_forward_returns,
    annotate_token_age,
    connect_ro,
    load_jev_coverage,
    load_market_baseline,
    load_signal_chain,
    summarize_execution,
    to_iso_z,
)
from review.attribution import (
    fill_attribution,
    load_backtest_summary,
    signal_attribution,
)
from review.joint import joint_jev_age_prior
from review.metrics import compute_signal_metrics, slice_jev_filtered
from review.report import render_weekly_md, write_report
from review.optimizer import (
    format_optimize_summary,
    optimize,
)
from review.evolution import build_evolution_suggestions

_ROOT = Path(__file__).resolve().parents[1]
_PERIOD_RE = re.compile(r"^(\d+)([dDhHmM])$")


def _parse_period(period: str, now: Optional[datetime] = None) -> tuple[str, str]:
    """Return (start_iso_z, end_iso_z) where end is exclusive-ish (now)."""
    m = _PERIOD_RE.match(period.strip())
    if not m:
        raise ValueError(f"invalid --period {period!r}; expected like 7d / 24h")
    n = int(m.group(1))
    unit = m.group(2).lower()
    now = now or datetime.now(timezone.utc)
    if unit == "d":
        delta = timedelta(days=n)
    elif unit == "h":
        delta = timedelta(hours=n)
    elif unit == "m":
        delta = timedelta(minutes=n)
    else:
        raise ValueError(f"invalid period unit in {period!r}")
    start = now - delta
    # end exclusive: use now + 1s so current-second rows included under ts < end
    end = now + timedelta(seconds=1)
    return to_iso_z(start), to_iso_z(end)


def _normalize_iso(s: str) -> str:
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s + "T00:00:00Z"
    if s.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", s):
        return s
    if "T" not in s:
        s = s + "T00:00:00"
    return s + "Z"


def _end_date_label(period_end: str) -> str:
    """Calendar date (UTC) for report filename from exclusive end."""
    s = period_end.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # exclusive end at 00:00 → previous day label is more natural; else use date
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            dt = dt - timedelta(seconds=1)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return period_end[:10]


def _row_to_dict(r: SignalChainRow) -> dict[str, Any]:
    d = asdict(r)
    return d



def _paths_under(db_root: Path) -> DbPaths:
    return DbPaths(
        main=str(db_root / "dexscreener.db"),
        execution=str(db_root / "execution_ledger.db"),
        paper=str(db_root / "paper_trading.db"),
        backtest=str(db_root / "backtest_ledger.db"),
    )


def _probe_dbs(paths: DbPaths) -> None:
    """Open all four DBs read-only; raise on failure (caller → exit 2)."""
    for name in (paths.main, paths.execution, paths.paper, paths.backtest):
        conn = connect_ro(name)
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="R1 review: signal-chain metrics + weekly markdown report (read-only)"
    )
    period_grp = parser.add_mutually_exclusive_group()
    period_grp.add_argument(
        "--period",
        default=None,
        help="rolling window ending now, e.g. 7d / 24h (default if no --start/--end)",
    )
    parser.add_argument("--start", default=None, help="ISO UTC inclusive")
    parser.add_argument("--end", default=None, help="ISO UTC exclusive")
    parser.add_argument("--strategy", default=None, help="filter strategy_id")
    parser.add_argument(
        "--db-root",
        default=str(_ROOT),
        help="directory containing the four DBs (default: repo root)",
    )
    parser.add_argument("--tol-sec", type=int, default=1800, help="price align tolerance")
    parser.add_argument(
        "--min-strength", type=float, default=0.15,
        help="JEV slice strength threshold (default 0.15)",
    )
    parser.add_argument(
        "--min-liq", type=float, default=50_000.0,
        help="JEV slice min liquidity USD (default 50000)",
    )
    parser.add_argument(
        "--min-coverage", type=float, default=10.0,
        help="JEV slice min coverage 0-100 (default 10)",
    )
    parser.add_argument(
        "--min-price", type=float, default=1e-6,
        help="JEV slice min price at signal (default 1e-6)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="write markdown under exports/reports/",
    )
    parser.add_argument(
        "--dump-chain",
        default=None,
        metavar="PATH",
        help="optional JSON dump of signal chain rows",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="override report path (default exports/reports/review_weekly_<end-date>.md)",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="R3 scaffold: param-grid / time-split dry-run "
        "(skip if history/samples insufficient; never writes config)",
    )
    args = parser.parse_args(argv)

    if args.start or args.end:
        if not (args.start and args.end):
            print("error: --start and --end must be used together", file=sys.stderr)
            return 2
        period_start = _normalize_iso(args.start)
        period_end = _normalize_iso(args.end)
    else:
        period = args.period or "7d"
        try:
            period_start, period_end = _parse_period(period)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    db_root = Path(args.db_root).resolve()
    paths = _paths_under(db_root)

    try:
        _probe_dbs(paths)
    except Exception as e:
        print(f"error: cannot open DB read-only: {e}", file=sys.stderr)
        return 2

    try:
        chain = load_signal_chain(
            paths,
            period_start=period_start,
            period_end=period_end,
            strategy_id=args.strategy,
        )
        chain = annotate_forward_returns(chain, paths, tol_sec=args.tol_sec)
        chain = load_jev_coverage(chain, paths)
        chain = annotate_token_age(chain, paths)
        metrics = compute_signal_metrics(chain)
        exec_summary = summarize_execution(chain)
        baseline = load_market_baseline(
            paths,
            period_start=period_start,
            period_end=period_end,
        )
        jev_rows = slice_jev_filtered(
            chain,
            min_strength=args.min_strength,
            min_liq=args.min_liq,
            min_coverage=args.min_coverage,
            min_price=args.min_price,
        )
        jev_metrics = compute_signal_metrics(jev_rows) if jev_rows else None
        if jev_metrics is not None:
            # drop nested by_strategy noise for slice table
            jev_metrics = {k: v for k, v in jev_metrics.items() if k != "by_strategy"}
        attr = signal_attribution(chain, baseline=baseline, db_root=db_root)
        fill_attr = fill_attribution(chain)
        bt_summary = load_backtest_summary(paths)
        joint = joint_jev_age_prior(chain, jev_rows, db_root=db_root)
    except FileNotFoundError as e:
        print(f"error: cannot open DB read-only: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: review failed: {e}", file=sys.stderr)
        return 1

    n = len(chain)
    n_exec = sum(1 for r in chain if r.exec_id is not None)
    print(
        f"review: period [{period_start}, {period_end}) n={n} with_exec={n_exec} "
        f"up_1h={metrics.get('up_1h_rate')} up_4h={metrics.get('up_4h_rate')} "
        f"validation_ok={metrics.get('validation_ok_rate')} "
        f"executed={metrics.get('executed_rate')}"
    )
    regime = (attr.get("dimensions") or {}).get("market_regime", {}).get("assigned")
    print(
        f"attribution: n={attr.get('n')} regime={regime} "
        f"fills={fill_attr.get('note')} "
        f"backtest={'ok' if bt_summary.get('ok') else bt_summary.get('note')}"
    )
    jev_n = (joint.get("jev_filtered") or {}).get("n", 0)
    full_n = (joint.get("full_sample") or {}).get("n", 0)
    print(f"joint: JEV×age×prior jev_n={jev_n} full_n={full_n}")

    opt_result = None
    optimize_section = None
    if args.optimize:
        # R3 scaffold only: dry_run=True, no run_backtest_fn, never rewrite strategy params
        opt_result = optimize(
            rows=chain,
            period_start=period_start,
            period_end=period_end,
            dry_run=True,
            run_backtest_fn=None,
            weekly_check_passed=False,
        )
        print(format_optimize_summary(opt_result))
        if opt_result.status == "skipped":
            optimize_section = (
                f"**status=`skipped`** — {opt_result.reason}\n\n"
                f"grid={opt_result.param_grid_size} · history_days={opt_result.history_days} · "
                f"不改参、不写配置。"
            )
        else:
            optimize_section = (
                f"**status=`{opt_result.status}`** — {opt_result.reason or 'scaffold'}\n\n"
                f"grid={opt_result.param_grid_size} · history_days={opt_result.history_days} · "
                f"train_n={opt_result.train_n} test_n={opt_result.test_n} · "
                f"不改参、不写配置。"
            )

    if args.dump_chain:
        dump_path = Path(args.dump_chain)
        if not dump_path.is_absolute():
            dump_path = db_root / dump_path
        # only allow under exports/ for safety of write surface
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "period_start": period_start,
            "period_end": period_end,
            "n": n,
            "rows": [_row_to_dict(r) for r in chain],
        }
        dump_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"dump-chain: {dump_path.resolve()}")

    if args.report:
        end_label = _end_date_label(period_end)
        if args.out:
            out_path = Path(args.out)
            if not out_path.is_absolute():
                out_path = db_root / out_path
        else:
            out_path = db_root / "exports" / "reports" / f"review_weekly_{end_label}.md"
        evo = build_evolution_suggestions(
            chain=chain,
            metrics=metrics,
            exec_summary=exec_summary,
            attribution=attr,
            fill_attr=fill_attr,
            jev_slice_metrics=jev_metrics,
            joint=joint,
            opt_result=opt_result,
            optimize_section=optimize_section,
            period_start=period_start,
            period_end=period_end,
            db_root=db_root,
        )
        print(f"evolution: {len(evo)} suggestions")
        md = render_weekly_md(
            period_start=period_start,
            period_end=period_end,
            chain=chain,
            metrics=metrics,
            baseline=baseline,
            exec_summary=exec_summary,
            jev_slice_metrics=jev_metrics,
            jev_slice_note=(
                f"`jev_strength_v1` ∩ buy ∩ strength≥{args.min_strength} "
                f"∩ liq≥{args.min_liq:,.0f} ∩ coverage≥{args.min_coverage:g} "
                f"∩ price≥{args.min_price:.0e}"
            ),
            attribution=attr,
            fill_attr=fill_attr,
            backtest_summary=bt_summary,
            joint=joint,
            optimize_section=optimize_section,
            evolution_suggestions=evo,
        )
        written = write_report(md, str(out_path))
        print(f"report: {written}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
