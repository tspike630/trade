"""R3 参数寻优脚手架：网格枚举 + 时间切分护栏 + 稳健性评分。

只搭框架，默认不跑正式寻优、不写配置、不改策略参数/闸门。
正式寻优门槛：历史 ≥7d 且周检跨门槛后再开；当前约 0.51d。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence, Union

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

STRATEGY_A = "jev_strength_v1"
STRATEGY_B = "tech_momentum_v1"
STRATEGY_C = "combined_jev_tech_v1"

STRATEGIES: dict[str, str] = {
    "A": STRATEGY_A,
    "B": STRATEGY_B,
    "C": STRATEGY_C,
}

# 占位小网格（可配置覆盖）；不代表正式寻优空间
DEFAULT_THRESHOLDS: list[float] = [0.15, 0.20, 0.30]
DEFAULT_TP: list[float] = [8.0, 10.0, 12.0]
DEFAULT_SL: list[float] = [4.0, 5.0, 6.0]
DEFAULT_MAX_HOLD: list[float] = [30.0, 60.0, 120.0]

MIN_HISTORY_DAYS = 7.0
MIN_SAMPLES = 15  # 对齐周检 n≥15 门槛的软门槛
TRAIN_RATIO_DEFAULT = 0.7

# 稳健性：相邻点核心指标相对突变超过该比例 → 高风险
ROBUSTNESS_JUMP_THRESHOLD = 0.25  # 25% relative jump

BacktestFn = Callable[[dict[str, Any], Sequence[Any]], dict[str, Any]]
RowsOrPeriod = Union[Sequence[Any], tuple[Any, Any], dict[str, Any]]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class OptimizeResult:
    """寻优结果（脚手架）。不自动写配置。"""

    status: str  # "ok" | "skipped" | "dry_run"
    reason: Optional[str] = None
    # 样本内 / 样本外参数建议表（list[dict]），分开存放
    in_sample: list[dict[str, Any]] = field(default_factory=list)
    out_of_sample: list[dict[str, Any]] = field(default_factory=list)
    best_in_sample: Optional[dict[str, Any]] = None
    best_out_of_sample: Optional[dict[str, Any]] = None
    in_sample_ne_out_of_sample: bool = False  # 样本内最优 ≠ 样本外最优
    mismatch_note: Optional[str] = None
    robustness: dict[str, Any] = field(default_factory=dict)
    param_grid_size: int = 0
    train_n: int = 0
    test_n: int = 0
    history_days: Optional[float] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Param grid
# ---------------------------------------------------------------------------


def build_param_grid(
    *,
    strategies: Optional[Sequence[str]] = None,
    thresholds: Optional[Sequence[float]] = None,
    tp: Optional[Sequence[float]] = None,
    sl: Optional[Sequence[float]] = None,
    max_hold: Optional[Sequence[float]] = None,
) -> list[dict[str, Any]]:
    """枚举 strategies × threshold × tp × sl × max_hold。

    strategies 可为简称 A/B/C 或完整 strategy_id；默认 A/B/C 全开。
    阈值为占位小网格，可配置覆盖。
    """
    if strategies is None:
        strat_ids = list(STRATEGIES.values())
    else:
        strat_ids = []
        for s in strategies:
            key = str(s).strip()
            if key in STRATEGIES:
                strat_ids.append(STRATEGIES[key])
            elif key in STRATEGIES.values():
                strat_ids.append(key)
            else:
                strat_ids.append(key)

    ths = list(thresholds) if thresholds is not None else list(DEFAULT_THRESHOLDS)
    tps = list(tp) if tp is not None else list(DEFAULT_TP)
    sls = list(sl) if sl is not None else list(DEFAULT_SL)
    holds = list(max_hold) if max_hold is not None else list(DEFAULT_MAX_HOLD)

    grid: list[dict[str, Any]] = []
    for sid in strat_ids:
        for th in ths:
            for tp_v in tps:
                for sl_v in sls:
                    for mh in holds:
                        grid.append(
                            {
                                "strategy_id": sid,
                                "threshold": float(th),
                                "tp": float(tp_v),
                                "sl": float(sl_v),
                                "max_hold": float(mh),
                            }
                        )
    return grid


# ---------------------------------------------------------------------------
# Time split (hard guardrail)
# ---------------------------------------------------------------------------


def _row_ts(row: Any) -> Optional[str]:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get("ts") or row.get("timestamp")
    return getattr(row, "ts", None) or getattr(row, "timestamp", None)


def _parse_ts(s: Any) -> Optional[datetime]:
    if s is None:
        return None
    if isinstance(s, datetime):
        dt = s
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    text = str(s).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def time_split(
    rows_or_period: RowsOrPeriod,
    train_ratio: float = TRAIN_RATIO_DEFAULT,
) -> tuple[list[Any], list[Any]]:
    """时间切分硬护栏：前 train_ratio 寻优，后 (1-ratio) 样本外验证。

    - 传入 list/tuple of rows（带 ts）：按时间排序后切分
    - 传入 (start, end) 或 {"start":..., "end":...}：返回空列表占位 + 切分边界写在
      各自 list 的说明由调用方自行切片；本函数对 period 返回 ([], []) 并依赖
      optimize() 侧用边界元数据。为保持签名清晰，period 形式返回两个空 list，
      实际切分逻辑在有 rows 时生效。
    """
    if train_ratio <= 0 or train_ratio >= 1:
        raise ValueError(f"train_ratio must be in (0,1), got {train_ratio}")

    # period forms → no rows to split
    if isinstance(rows_or_period, dict):
        return [], []
    if (
        isinstance(rows_or_period, tuple)
        and len(rows_or_period) == 2
        and not hasattr(rows_or_period[0], "ts")
        and not isinstance(rows_or_period[0], dict)
    ):
        # (start, end) strings / datetimes
        a, b = rows_or_period
        if isinstance(a, (str, datetime)) and isinstance(b, (str, datetime)):
            return [], []

    rows = list(rows_or_period)  # type: ignore[arg-type]
    if not rows:
        return [], []

    decorated: list[tuple[datetime, int, Any]] = []
    for i, r in enumerate(rows):
        dt = _parse_ts(_row_ts(r))
        if dt is None:
            dt = datetime.min.replace(tzinfo=timezone.utc)
        decorated.append((dt, i, r))
    decorated.sort(key=lambda x: (x[0], x[1]))

    n = len(decorated)
    cut = max(1, min(n - 1, int(n * train_ratio))) if n >= 2 else n
    # if only 1 row, all train / empty test (caller should have skipped)
    if n < 2:
        return [decorated[0][2]] if n == 1 else [], []

    train = [t[2] for t in decorated[:cut]]
    test = [t[2] for t in decorated[cut:]]
    return train, test


def split_period_bounds(
    period_start: str,
    period_end: str,
    train_ratio: float = TRAIN_RATIO_DEFAULT,
) -> tuple[str, str, str]:
    """Return (train_start, train_end=test_start, test_end) ISO bounds."""
    if train_ratio <= 0 or train_ratio >= 1:
        raise ValueError(f"train_ratio must be in (0,1), got {train_ratio}")
    start = _parse_ts(period_start)
    end = _parse_ts(period_end)
    if start is None or end is None or end <= start:
        raise ValueError(f"invalid period bounds: {period_start!r} .. {period_end!r}")
    span = end - start
    mid = start + span * train_ratio

    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return _iso(start), _iso(mid), _iso(end)


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def _metric_value(row: dict[str, Any]) -> Optional[float]:
    for key in ("score", "up_1h_rate", "sharpe", "total_pnl", "median_1h"):
        v = row.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _param_neighbors(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True if a and b differ by exactly one numeric grid axis (same strategy)."""
    if a.get("strategy_id") != b.get("strategy_id"):
        return False
    keys = ("threshold", "tp", "sl", "max_hold")
    diffs = 0
    for k in keys:
        av, bv = a.get(k), b.get(k)
        if av is None or bv is None:
            continue
        if float(av) != float(bv):
            diffs += 1
    return diffs == 1


