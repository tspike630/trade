"""SIGNAL_STRUCTURE_CHECK (S2C-FW), R1 read-only scaffold.

Reads ``structure_registry.json``, scores the active decision and execution
layers, and returns a verdict. Collection, indicator, and meta stay pending:
there is no activation path for them.

Business DBs are opened only through ``review.data.connect_ro``. This module
does not write them, and it does not change gates, weights, TP/SL, hold, or
the decision main chain.

Token age uses ``review.attribution.resolve_token_age_hours`` (raw, then
pairs). Do not add a second parser.

Execution cells use realized / exit / shadow only. ``up_1h`` and ``fwd_*``
are forbidden there. ``stop_loss_share`` (dashboard) and
``stop_loss_share_of_neg`` (share of negative pnl) always coexist.
``mean_peak_pnl_pct`` is null when a bucket has no shadow — never filled
with 0. The slippage axis is baseline-only this period (yellow frozen);
do not change TP/SL to make that yellow fire.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from review.attribution import _coarse_age_bucket, _liq_bucket, _strength_bucket, resolve_token_age_hours
from review.data import DbPaths, SignalChainRow, connect_ro, parse_ts, safe_float

_LAYER_IDS = ("collection", "indicator", "decision", "execution", "meta")
_ACTIVE_LAYER_IDS = frozenset({"decision", "execution"})
_EXEC_AXIS_IDS = (
    "exit_reason",
    "daily_opens",
    "peak_giveback",
    "slippage",
    "size_band",
    "hold_duration",
)
EXECUTION_METRIC_KEYS = (
    "n",
    "realized_pnl_usd",
    "win_rate",
    "mean_peak_pnl_pct",
    "mean_giveback_from_peak",
    "stop_loss_share",
    "stop_loss_share_of_neg",
)
FORBIDDEN_EXECUTION_FIELDS = frozenset(
    {
        "up_1h",
        "up_4h",
        "fwd_1h",
        "fwd_4h",
        "med_1h",
        "median_1h",
        "median_4h",
        "up_1h_rate",
        "jev_strength",
    }
)
_EXIT_REASONS = frozenset({"stop_loss", "max_hold", "take_profit", "manual"})
_FINE_AGE_LABELS = ("<1h", "1-24h", "1-7d", ">7d", "unknown")
_COARSE_AGE_LABELS = ("<1d", "1-7d", ">7d", "unknown")

try:
    _HK = ZoneInfo("Asia/Hong_Kong")
    HK_TZ_NAME = "Asia/Hong_Kong"
except ZoneInfoNotFoundError:  # pragma: no cover - tzdata missing
    _HK = timezone(timedelta(hours=8))
    HK_TZ_NAME = "UTC+8"


def default_registry_path() -> Path:
    return Path(__file__).with_name("structure_registry.json")


def load_registry(path: Optional[str | Path] = None) -> dict[str, Any]:
    """Load and check the five-layer R1 registry."""
    target = Path(path) if path is not None else default_registry_path()
    data = json.loads(target.read_text(encoding="utf-8"))
    layers = data.get("layers")
    if not isinstance(layers, list):
        raise ValueError("structure registry layers must be a list")
    ids = [layer.get("id") for layer in layers]
    if ids != list(_LAYER_IDS):
        raise ValueError(f"structure registry layers must be {_LAYER_IDS}, got {ids}")
    by_id = {layer["id"]: layer for layer in layers}
    for layer_id in _LAYER_IDS:
        status = by_id[layer_id].get("status")
        if layer_id in _ACTIVE_LAYER_IDS:
            if status != "active":
                raise ValueError(f"{layer_id} must be active in R1")
        elif status != "pending":
            raise ValueError(f"{layer_id} must stay pending in R1")
    execution = by_id["execution"]
    if list(execution.get("cell_metrics") or []) != list(EXECUTION_METRIC_KEYS):
        raise ValueError("execution cell_metrics must keep both stop-loss fields")
    forbid = set(execution.get("forbid_fields") or [])
    missing = {"up_1h", "fwd_1h", "fwd_4h"} - forbid
    if missing:
        raise ValueError(f"execution forbid_fields missing {sorted(missing)}")
    axes = [axis.get("id") for axis in execution.get("axes") or []]
    if axes != list(_EXEC_AXIS_IDS):
        raise ValueError(f"execution axes must be {_EXEC_AXIS_IDS}, got {axes}")
    return data


@dataclass
class _Pos:
    position_id: str
    status: str
    pnl_usd: Optional[float]
    exit_reason: str
    closed_at: str
    opened_at: Optional[str]
    size_usd: Optional[float]
    max_hold_min: Optional[float]
    slippage_pct: Optional[float]
    strategy_id: str
    has_shadow: bool
    peak_pnl_pct: Optional[float]
    giveback_from_peak_pct: Optional[float]


def structure_check(
    *,
    chain: list[SignalChainRow],
    jev_rows: list[SignalChainRow],
    paths: DbPaths,
    period_start: str,
    period_end: str,
    strategy_id: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    registry: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Score active layers. Does not write."""
    reg = registry if registry is not None else load_registry()
    ctx = _Context(
        chain=chain,
        jev_rows=jev_rows,
        paths=paths,
        period_start=period_start,
        period_end=period_end,
        strategy_id=strategy_id,
        history=list(history or []),
        registry=reg,
    )
    layers: dict[str, Any] = {}
    for layer in reg.get("layers") or []:
        built = _evaluate_layer(layer, ctx)
        layers[built["id"]] = built
    _assert_execution_closed(layers.get("execution"))
    conclusions = {
        "decision": (layers.get("decision") or {}).get("conclusion") or "",
        "execution": (layers.get("execution") or {}).get("conclusion") or "",
    }
    if "up_1h" in conclusions["execution"] or "fwd_" in conclusions["execution"]:
        raise RuntimeError("execution conclusion must not cite forward returns")
    history_rows = [
        _history_row(period_end, layers[name])
        for name in ("decision", "execution")
        if name in layers and layers[name].get("activated")
    ]
    return {
        "id": reg.get("id") or "SIGNAL_STRUCTURE_CHECK",
        "version": reg.get("version"),
        "period_start": period_start,
        "period_end": period_end,
        "read_only": True,
        "param_freeze_until": reg.get("param_freeze_until"),
        "freeze_active": _freeze_active(reg, period_end),
        "footnotes": list(reg.get("footnotes") or []),
        "layers": layers,
        "conclusions": conclusions,
        "history_rows": history_rows,
    }


