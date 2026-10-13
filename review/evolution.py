"""Section 10: evolution suggestions (read-only observations; never mutates params)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence

from review.data import SignalChainRow
from review.gates import (
    CONSECUTIVE_WEEKS,
    MIN_RAW_N as JEV_N_GATE,
    MIN_UNIQUE_TOKEN as JEV_UNIQUE_TOKEN_GATE,
    UP_1H_DEGRADE as JEV_UP1H_WEAK,
    UP_1H_PROMOTE as JEV_UP1H_PASS,
)
from review.optimizer import (
    MIN_HISTORY_DAYS,
    MIN_SAMPLES,
    OptimizeResult,
    check_optimize_gates,
    estimate_history_days,
)

# status vocabulary (钉死)
STATUS_WATCH = "观察中"
STATUS_SUGGEST = "建议落地"
STATUS_DECIDE = "裁决"

LAYERS = ("采集", "指标", "决策", "执行", "复盘")

def _pct(rate: Optional[float], digits: int = 1) -> str:
    if rate is None:
        return "—"
    return f"{rate * 100:.{digits}f}%"


def _suggestion(
    *,
    layer: str,
    item: str,
    status: str,
    evidence: str,
    action_hint: str,
) -> dict[str, str]:
    return {
        "layer": layer,
        "item": item,
        "status": status,
        "evidence": evidence,
        "action_hint": action_hint,
    }


def _count_news_events(db_root: Path) -> tuple[Optional[int], str]:
    """Return (count, note). count=None if db missing / unreadable."""
    path = db_root / "news_events.db"
    if not path.is_file():
        return None, "news_events.db 不存在（旁路未建或未拉取）"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
            return int(n), f"news_events.db 可读，累计 {n} 条"
        finally:
            conn.close()
    except Exception as e:
        return None, f"news_events.db 打开失败: {e}"


def _read_indicator_weights() -> dict[str, float]:
    """Best-effort import of INDICATORS_WEIGHT_* (default 0)."""
    try:
        import config  # type: ignore

        return {
            "TECHNICAL": float(getattr(config, "INDICATORS_WEIGHT_TECHNICAL", 0) or 0),
            "FUNDING": float(getattr(config, "INDICATORS_WEIGHT_FUNDING", 0) or 0),
            "SENTIMENT": float(getattr(config, "INDICATORS_WEIGHT_SENTIMENT", 0) or 0),
        }
    except Exception:
        return {"TECHNICAL": 0.0, "FUNDING": 0.0, "SENTIMENT": 0.0}


def _token_age_unknown_ratio(attribution: Optional[dict[str, Any]]) -> tuple[int, int]:
    """Return (n_unknown, n_total) from attribution token_age buckets."""
    if not attribution:
        return 0, 0
    dims = attribution.get("dimensions") or {}
    age = dims.get("token_age") or {}
    buckets = age.get("buckets") or {}
    n_unknown = int((buckets.get("unknown") or {}).get("n", 0) or 0)
    n_total = int(attribution.get("n", 0) or 0)
    if n_total <= 0:
        n_total = sum(int((b or {}).get("n", 0) or 0) for b in buckets.values())
    return n_unknown, n_total


def _liq_differentiation_note(attribution: Optional[dict[str, Any]]) -> str:
    """Summarize liq bucket up_1h spread as candidate focus (observation only)."""
    if not attribution:
        return "归因未计算，liq 分层区分度待各层交来"
    buckets = ((attribution.get("dimensions") or {}).get("liq") or {}).get("buckets") or {}
    parts: list[str] = []
    for lab in ("<50k", "50-500k", "≥500k", "unknown"):
        m = buckets.get(lab) or {}
        n = m.get("n", 0)
        rate = m.get("up_1h_rate")
        parts.append(f"{lab}: n={n} up_1h={_pct(rate)}")
    rates = [
        (lab, (buckets.get(lab) or {}).get("up_1h_rate"))
        for lab in ("<50k", "50-500k", "≥500k")
        if (buckets.get(lab) or {}).get("up_1h_rate") is not None
        and (buckets.get(lab) or {}).get("n", 0)
    ]
    spread = ""
    if len(rates) >= 2:
        vals = [r for _, r in rates if r is not None]
        spread = f"；跨桶 up_1h 极差={_pct(max(vals) - min(vals))}"
    return "；".join(parts) + spread


def _jev_gate_distance(jev_slice_metrics: Optional[dict[str, Any]]) -> str:
    if not jev_slice_metrics:
        return "无 JEV 过滤切片样本，门槛距离待决策层交来"
    ut = int(jev_slice_metrics.get("unique_token", 0) or 0)
    n = int(jev_slice_metrics.get("n", 0) or 0)
    up1 = jev_slice_metrics.get("up_1h_rate")
    d_ut = JEV_UNIQUE_TOKEN_GATE - ut
    d_n = JEV_N_GATE - n
    bits = [
        f"过滤后 n={n}（距≥{JEV_N_GATE}：{'已达' if d_n <= 0 else f'差 {d_n}'}）",
        f"unique_token={ut}（距≥{JEV_UNIQUE_TOKEN_GATE}：{'已达' if d_ut <= 0 else f'差 {d_ut}'}）",
        f"up_1h={_pct(up1)}（通过线≥{_pct(JEV_UP1H_PASS)} / 弱线<{_pct(JEV_UP1H_WEAK)}）",
    ]
    return "；".join(bits)


def _r3_skip_evidence(
    *,
    opt_result: Optional[OptimizeResult],
    chain: Sequence[SignalChainRow],
    period_start: Optional[str],
    period_end: Optional[str],
) -> str:
    if opt_result is not None:
        return (
            f"status=`{opt_result.status}`"
            + (f"：{opt_result.reason}" if opt_result.reason else "")
            + f"；history_days={opt_result.history_days}；grid={opt_result.param_grid_size}"
        )
    # synthesize gate check without running full optimize
    history_days = estimate_history_days(
        list(chain), period_start=period_start, period_end=period_end
    )
    ok, reason = check_optimize_gates(
        history_days=history_days,
        n_samples=len(chain),
        min_history_days=MIN_HISTORY_DAYS,
        min_samples=MIN_SAMPLES,
        weekly_check_passed=False,
    )
    if ok:
        return (
            f"闸门可过（history≈{history_days:.2f}d, n={len(chain)}），"
            "但本跑未传 --optimize；正式寻优仍需周检跨门槛且不自动写配置"
        )
    return f"未跑 --optimize；按闸门预检将 skip：{reason}"


def build_evolution_suggestions(
    *,
    chain: Sequence[SignalChainRow],
    metrics: Optional[dict[str, Any]] = None,
    exec_summary: Optional[dict[str, Any]] = None,
    attribution: Optional[dict[str, Any]] = None,
    fill_attr: Optional[dict[str, Any]] = None,
    jev_slice_metrics: Optional[dict[str, Any]] = None,
    opt_result: Optional[OptimizeResult] = None,
    optimize_section: Optional[str] = None,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    db_root: Optional[str | Path] = None,
) -> list[dict[str, str]]:
    """Build structured evolution suggestions for weekly report §10.

    All items default to status=观察中 when auto-filled from read-only data.
    Never mutates strategy params / gates; landing goes through R4.
    """
    root = Path(db_root) if db_root else Path(__file__).resolve().parents[1]
    metrics = metrics or {}
    exec_summary = exec_summary or {}
    fill_attr = fill_attr or {}
    suggestions: list[dict[str, str]] = []

    # --- 采集 ---
    n_unk, n_tot = _token_age_unknown_ratio(attribution)
    if n_tot > 0:
        age_ev = (
            f"归因 token_age 桶 unknown={n_unk}/{n_tot}"
            + ("（创建时间字段缺口，全样本 unknown）" if n_unk == n_tot else "")
        )
    else:
        age_ev = "归因未提供 token_age 分桶，等采集/信号层交来创建时间字段覆盖率"
    suggestions.append(
        _suggestion(
            layer="采集",
            item="token_age 字段缺口",
            status=STATUS_WATCH,
            evidence=age_ev,
            action_hint=(
                "在 crawl/raw_json 补齐 pairCreatedAt（或等价创建时间）；"
                "复盘侧仅观察 unknown 占比下降，不改桶边界"
            ),
        )
    )

    news_n, news_note = _count_news_events(root)
    if news_n is None:
        news_ev = news_note + "；等采集旁路交来后再填条数"
    else:
        news_ev = news_note
    suggestions.append(
        _suggestion(
            layer="采集",
            item="news_events 旁路积累",
            status=STATUS_WATCH,
            evidence=news_ev,
            action_hint=(
                "旁路继续 RSS 入库即可；不接决策/JEV；"
                "链上验证器接入前只记 pending"
            ),
        )
    )

    # --- 指标 ---
    weights = _read_indicator_weights()
    w_sum = sum(weights.values())
    w_ev = (
        f"INDICATORS_WEIGHT_*: TECHNICAL={weights['TECHNICAL']:g} "
        f"FUNDING={weights['FUNDING']:g} SENTIMENT={weights['SENTIMENT']:g} "
        f"（sum={w_sum:g}，全 0 → composite no-op）"
    )
    suggestions.append(
        _suggestion(
            layer="指标",
            item="合成权重仍全 0",
            status=STATUS_WATCH,
            evidence=w_ev,
            action_hint="保持关闸至明确接线验收；开权走 R4，不由复盘自动改 config",
        )
    )

    suggestions.append(
        _suggestion(
            layer="指标",
            item="liq 分层区分度（候选关注）",
            status=STATUS_WATCH,
            evidence=_liq_differentiation_note(attribution),
            action_hint=(
                "仅作候选关注：样本不足时不据此调参；"
                "若跨桶极差持续且 unique_token 过闸，再交 R3/R4 评估过滤权重"
            ),
        )
    )

    # --- 决策 ---
    suggestions.append(
        _suggestion(
            layer="决策",
            item="JEV unique_token / 门槛距离",
            status=STATUS_WATCH,
            evidence=_jev_gate_distance(jev_slice_metrics),
            action_hint=(
                f"继续周检攒样本；连续 {CONSECUTIVE_WEEKS} 周过滤后 "
                f"1h≥{_pct(JEV_UP1H_PASS, 0)} 且 unique_token≥{JEV_UNIQUE_TOKEN_GATE} "
                f"（且 n≥{JEV_N_GATE}）后再启动 Step2；本层只观察"
            ),
        )
    )

    suggestions.append(
        _suggestion(
            layer="决策",
            item="策略退役规则提醒",
            status=STATUS_WATCH,
            evidence=(
                f"规则（决策表）：连续 {CONSECUTIVE_WEEKS} 周过滤后 "
                f"1h<{_pct(JEV_UP1H_WEAK, 0)} 且 unique_token≥{JEV_UNIQUE_TOKEN_GATE} → "
                "建议降级/停用，勿进 R3 无限调参。"
                f"本期：{_jev_gate_distance(jev_slice_metrics)}。规则只提醒不裁决"
            ),
            action_hint="周检写入决策表时核对此条；退役=止损，调参=修补；落地需 R4",
        )
    )

    # --- 执行 ---
    n_exec = int(exec_summary.get("n_with_exec", 0) or 0)
    executed_rate = exec_summary.get("executed_rate")
    dry_rate = exec_summary.get("dry_run_rate")
    dist = exec_summary.get("executor_dist") or {}
    dist_s = ", ".join(f"{k}={v}" for k, v in dist.items()) or "—"
    fill_note = fill_attr.get("note") or (
        "no_fills" if not fill_attr.get("n_fills") else "has_fills"
    )
    exec_ev = (
        f"executed_rate={_pct(executed_rate)}；dry_run={_pct(dry_rate)}；"
        f"with_exec={n_exec}/{exec_summary.get('n', len(chain))}；"
        f"executor分布=[{dist_s}]；paper fills note={fill_note}"
    )
    suggestions.append(
        _suggestion(
            layer="执行",
            item="paper / executed 状态",
            status=STATUS_WATCH,
            evidence=exec_ev,
            action_hint="开闸与换 executor 属执行层/R4，复盘不自动开闸",
        )
    )

    buckets = exec_summary.get("validation_error_buckets") or {}
    if buckets:
        rej_ev = "；".join(f"{k}={v}" for k, v in buckets.items())
        rej_hint = "分桶仅观察；改校验规则走执行层变更 + R4，不由本节自动改"
    else:
        rej_ev = "本期无 validation_errors 分桶（或全 validation_ok）"
        rej_hint = "保持观察；若后续出现拒绝集中，再交执行层排查"
    suggestions.append(
        _suggestion(
            layer="执行",
            item="拒绝分布",
            status=STATUS_WATCH,
            evidence=rej_ev,
            action_hint=rej_hint,
        )
    )

    # --- 复盘 ---
    r3_ev = _r3_skip_evidence(
        opt_result=opt_result,
        chain=chain,
        period_start=period_start,
        period_end=period_end,
    )
    if optimize_section and opt_result is None:
        r3_ev = r3_ev + f"；report§9={optimize_section[:120]}"
    suggestions.append(
        _suggestion(
            layer="复盘",
            item="R3 skip 原因",
            status=STATUS_WATCH,
            evidence=r3_ev,
            action_hint=(
                "历史≥7d 且周检跨门槛后再正式寻优；"
                "本节只建议，不自动改参/开闸；落地走 R4"
            ),
        )
    )

    return suggestions


def format_evolution_section(suggestions: Sequence[dict[str, str]]) -> str:
    """Markdown body for §10 (without the ## heading)."""
    lines: list[str] = []
    lines.append(
        "> **钉死：** 本节只建议，**不**自动改参 / 开闸 / 写配置；"
        "任何落地须走 **R4 人工确认**。"
    )
    lines.append("")
    if not suggestions:
        lines.append("_本期无进化建议（各层数据未交来）。_")
        lines.append("")
        return "\n".join(lines)

    lines.append("| 层 | 项 | 状态 | 证据 | 行动提示 |")
    lines.append("|---|---|---|---|---|")
    for s in suggestions:
        lines.append(
            f"| {s.get('layer', '')} | {s.get('item', '')} | "
            f"**{s.get('status', '')}** | {s.get('evidence', '')} | "
            f"{s.get('action_hint', '')} |"
        )
    lines.append("")
    lines.append(
        f"共 {len(suggestions)} 条；状态词汇：`观察中` / `建议落地` / `裁决`。"
        "自动填充项默认为「观察中」。"
    )
    lines.append("")
    return "\n".join(lines)