def score_robustness(
    results: Sequence[dict[str, Any]],
    *,
    jump_threshold: float = ROBUSTNESS_JUMP_THRESHOLD,
) -> dict[str, Any]:
    """相邻参数点结果突变大的标高风险（过拟合信号）。

    输入 results 每项应含 strategy_id/threshold/tp/sl/max_hold + 至少一个指标。
    返回 dict：flagged 列表、summary、每点 risk 标注。
    """
    items = [dict(r) for r in results]
    flagged: list[dict[str, Any]] = []
    annotated: list[dict[str, Any]] = []

    for i, ri in enumerate(items):
        mi = _metric_value(ri)
        max_jump = 0.0
        risk = "low"
        notes: list[str] = []
        for j, rj in enumerate(items):
            if i == j:
                continue
            if not _param_neighbors(ri, rj):
                continue
            mj = _metric_value(rj)
            if mi is None or mj is None:
                continue
            base = max(abs(mi), abs(mj), 1e-9)
            jump = abs(mi - mj) / base
            if jump > max_jump:
                max_jump = jump
            if jump >= jump_threshold:
                risk = "high"
                notes.append(
                    f"neighbor jump {jump:.1%} vs "
                    f"th={rj.get('threshold')} tp={rj.get('tp')} "
                    f"sl={rj.get('sl')} hold={rj.get('max_hold')}"
                )
                flagged.append(
                    {
                        "a": {
                            "strategy_id": ri.get("strategy_id"),
                            "threshold": ri.get("threshold"),
                            "tp": ri.get("tp"),
                            "sl": ri.get("sl"),
                            "max_hold": ri.get("max_hold"),
                            "metric": mi,
                        },
                        "b": {
                            "strategy_id": rj.get("strategy_id"),
                            "threshold": rj.get("threshold"),
                            "tp": rj.get("tp"),
                            "sl": rj.get("sl"),
                            "max_hold": rj.get("max_hold"),
                            "metric": mj,
                        },
                        "jump": round(jump, 4),
                        "risk": "high",
                        "note": "相邻参数点结果突变大 — 过拟合信号",
                    }
                )
        if risk == "low" and max_jump >= jump_threshold * 0.6:
            risk = "medium"
        row = dict(ri)
        row["robustness_risk"] = risk
        row["robustness_max_jump"] = round(max_jump, 4)
        if notes:
            row["robustness_notes"] = notes
            row["flag"] = "RED" if risk == "high" else "YELLOW"
        annotated.append(row)

    # de-dup flagged pairs (a,b) / (b,a)
    seen: set[tuple] = set()
    uniq: list[dict[str, Any]] = []
    for f in flagged:
        key = tuple(
            sorted(
                [
                    (
                        f["a"].get("strategy_id"),
                        f["a"].get("threshold"),
                        f["a"].get("tp"),
                        f["a"].get("sl"),
                        f["a"].get("max_hold"),
                    ),
                    (
                        f["b"].get("strategy_id"),
                        f["b"].get("threshold"),
                        f["b"].get("tp"),
                        f["b"].get("sl"),
                        f["b"].get("max_hold"),
                    ),
                ]
            )
        )
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)

    n_high = sum(1 for r in annotated if r.get("robustness_risk") == "high")
    return {
        "jump_threshold": jump_threshold,
        "n_results": len(annotated),
        "n_high_risk": n_high,
        "n_flagged_pairs": len(uniq),
        "flagged_pairs": uniq,
        "annotated": annotated,
        "summary": (
            f"高风险相邻突变 {n_high}/{len(annotated)} 点，"
            f"成对标记 {len(uniq)}（阈值 {jump_threshold:.0%}）"
            if annotated
            else "无结果可评"
        ),
    }