@dataclass
class _Context:
    chain: list[SignalChainRow]
    jev_rows: list[SignalChainRow]
    paths: DbPaths
    period_start: str
    period_end: str
    strategy_id: Optional[str]
    history: list[dict[str, Any]]
    registry: dict[str, Any]


def _evaluate_layer(layer: dict[str, Any], ctx: _Context) -> dict[str, Any]:
    layer_id = str(layer.get("id") or "")
    if layer_id not in _LAYER_IDS:
        raise ValueError(f"unknown structure layer {layer_id}")
    status = layer.get("status")
    # Pending layers, and any non-decision/execution id, never get an evaluator.
    if status != "active" or layer_id not in _ACTIVE_LAYER_IDS:
        refused = status == "active" and layer_id not in _ACTIVE_LAYER_IDS
        return _inactive_layer(layer, refused=refused)
    if layer_id == "decision":
        return _decision_layer(layer, ctx)
    if layer_id == "execution":
        return _execution_layer(layer, ctx)
    return _inactive_layer(layer, refused=True)


def _inactive_layer(layer: dict[str, Any], *, refused: bool) -> dict[str, Any]:
    note = str(layer.get("note") or "pending")
    if refused:
        note = "R1 不写激活路径；collection/indicator/meta 即使被标成 active 也不计算。"
    return {
        "id": layer.get("id"),
        "status": "pending",
        "activated": False,
        "metrics": None,
        "note": note,
    }


def _decision_layer(layer: dict[str, Any], ctx: _Context) -> dict[str, Any]:
    rules = layer.get("yellow") or {}
    min_n = int(rules.get("min_n", 15))
    min_unique = int(rules.get("min_unique", 10))
    min_abs = float(rules.get("min_abs_delta_up_1h", 0.10))
    sparse_below = int(ctx.registry.get("sparse_n_lt", 5))
    baseline, _baseline_n = _up_rate(ctx.jev_rows)
    groups: dict[tuple[str, str, str], list[SignalChainRow]] = defaultdict(list)
    for row in ctx.jev_rows:
        hours = resolve_token_age_hours(row)
        age = "unknown" if hours is None else _coarse_age_bucket(hours)
        groups[(age, _liq_bucket(row.liquidity_at_signal), _strength_bucket(row.jev_strength))].append(row)

    cells: list[dict[str, Any]] = []
    directions: list[str] = []
    for key in sorted(groups, key=lambda item: (-len(groups[item]), item)):
        rows = groups[key]
        rate, n_1h = _up_rate(rows)
        n = len(rows)
        unique = _unique_tokens(rows)
        sparse = n < sparse_below
        delta = None if rate is None or baseline is None else rate - baseline
        yellow = (
            not sparse
            and n >= min_n
            and unique >= min_unique
            and delta is not None
            and abs(delta) >= min_abs
        )
        cell = {
            "age": key[0],
            "liq": key[1],
            "strength": key[2],
            "n": n,
            "unique_token": unique,
            "n_1h": n_1h,
            "up_1h_rate": rate,
            "delta_up_1h": delta,
            "sparse": sparse,
            "interpret": not sparse,
            "yellow": yellow,
        }
        cells.append(cell)
        if yellow and delta is not None:
            sign = "up" if delta > 0 else "down"
            directions.append(f"{key[0]}/{key[1]}/{key[2]}:{sign}")

    candidate = "yellow" if directions else "green"
    verdict, ticket = _streak_verdict(
        ctx,
        layer_id="decision",
        candidate=candidate,
        directions=directions,
    )
    coverage = _age_coverage(ctx.chain)
    fine = _fine_age_coverage(ctx.chain)
    n_sparse = sum(1 for cell in cells if cell["sparse"])
    conclusion = _decision_conclusion(
        baseline=baseline,
        n_cells=len(cells),
        n_sparse=n_sparse,
        n_yellow=len(directions),
        verdict=verdict,
        freeze=_freeze_active(ctx.registry, ctx.period_end),
    )
    return {
        "id": "decision",
        "status": "active",
        "activated": True,
        "baseline_name": "jev_filtered_up_1h",
        "baseline_up_1h": baseline,
        "n": len(ctx.jev_rows),
        "unique_token": _unique_tokens(ctx.jev_rows),
        "age_resolver": "review.attribution.resolve_token_age_hours",
        "age_coverage": coverage,
        "core_cross": cells,
        "n_sparse": n_sparse,
        "n_yellow": len(directions),
        "experimental": {
            "folded": True,
            "token_age_fine": {
                "yellow_eligible": False,
                "note": "细桶只记覆盖，不出黄。样本不足时不单列信号。",
                "buckets": fine,
            },
        },
        "candidate": candidate,
        "verdict": verdict,
        "directions": directions,
        "review_ticket": ticket,
        "conclusion": conclusion,
    }


