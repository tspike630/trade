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
    connect_ro,
    load_jev_coverage,
    load_market_baseline,
    load_signal_chain,
    parse_ts,
    summarize_execution,
    to_iso_z,
)
from review.attribution import (
    fill_attribution,
    load_backtest_summary,
    signal_attribution,
)
from review.metrics import compute_signal_metrics, slice_jev_filtered
from review.report import render_weekly_md, write_report
from review.optimizer import (
    format_optimize_summary,
    optimize,
)
from review.evolution import build_evolution_suggestions
from review.weekly_evidence import compute_weekly_evidence
from review.factor_layers import (
    FACTOR_GETTERS,
    factor_layers,
    list_factors,
    performance_matrix,
)
from review.factor_ic_history import load_history, summarize_all, upsert_row

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
    """Normalize --start/--end to UTC ISO-8601 Z before string compare."""
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        s = s + "T00:00:00"
    try:
        return to_iso_z(parse_ts(s))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid ISO timestamp {s!r}") from exc


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


def _evidence_export_roots(db_root: Path) -> list[Path]:
    return [db_root / "exports", db_root / "crawler" / "exports"]


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
    parser.add_argument(
        "--factor-layers",
        action="store_true",
        help="append §11 factor layer tables + performance matrix "
        "(default off; does not change §1–§10)",
    )
    parser.add_argument(
        "--factors",
        default=None,
        help="comma-separated factor names for layers (default: all registered)",
    )
    parser.add_argument(
        "--ic-history",
        default=None,
        help="IC history CSV path (default exports/reports/factor_ic_history.csv; "
        "only with --factor-layers)",
    )
    args = parser.parse_args(argv)

    if args.start or args.end:
        if not (args.start and args.end):
            print("error: --start and --end must be used together", file=sys.stderr)
            return 2
        try:
            period_start = _normalize_iso(args.start)
            period_end = _normalize_iso(args.end)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
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
        attr = signal_attribution(chain, baseline=baseline)
        fill_attr = fill_attribution(chain)
        up_1h_rows = [
            {
                "strategy_id": row.strategy_id,
                "ts": row.ts,
                "decision_id": row.decision_id,
                "fwd_1h": row.fwd_1h,
            }
            for row in chain
        ]
        weekly_evidence = compute_weekly_evidence(
            paths,
            period_start=period_start,
            period_end=period_end,
            strategy_id=args.strategy,
            export_roots=_evidence_export_roots(db_root),
            up_1h_rows=up_1h_rows,
        )
        fill_attr["evidence_1019"] = weekly_evidence
        bt_summary = load_backtest_summary(paths)
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
    ev_stop = (weekly_evidence.get("stop_loss") or {}).get("loss_share")
    ev_peak = (weekly_evidence.get("daily_open_peak") or {}).get("peak")
    ev_veto = (weekly_evidence.get("step2_veto") or {}).get("triggered")
    f1 = weekly_evidence.get("f1_streak") or {}
    f1_up = (f1.get("up_1h") or {}).get("status")
    f1_realized = (f1.get("realized_pnl") or {}).get("status")
    print(
        f"evidence_1019: stop_loss_loss_share={ev_stop} "
        f"daily_open_peak={ev_peak} step2_veto={ev_veto} "
        f"f1_up_1h={f1_up} f1_realized={f1_realized}"
    )

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
            opt_result=opt_result,
            optimize_section=optimize_section,
            period_start=period_start,
            period_end=period_end,
            db_root=db_root,
        )
        print(f"evolution: {len(evo)} suggestions")
        factor_layer_result: Optional[dict[str, Any]] = None
        if args.factor_layers:
            want = [
                f.strip() for f in (args.factors or "").split(",") if f.strip()
            ] or list_factors()
            layers: dict[str, Any] = {}
            for fname in want:
                if fname not in FACTOR_GETTERS:
                    print(f"warning: unknown factor {fname!r}; skip", file=sys.stderr)
                    continue
                res = factor_layers(chain, fname, min_samples=10)
                if res is not None:
                    layers[fname] = res
            factor_layer_result = {
                "layers": layers,
                "matrix": performance_matrix(metrics.get("by_strategy") or {}),
                "factors": sorted(layers),
            }
            # IC 历史：当期 IC 入 csv（同期覆盖）+ 多期汇总。仅 --factor-layers 时写。
            hist_path = (
                Path(args.ic_history)
                if args.ic_history
                else db_root / "exports" / "reports" / "factor_ic_history.csv"
            )
            period_end_key = period_end[:10]
            for fname, res in layers.items():
                upsert_row(
                    hist_path,
                    period_end_key,
                    fname,
                    res.get("ic"),
                    res.get("monotonic", "n/a"),
                    int(res.get("n", 0)),
                )
            hist = load_history(hist_path)
            factor_layer_result["ic_history"] = {
                "path": str(hist_path.resolve()),
                "summary": summarize_all(hist, sorted(layers)),
            }
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
            optimize_section=optimize_section,
            evolution_suggestions=evo,
            factor_layers=factor_layer_result,
            weekly_evidence=weekly_evidence,
        )
        written = write_report(md, str(out_path))
        print(f"report: {written}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