# ---------------------------------------------------------------------------
# Gate / history helpers
# ---------------------------------------------------------------------------


def estimate_history_days(
    rows: Optional[Sequence[Any]] = None,
    *,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
) -> float:
    """Estimate span in days from rows ts range or explicit period."""
    if rows:
        dts = [_parse_ts(_row_ts(r)) for r in rows]
        dts = [d for d in dts if d is not None]
        if len(dts) >= 2:
            return max(0.0, (max(dts) - min(dts)).total_seconds() / 86400.0)
        if len(dts) == 1 and period_start and period_end:
            pass  # fall through to period
        elif len(dts) == 1:
            return 0.0
    if period_start and period_end:
        a, b = _parse_ts(period_start), _parse_ts(period_end)
        if a and b and b > a:
            return (b - a).total_seconds() / 86400.0
    return 0.0


def check_optimize_gates(
    *,
    history_days: float,
    n_samples: int,
    min_history_days: float = MIN_HISTORY_DAYS,
    min_samples: int = MIN_SAMPLES,
    weekly_check_passed: bool = False,
) -> tuple[bool, str]:
    """Return (ok, reason). ok=False → optimize should skip."""
    reasons: list[str] = []
    if history_days < min_history_days:
        reasons.append(
            f"历史不足：{history_days:.2f}d < {min_history_days:g}d"
            f"（现约 {history_days:.2f}d，需 ≥{min_history_days:g}d）"
        )
    if n_samples < min_samples:
        reasons.append(f"样本不足：n={n_samples} < {min_samples}")
    if not weekly_check_passed:
        reasons.append("周检尚未跨门槛（连续 2 周过滤后达标前不正式寻优）")
    if reasons:
        return False, "；".join(reasons)
    return True, ""