def _execution_layer(layer: dict[str, Any], ctx: _Context) -> dict[str, Any]:
    loaded = _load_positions(ctx)
    if loaded is None:
        return _execution_gap(ctx, "paper positions unreadable")
    closed, opens_by_day, open_count = loaded
    metrics = _metric_block(closed)
    rules = layer.get("yellow") or {}
    axis_defs = {axis["id"]: axis for axis in layer.get("axes") or []}
    day_count = dict(opens_by_day)
    peak_day = None
    peak = 0
    if day_count:
        peak_day = max(day_count, key=lambda day: (day_count[day], day))
        peak = day_count[peak_day]

    grouped = _group_positions(closed, day_count)
    axes = [
        _axis_payload(axis_defs[name], grouped[name], closed, name)
        for name in _EXEC_AXIS_IDS
        if name in axis_defs
    ]
    shown_peak = peak if day_count else 0
    shown_day = peak_day or "—"
    for axis in axes:
        if axis["id"] != "daily_opens":
            continue
        counts = "；".join(
            f"{bucket['bucket']} n={bucket['metrics']['n']}"
            for bucket in axis["buckets"]
            if bucket["bucket"] != "unknown" or bucket["metrics"]["n"]
        )
        axis["summary"] = (
            f"{counts}。峰 {shown_peak} @ {shown_day} {HK_TZ_NAME}"
            "（#71 标量，不进交叉格）。"
        )
    giveback = _giveback_stats(closed)
    slip_share = _slippage_gt15_share(closed)
    slip_frozen = bool((axis_defs.get("slippage") or {}).get("yellow_frozen", True))
    triggers: list[str] = []
    frozen_would: list[str] = []
    share = metrics["stop_loss_share"]
    total = metrics["realized_pnl_usd"]
    share_cut = float(rules.get("stop_loss_share_gte", 0.5))
    if (
        share is not None
        and total is not None
        and total < 0
        and float(share) >= share_cut
    ):
        triggers.append("stop_loss_share")
    ratio_cut = float(rules.get("mean_giveback_over_peak_gt", 2.0))
    if giveback["mean_ratio"] is not None and float(giveback["mean_ratio"]) > ratio_cut:
        triggers.append("giveback_over_peak")
    slip_cut = float(rules.get("slippage_gt_15_share_gte", 0.30))
    slip_hit = slip_share is not None and float(slip_share) >= slip_cut
    if slip_hit and slip_frozen:
        frozen_would.append("slippage_gt_15")
    elif slip_hit:
        triggers.append("slippage_gt_15")

    candidate = "yellow" if triggers else "green"
    verdict, ticket = _streak_verdict(
        ctx,
        layer_id="execution",
        candidate=candidate,
        directions=triggers,
    )
    hold_axis = next(axis for axis in axes if axis["id"] == "hold_duration")
    size_axis = next(axis for axis in axes if axis["id"] == "size_band")
    if share is not None and float(share) >= share_cut:
        bleed = "主要出血在 exit_reason=stop_loss"
    else:
        bleed = "exit_reason 分布见摘要"
    conclusion = _execution_conclusion(
        metrics=metrics,
        giveback=giveback,
        slip_share=slip_share,
        hold_axis=hold_axis,
        size_axis=size_axis,
        n_closed=len(closed),
        verdict=verdict,
        freeze=_freeze_active(ctx.registry, ctx.period_end),
        bleed=bleed,
        slippage_frozen=slip_frozen,
    )
    gt2 = _pct_text(giveback["gt_2x_share"])
    return {
        "id": "execution",
        "status": "active",
        "activated": True,
        "data_gap": False,
        "tz": HK_TZ_NAME,
        "open_count": open_count,
        "n_closed": len(closed),
        "n_opens": sum(day_count.values()),
        "metrics": metrics,
        "peak_daily_opens": shown_peak,
        "peak_day": peak_day,
        "by_day": day_count,
        "giveback": giveback,
        "slippage_gt_15_share": slip_share,
        "slippage_yellow_frozen": slip_frozen,
        "triggers": triggers,
        "frozen_would_trigger": frozen_would,
        "axes": axes,
        "cross": {
            "folded": True,
            "note": "首期单维 6 轴；轴间交叉折叠，防假阳性。",
        },
        "candidate": candidate,
        "verdict": verdict,
        "directions": list(triggers),
        "review_ticket": ticket,
        "note_71": (
            f"Step2 硬旁证：stop_loss_share（dashboard）={_pct_text(share)}；"
            f"日开峰={shown_peak} @ {shown_day} {HK_TZ_NAME}。≠改闸。"
        ),
        "note_72": (
            f"退出质量：可算比 >2x 占比={gt2}"
            f"（{giveback['gt_2x']}/{giveback['computable']}）。不并进 Step2 否决。"
        ),
        "conclusion": conclusion,
    }


def _execution_gap(ctx: _Context, note: str) -> dict[str, Any]:
    verdict, ticket = _streak_verdict(
        ctx,
        layer_id="execution",
        candidate="green",
        directions=[],
    )
    return {
        "id": "execution",
        "status": "active",
        "activated": True,
        "data_gap": True,
        "note": note,
        "tz": HK_TZ_NAME,
        "open_count": 0,
        "n_closed": 0,
        "n_opens": 0,
        "metrics": _metric_block([]),
        "peak_daily_opens": None,
        "peak_day": None,
        "by_day": {},
        "giveback": _giveback_stats([]),
        "slippage_gt_15_share": None,
        "slippage_yellow_frozen": True,
        "triggers": [],
        "frozen_would_trigger": [],
        "axes": [],
        "cross": {"folded": True, "note": "无执行样本，交叉不展开。"},
        "candidate": "green",
        "verdict": verdict,
        "directions": [],
        "review_ticket": ticket,
        "note_71": "Step2 硬旁证：本窗口无已平仓样本。≠改闸。",
        "note_72": "退出质量：无 shadow 可算比。不并进 Step2 否决。",
        "conclusion": "本周执行层无已平仓样本，6 轴留空。判定 🟢。旁证≠改闸；#71≠#72。",
    }


def _group_positions(
    closed: list[_Pos],
    day_count: dict[str, int],
) -> dict[str, dict[str, list[_Pos]]]:
    grouped: dict[str, dict[str, list[_Pos]]] = {name: defaultdict(list) for name in _EXEC_AXIS_IDS}
    for pos in closed:
        grouped["exit_reason"][_exit_bucket(pos.exit_reason)].append(pos)
        grouped["daily_opens"][_daily_bucket(pos, day_count)].append(pos)
        grouped["peak_giveback"][_peak_bucket(pos)].append(pos)
        grouped["slippage"][_slip_bucket(pos.slippage_pct)].append(pos)
        grouped["size_band"][_size_bucket(pos.size_usd)].append(pos)
        grouped["hold_duration"][_hold_bucket(_hold_minutes(pos))].append(pos)
    return grouped


def _axis_payload(
    axis_def: dict[str, Any],
    groups: dict[str, list[_Pos]],
    closed: list[_Pos],
    axis_id: str,
) -> dict[str, Any]:
    labels = list(axis_def.get("buckets") or [])
    folded = list(axis_def.get("folded_buckets") or [])
    ordered = labels + [label for label in folded if label not in labels]
    for extra in sorted(groups):
        if extra not in ordered:
            ordered.append(extra)
    buckets: list[dict[str, Any]] = []
    for label in ordered:
        rows = groups.get(label, [])
        payload = _bucket_payload(label, rows)
        if axis_id == "hold_duration" and label == ">2h" and payload["empty"]:
            payload["empty_by_design"] = _gt2h_empty_by_design(closed)
        elif label in folded:
            payload["folded"] = True
        buckets.append(payload)
    return {
        "id": axis_id,
        "status": axis_def.get("status"),
        "screen": axis_def.get("screen"),
        "yellow_frozen": bool(axis_def.get("yellow_frozen", False)),
        "summary": _axis_summary(axis_id, buckets, closed),
        "buckets": buckets,
    }


def _bucket_payload(label: str, rows: list[_Pos]) -> dict[str, Any]:
    n = len(rows)
    return {
        "bucket": label,
        "empty": n == 0,
        "sparse": 0 < n < 5,
        "interpret": n >= 5,
        "empty_by_design": False,
        "folded": False,
        "metrics": _metric_block(rows),
    }