def _pick_best(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not rows:
        return None
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        m = _metric_value(r)
        if m is None:
            continue
        scored.append((m, r))
    if not scored:
        return dict(rows[0])
    scored.sort(key=lambda x: x[0], reverse=True)
    return dict(scored[0][1])


def _params_equal(a: Optional[dict[str, Any]], b: Optional[dict[str, Any]]) -> bool:
    if a is None or b is None:
        return a is b
    keys = ("strategy_id", "threshold", "tp", "sl", "max_hold")
    return all(a.get(k) == b.get(k) for k in keys)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def optimize(
    *,
    rows: Optional[Sequence[Any]] = None,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    train_ratio: float = TRAIN_RATIO_DEFAULT,
    strategies: Optional[Sequence[str]] = None,
    thresholds: Optional[Sequence[float]] = None,
    tp: Optional[Sequence[float]] = None,
    sl: Optional[Sequence[float]] = None,
    max_hold: Optional[Sequence[float]] = None,
    min_history_days: float = MIN_HISTORY_DAYS,
    min_samples: int = MIN_SAMPLES,
    weekly_check_passed: bool = False,
    dry_run: bool = True,
    run_backtest_fn: Optional[BacktestFn] = None,
) -> OptimizeResult:
    """R3 寻优入口。

    - 历史 < min_history_days 或样本不足 → status=skipped + reason，不调用重计算
    - dry_run=True（默认）：只建网格/切分结构，不调用 run_backtest_fn，不改参
    - run_backtest_fn：可选回调，签名 (param_dict, rows) -> metrics dict；默认不调用
    - 正式跑通后：样本内/外都报；样本内最优 ≠ 样本外最优明确标注
    - **不自动写配置**
    """
    row_list: list[Any] = list(rows) if rows is not None else []
    n_samples = len(row_list)
    history_days = estimate_history_days(
        row_list, period_start=period_start, period_end=period_end
    )
    # 若无 rows 但有 period，用 period 跨度作历史估计，样本以 0 计
    if rows is None and period_start and period_end and history_days <= 0:
        history_days = estimate_history_days(
            None, period_start=period_start, period_end=period_end
        )

    grid = build_param_grid(
        strategies=strategies,
        thresholds=thresholds,
        tp=tp,
        sl=sl,
        max_hold=max_hold,
    )

    gate_ok, gate_reason = check_optimize_gates(
        history_days=history_days,
        n_samples=n_samples,
        min_history_days=min_history_days,
        min_samples=min_samples,
        weekly_check_passed=weekly_check_passed,
    )

    meta: dict[str, Any] = {
        "train_ratio": train_ratio,
        "min_history_days": min_history_days,
        "min_samples": min_samples,
        "weekly_check_passed": weekly_check_passed,
        "dry_run": dry_run,
        "has_backtest_fn": run_backtest_fn is not None,
        "writes_config": False,
    }
    if period_start and period_end:
        try:
            ts, tm, te = split_period_bounds(period_start, period_end, train_ratio)
            meta["train_start"] = ts
            meta["train_end"] = tm
            meta["test_start"] = tm
            meta["test_end"] = te
        except ValueError as e:
            meta["period_split_error"] = str(e)

    if not gate_ok:
        return OptimizeResult(
            status="skipped",
            reason=gate_reason,
            param_grid_size=len(grid),
            history_days=round(history_days, 4),
            train_n=0,
            test_n=0,
            meta=meta,
        )

    train, test = time_split(row_list, train_ratio=train_ratio) if row_list else ([], [])
    meta["train_n"] = len(train)
    meta["test_n"] = len(test)

    # Scaffold / dry_run: never call heavy backtest; never write config
    if dry_run or run_backtest_fn is None:
        return OptimizeResult(
            status="dry_run",
            reason=(
                "脚手架 dry_run：已过历史/样本门槛，但未调用回测"
                "（run_backtest_fn 未注入或 dry_run=True）；不改参、不写配置"
                if dry_run
                else "未注入 run_backtest_fn，跳过重计算；不改参、不写配置"
            ),
            param_grid_size=len(grid),
            history_days=round(history_days, 4),
            train_n=len(train),
            test_n=len(test),
            in_sample=[],
            out_of_sample=[],
            meta=meta,
        )

    # Formal path (scaffold-ready): optional callback only
    in_sample: list[dict[str, Any]] = []
    out_of_sample: list[dict[str, Any]] = []
    for param in grid:
        try:
            train_m = run_backtest_fn(param, train)
        except Exception as e:  # noqa: BLE001 — isolate one cell failure
            train_m = {"error": str(e), "score": None}
        try:
            test_m = run_backtest_fn(param, test)
        except Exception as e:  # noqa: BLE001
            test_m = {"error": str(e), "score": None}
        in_sample.append({**param, **train_m, "split": "in_sample"})
        out_of_sample.append({**param, **test_m, "split": "out_of_sample"})

    rob_in = score_robustness(in_sample)
    rob_out = score_robustness(out_of_sample)
    best_in = _pick_best(rob_in.get("annotated") or in_sample)
    best_out = _pick_best(rob_out.get("annotated") or out_of_sample)
    mismatch = not _params_equal(best_in, best_out)
    mismatch_note = None
    if mismatch:
        mismatch_note = (
            "样本内最优 ≠ 样本外最优 — 过拟合风险，勿直接改参；"
            f"in={_param_key(best_in)} out={_param_key(best_out)}"
        )

    return OptimizeResult(
        status="ok",
        reason=None,
        in_sample=rob_in.get("annotated") or in_sample,
        out_of_sample=rob_out.get("annotated") or out_of_sample,
        best_in_sample=best_in,
        best_out_of_sample=best_out,
        in_sample_ne_out_of_sample=mismatch,
        mismatch_note=mismatch_note,
        robustness={
            "in_sample": {
                "n_high_risk": rob_in.get("n_high_risk"),
                "n_flagged_pairs": rob_in.get("n_flagged_pairs"),
                "summary": rob_in.get("summary"),
                "flagged_pairs": rob_in.get("flagged_pairs"),
            },
            "out_of_sample": {
                "n_high_risk": rob_out.get("n_high_risk"),
                "n_flagged_pairs": rob_out.get("n_flagged_pairs"),
                "summary": rob_out.get("summary"),
                "flagged_pairs": rob_out.get("flagged_pairs"),
            },
        },
        param_grid_size=len(grid),
        history_days=round(history_days, 4),
        train_n=len(train),
        test_n=len(test),
        meta=meta,
    )


def _param_key(p: Optional[dict[str, Any]]) -> str:
    if not p:
        return "—"
    return (
        f"{p.get('strategy_id')}@th={p.get('threshold')}"
        f"/tp={p.get('tp')}/sl={p.get('sl')}/hold={p.get('max_hold')}"
    )


def format_optimize_summary(result: OptimizeResult) -> str:
    """One-line / short multi-line CLI summary."""
    lines = [
        f"optimize: status={result.status} grid={result.param_grid_size} "
        f"history_days={result.history_days} "
        f"train_n={result.train_n} test_n={result.test_n}"
    ]
    if result.reason:
        lines.append(f"optimize: reason={result.reason}")
    if result.in_sample_ne_out_of_sample and result.mismatch_note:
        lines.append(f"optimize: MISMATCH {result.mismatch_note}")
    lines.append("optimize: writes_config=false（R3 不自动写配置；R4 人工确认）")
    return "\n".join(lines)