def _metric_block(rows: list[_Pos]) -> dict[str, Any]:
    """Seven execution metrics. Both stop-loss fields are always present."""
    n = len(rows)
    pnls = [row.pnl_usd for row in rows if row.pnl_usd is not None]
    total = sum(pnls) if pnls else None
    wins = sum(1 for value in pnls if value > 0)
    stop_pnls = [
        row.pnl_usd
        for row in rows
        if row.exit_reason == "stop_loss" and row.pnl_usd is not None
    ]
    negatives = [value for value in pnls if value < 0]
    share: Optional[float] = None
    of_neg: Optional[float] = None
    if stop_pnls:
        stop_sum = sum(stop_pnls)
        if total is not None and total < 0 and abs(total) > 1e-12:
            share = min(1.0, abs(stop_sum) / abs(total))
        if negatives and abs(sum(negatives)) > 1e-12:
            of_neg = abs(stop_sum) / abs(sum(negatives))
    peaks = [
        row.peak_pnl_pct
        for row in rows
        if row.has_shadow and row.peak_pnl_pct is not None
    ]
    gives = [
        row.giveback_from_peak_pct
        for row in rows
        if row.has_shadow and row.giveback_from_peak_pct is not None
    ]
    # No shadow → null. Do not substitute 0.
    mean_peak = (sum(peaks) / len(peaks)) if peaks else None
    mean_give = (sum(gives) / len(gives)) if gives else None
    return {
        "n": n,
        "realized_pnl_usd": total,
        "win_rate": (wins / n) if n else None,
        "mean_peak_pnl_pct": mean_peak,
        "mean_giveback_from_peak": mean_give,
        "stop_loss_share": share,
        "stop_loss_share_of_neg": of_neg,
    }


def _giveback_stats(rows: list[_Pos]) -> dict[str, Any]:
    ratios: list[float] = []
    for row in rows:
        peak = row.peak_pnl_pct
        give = row.giveback_from_peak_pct
        if not row.has_shadow or peak is None or peak <= 0 or give is None:
            continue
        ratios.append(give / peak)
    gt = sum(1 for ratio in ratios if ratio > 2)
    return {
        "computable": len(ratios),
        "gt_2x": gt,
        "gt_2x_share": (gt / len(ratios)) if ratios else None,
        "mean_ratio": (sum(ratios) / len(ratios)) if ratios else None,
    }


def _slippage_gt15_share(rows: list[_Pos]) -> Optional[float]:
    known = [row.slippage_pct for row in rows if row.slippage_pct is not None]
    if not known:
        return None
    return sum(1 for value in known if value > 15) / len(known)


def _axis_summary(axis_id: str, buckets: list[dict[str, Any]], closed: list[_Pos]) -> str:
    by_label = {bucket["bucket"]: bucket for bucket in buckets}

    def n_of(label: str) -> int:
        bucket = by_label.get(label)
        if not bucket:
            return 0
        return int(bucket["metrics"]["n"])

    if axis_id == "exit_reason":
        parts = [
            f"{label} n={n_of(label)}"
            for label in ("stop_loss", "max_hold", "take_profit", "manual")
        ]
        return "；".join(parts)
    if axis_id == "daily_opens":
        return "强度四桶见明细；峰值为 #71 标量，不进交叉格。"
    if axis_id == "peak_giveback":
        return (
            f"可算比 <1x/{n_of('<1x')}、1-2x/{n_of('1-2x')}、>2x/{n_of('>2x')}；"
            f"peak_nonpositive={n_of('peak_nonpositive')}；no_shadow={n_of('no_shadow')}。"
            "无 shadow 的 mean peak 为 null。"
        )
    if axis_id == "slippage":
        share = _slippage_gt15_share(closed)
        shown = "—" if share is None else f"{share * 100:.1f}%"
        return f">15% 占比 {shown}（入场 meta）。首期只建基线，黄冻结。"
    if axis_id == "size_band":
        return _size_summary(buckets, len(closed))
    if axis_id == "hold_duration":
        gt = by_label.get(">2h") or {}
        if n_of(">2h") == 0 and gt.get("empty_by_design"):
            return ">2h 空（empty_by_design）；明细折叠。"
        if n_of(">2h") == 0:
            return ">2h 本周无样本；明细折叠。"
        return f">2h n={n_of('>2h')}；明细折叠。"
    return ""


def _size_summary(buckets: list[dict[str, Any]], n_closed: int) -> str:
    sized = [
        bucket
        for bucket in buckets
        if bucket["bucket"] != "unknown" and int(bucket["metrics"]["n"]) > 0
    ]
    if not sized or n_closed <= 0:
        return "无 size；边缘桶折叠。"
    top = max(sized, key=lambda bucket: int(bucket["metrics"]["n"]))
    n = int(top["metrics"]["n"])
    if n / n_closed >= 0.8:
        return f"几乎单桶 {top['bucket']}（{n}/{n_closed}）；边缘桶折叠。"
    return f"主桶 {top['bucket']}（{n}/{n_closed}）；边缘桶折叠。"


def _streak_verdict(
    ctx: _Context,
    *,
    layer_id: str,
    candidate: str,
    directions: list[str],
) -> tuple[str, Optional[str]]:
    """Single week stays yellow. Red needs the previous week, same direction."""
    if candidate != "yellow":
        return "green", None
    gap_days = float(ctx.registry.get("consecutive_gap_days_max", 8))
    if not _same_direction_previous(ctx.history, layer_id, ctx.period_end, directions, gap_days):
        return "yellow", None
    freeze = _freeze_active(ctx.registry, ctx.period_end)
    if freeze:
        ticket = "连续 2 周同向，升复核单；冻结期内只建议，不落地改 TP/SL/hold。旁证≠改闸；#71≠#72。"
    else:
        ticket = "连续 2 周同向，升复核单；只建议，不改闸、不改权、不改 TP/SL/hold。旁证≠改闸；#71≠#72。"
    return "red", ticket


def _same_direction_previous(
    history: list[dict[str, Any]],
    layer_id: str,
    period_end: str,
    directions: list[str],
    gap_days: float,
) -> bool:
    if not directions:
        return False
    try:
        current = parse_ts(period_end)
    except (TypeError, ValueError):
        return False
    want = set(directions)
    best: Optional[tuple[datetime, set[str]]] = None
    for row in history:
        if str(row.get("layer") or "") != layer_id:
            continue
        raw_end = str(row.get("period_end") or "")
        if not raw_end or raw_end == period_end:
            continue
        try:
            prev = parse_ts(raw_end)
        except (TypeError, ValueError):
            continue
        if prev >= current:
            continue
        delta_days = (current - prev).total_seconds() / 86400.0
        if delta_days <= 0 or delta_days > gap_days:
            continue
        prev_dirs = {part for part in str(row.get("direction") or "").split(";") if part}
        if best is None or prev > best[0]:
            best = (prev, prev_dirs)
    if best is None:
        return False
    return bool(best[1] & want)


def _freeze_active(registry: dict[str, Any], period_end: str) -> bool:
    raw = str(registry.get("param_freeze_until") or "").strip()
    if not raw:
        return False
    try:
        end = parse_ts(period_end).date()
    except (TypeError, ValueError):
        return False
    try:
        if len(raw) == 10:
            freeze = datetime.fromisoformat(raw).date()
        else:
            freeze = parse_ts(raw).date()
    except (TypeError, ValueError):
        return False
    return end <= freeze


def _history_row(period_end: str, layer: dict[str, Any]) -> dict[str, Any]:
    directions = layer.get("directions") or []
    return {
        "period_end": period_end,
        "layer": layer.get("id"),
        "candidate": layer.get("candidate") or "",
        "verdict": layer.get("verdict") or "",
        "direction": ";".join(directions),
        "n": layer.get("n_closed", layer.get("n", 0)),
        "note": layer.get("verdict") or "",
    }


def _decision_conclusion(
    *,
    baseline: Optional[float],
    n_cells: int,
    n_sparse: int,
    n_yellow: int,
    verdict: str,
    freeze: bool,
) -> str:
    base = "—" if baseline is None else f"{baseline * 100:.1f}%"
    readable = n_cells - n_sparse
    streak = _streak_clause(verdict, freeze)
    return (
        f"决策核心交叉相对同期 JEV 过滤全样本 up_1h {base}："
        f"可解读格 {readable}、sparse {n_sparse}（不解读）、黄候选 {n_yellow}，"
        f"本期判定 {_verdict_word(verdict)}；{streak}"
    )


def _execution_conclusion(
    *,
    metrics: dict[str, Any],
    giveback: dict[str, Any],
    slip_share: Optional[float],
    hold_axis: dict[str, Any],
    size_axis: dict[str, Any],
    n_closed: int,
    verdict: str,
    freeze: bool,
    bleed: str,
    slippage_frozen: bool,
) -> str:
    hold_note = ">2h 本周无样本"
    for bucket in hold_axis.get("buckets") or []:
        if bucket.get("bucket") != ">2h":
            continue
        if int(bucket["metrics"]["n"]) == 0 and bucket.get("empty_by_design"):
            hold_note = ">2h 本周无样本（empty_by_design）"
        elif int(bucket["metrics"]["n"]) == 0:
            hold_note = ">2h 本周无样本"
        else:
            hold_note = f">2h n={bucket['metrics']['n']}"
    size_note = _size_summary(size_axis.get("buckets") or [], n_closed).replace("；边缘桶折叠。", "")
    gt2 = _pct_text(giveback.get("gt_2x_share"))
    slip = "—" if slip_share is None else f"{slip_share * 100:.1f}%"
    slip_note = "入场 meta，黄冻结只建基线" if slippage_frozen else "入场 meta"
    streak = _streak_clause(verdict, freeze)
    return (
        f"本周退出结构：止损 share（dashboard）{_pct_text(metrics.get('stop_loss_share'))} / "
        f"占负贡献 {_pct_text(metrics.get('stop_loss_share_of_neg'))}；"
        f"shadow 可算 giveback/peak >2x 占比 {gt2}"
        f"（{giveback.get('gt_2x', 0)}/{giveback.get('computable', 0)}）；"
        f"滑点>15% 占比 {slip}（{slip_note}）——"
        f"{bleed}；hold_duration {hold_note}；"
        f"size_band {size_note}。"
        f"判定 {_verdict_word(verdict)}，{streak}"
        "旁证≠改闸；#71≠#72。"
    )


def _streak_clause(verdict: str, freeze: bool) -> str:
    if verdict == "red":
        if freeze:
            return "连续 2 周同向，升复核单；冻结期内只建议，不落地改 TP/SL/hold。"
        return "连续 2 周同向，升复核单；只建议，不改闸、不改 TP/SL/hold。"
    if verdict == "yellow":
        if freeze:
            return "单周不算数，不升红；冻结期内只建议，不改 TP/SL/hold。"
        return "单周不算数，不升红。"
    return "未触黄。"


def _verdict_word(verdict: str) -> str:
    if verdict == "green":
        return "绿"
    if verdict == "yellow":
        return "黄"
    if verdict == "red":
        return "红"
    return verdict


def _pct_text(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _age_coverage(rows: list[SignalChainRow]) -> dict[str, Any]:
    counts = {label: 0 for label in _COARSE_AGE_LABELS}
    for row in rows:
        hours = resolve_token_age_hours(row)
        label = "unknown" if hours is None else _coarse_age_bucket(hours)
        counts[label] = counts.get(label, 0) + 1
    return {
        "resolver": "review.attribution.resolve_token_age_hours",
        "population": "signal_chain",
        "n": len(rows),
        "buckets": counts,
        "unknown": counts.get("unknown", 0),
    }


def _fine_age_coverage(rows: list[SignalChainRow]) -> list[dict[str, Any]]:
    counts = {label: 0 for label in _FINE_AGE_LABELS}
    for row in rows:
        counts[_fine_age(resolve_token_age_hours(row))] += 1
    total = len(rows)
    return [
        {
            "bucket": label,
            "n": counts[label],
            "coverage": (counts[label] / total) if total else None,
        }
        for label in _FINE_AGE_LABELS
    ]


def _fine_age(hours: Optional[float]) -> str:
    """Coverage label on hours already resolved. Not a second timestamp parser."""
    if hours is None:
        return "unknown"
    if hours < 1:
        return "<1h"
    if hours < 24:
        return "1-24h"
    if hours <= 7 * 24:
        return "1-7d"
    return ">7d"


def _up_rate(rows: list[SignalChainRow]) -> tuple[Optional[float], int]:
    values = [row.fwd_1h for row in rows if row.fwd_1h is not None]
    if not values:
        return None, 0
    return sum(1 for value in values if value > 0) / len(values), len(values)


def _unique_tokens(rows: list[SignalChainRow]) -> int:
    return len({(row.chain_id, row.token_address) for row in rows if row.token_address})


def _exit_bucket(reason: str) -> str:
    if reason in _EXIT_REASONS:
        return reason
    return "unknown"


def _daily_bucket(pos: _Pos, day_count: dict[str, int]) -> str:
    if not pos.opened_at:
        return "unknown"
    day = _hk_day(pos.opened_at)
    if day is None or day not in day_count:
        return "unknown"
    count = day_count[day]
    if count <= 5:
        return "1-5"
    if count <= 10:
        return "6-10"
    if count <= 19:
        return "11-19"
    return "≥20"


def _peak_bucket(pos: _Pos) -> str:
    if not pos.has_shadow:
        return "no_shadow"
    peak = pos.peak_pnl_pct
    if peak is None or peak <= 0:
        return "peak_nonpositive"
    give = pos.giveback_from_peak_pct
    if give is None or peak == 0:
        return "ratio_unavailable"
    ratio = give / peak
    if ratio < 1:
        return "<1x"
    if ratio <= 2:
        return "1-2x"
    return ">2x"


def _slip_bucket(pct: Optional[float]) -> str:
    if pct is None:
        return "unknown"
    if pct < 5:
        return "<5%"
    if pct <= 15:
        return "5-15%"
    return ">15%"


def _size_bucket(size: Optional[float]) -> str:
    if size is None:
        return "unknown"
    if size < 250:
        return "<$250"
    if size <= 500:
        return "$250-500"
    return ">$500"


def _hold_bucket(minutes: Optional[float]) -> str:
    if minutes is None:
        return "unknown"
    if minutes < 30:
        return "<30m"
    if minutes <= 120:
        return "30m-2h"
    return ">2h"


def _hold_minutes(pos: _Pos) -> Optional[float]:
    if not pos.opened_at or not pos.closed_at:
        return None
    try:
        return (parse_ts(pos.closed_at) - parse_ts(pos.opened_at)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


def _gt2h_empty_by_design(closed: list[_Pos]) -> bool:
    """True when max_hold caps every close at or below 2h, so >2h cannot occur."""
    caps = [pos.max_hold_min for pos in closed if pos.max_hold_min is not None]
    if not caps or any(cap > 120 for cap in caps):
        return False
    for pos in closed:
        minutes = _hold_minutes(pos)
        if minutes is not None and minutes > 120:
            return False
    return True


def _hk_day(ts: str) -> Optional[str]:
    try:
        return parse_ts(ts).astimezone(_HK).date().isoformat()
    except (TypeError, ValueError):
        return None


def _in_window(ts: Optional[str], start: datetime, end: datetime) -> bool:
    if not ts:
        return False
    try:
        stamp = parse_ts(ts)
    except (TypeError, ValueError):
        return False
    return start <= stamp < end


def _parse_meta(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    if not text:
        return {}
    try:
        obj = json.loads(str(text))
    except (json.JSONDecodeError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _slippage_pct(meta: dict[str, Any]) -> Optional[float]:
    """Entry slippage. Fractions ≤1 become percent (0.02 → 2)."""
    value = safe_float(meta.get("slippage"))
    extras = meta.get("extras")
    if value is None and isinstance(extras, dict):
        value = safe_float(extras.get("slippage"))
    if value is None:
        return None
    magnitude = abs(value)
    if magnitude <= 1:
        return magnitude * 100.0
    return magnitude


def _strategy_of(meta: dict[str, Any]) -> str:
    sid = meta.get("strategy_id")
    if sid is None:
        return "(unknown)"
    text = str(sid).strip()
    return text or "(unknown)"


def _load_positions(
    ctx: _Context,
) -> Optional[tuple[list[_Pos], dict[str, int], int]]:
    """Read paper closes and shadow peaks. None when the positions table is unusable."""
    try:
        start = parse_ts(ctx.period_start)
        end = parse_ts(ctx.period_end)
    except (TypeError, ValueError):
        return None
    try:
        conn = connect_ro(ctx.paths.paper)
    except (FileNotFoundError, sqlite3.Error):
        return None
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "positions" not in tables:
            return None
        cols = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(positions)").fetchall()
        }
        wanted = [
            name
            for name in (
                "id",
                "status",
                "pnl_usd",
                "exit_reason",
                "closed_at",
                "opened_at",
                "size_usd",
                "max_hold_min",
                "meta_json",
            )
            if name in cols
        ]
        if "id" not in wanted:
            return None
        raw_rows = conn.execute(
            f"SELECT {', '.join(wanted)} FROM positions"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    shadow = _load_shadow(ctx.paths.execution)
    closed: list[_Pos] = []
    day_count: dict[str, int] = defaultdict(int)
    open_count = 0
    for row in raw_rows:
        keys = set(row.keys())
        meta = _parse_meta(row["meta_json"] if "meta_json" in keys else None)
        sid = _strategy_of(meta)
        if ctx.strategy_id and sid != ctx.strategy_id:
            continue
        status = str(row["status"]).strip().lower() if "status" in keys and row["status"] else ""
        opened = str(row["opened_at"]) if "opened_at" in keys and row["opened_at"] else None
        closed_at = str(row["closed_at"]) if "closed_at" in keys and row["closed_at"] else ""
        if status == "open":
            open_count += 1
        if opened and _in_window(opened, start, end):
            day = _hk_day(opened)
            if day:
                day_count[day] += 1
        is_closed = status == "closed" or (not status and bool(closed_at))
        if not is_closed or not _in_window(closed_at, start, end):
            continue
        pid = str(row["id"])
        shadow_row = shadow.get(pid)
        reason = ""
        if "exit_reason" in keys and row["exit_reason"] is not None:
            reason = str(row["exit_reason"]).strip()
        closed.append(
            _Pos(
                position_id=pid,
                status=status or "closed",
                pnl_usd=safe_float(row["pnl_usd"]) if "pnl_usd" in keys else None,
                exit_reason=reason,
                closed_at=closed_at,
                opened_at=opened,
                size_usd=safe_float(row["size_usd"]) if "size_usd" in keys else None,
                max_hold_min=safe_float(row["max_hold_min"]) if "max_hold_min" in keys else None,
                slippage_pct=_slippage_pct(meta),
                strategy_id=sid,
                has_shadow=shadow_row is not None,
                peak_pnl_pct=None if shadow_row is None else shadow_row.get("peak_pnl_pct"),
                giveback_from_peak_pct=(
                    None if shadow_row is None else shadow_row.get("giveback_from_peak_pct")
                ),
            )
        )
    return closed, dict(day_count), open_count


def _load_shadow(path: str) -> dict[str, dict[str, Optional[float]]]:
    try:
        conn = connect_ro(path)
    except (FileNotFoundError, sqlite3.Error):
        return {}
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "shadow_c3" not in tables:
            return {}
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(shadow_c3)").fetchall()}
        if "position_id" not in cols:
            return {}
        if "peak_pnl_pct" not in cols and "giveback_from_peak_pct" not in cols:
            return {}
        selected = ["position_id"]
        if "id" in cols:
            selected.append("id")
        if "peak_pnl_pct" in cols:
            selected.append("peak_pnl_pct")
        if "giveback_from_peak_pct" in cols:
            selected.append("giveback_from_peak_pct")
        order = " ORDER BY id ASC" if "id" in cols else ""
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM shadow_c3{order}"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, dict[str, Optional[float]]] = {}
    for row in rows:
        pid = str(row["position_id"] or "").strip()
        if not pid:
            continue
        keys = set(row.keys())
        out[pid] = {
            "peak_pnl_pct": safe_float(row["peak_pnl_pct"]) if "peak_pnl_pct" in keys else None,
            "giveback_from_peak_pct": (
                safe_float(row["giveback_from_peak_pct"])
                if "giveback_from_peak_pct" in keys
                else None
            ),
        }
    return out


def _assert_execution_closed(layer: Optional[dict[str, Any]]) -> None:
    if not layer:
        return
    bad = _forbidden_keys(layer)
    if bad:
        raise RuntimeError(f"execution layer forbids fields {sorted(bad)}")


def _forbidden_keys(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        found.update(FORBIDDEN_EXECUTION_FIELDS.intersection(obj))
        for value in obj.values():
            found.update(_forbidden_keys(value))
    elif isinstance(obj, list):
        for value in obj:
            found.update(_forbidden_keys(value))
    return found
