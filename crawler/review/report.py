"""Markdown weekly report renderer for R1 review layer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from review.data import SignalChainRow

HK_TZ = timezone(timedelta(hours=8))


def _pct(rate: Optional[float], digits: int = 1) -> str:
    if rate is None:
        return "—"
    return f"{rate * 100:.{digits}f}%"


def _ret(v: Optional[float], digits: int = 2) -> str:
    """fwd return as fraction → percent string."""
    if v is None:
        return "—"
    return f"{v * 100:+.{digits}f}%"


def _num(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _chg_pct(v: Optional[float], digits: int = 2) -> str:
    """indicator_history change_* already in percent units."""
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def _now_hk() -> str:
    return datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M HKT")


def _metrics_row(label: str, m: dict[str, Any]) -> str:
    return (
        f"| {label} | {m.get('n', 0)} | {m.get('unique_token', 0)} | "
        f"{m.get('n_1h', 0)} | {_pct(m.get('up_1h_rate'))} | {_ret(m.get('median_1h'))} | "
        f"{m.get('n_4h', 0)} | {_pct(m.get('up_4h_rate'))} | {_ret(m.get('median_4h'))} | "
        f"{_pct(m.get('validation_ok_rate'))} | {_pct(m.get('executed_rate'))} | "
        f"{_pct(m.get('dry_run_rate'))} |"
    )


_TABLE_HEADER = (
    "| 集合 | n | unique_token | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h | "
    "校验通过 | executed | dry_run |\n"
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
)



_ATTR_TABLE_HEADER = (
    "| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |\n"
    "|---|---:|---:|---:|---:|---:|---:|---:|"
)


def _attr_row(label: str, m: dict[str, Any]) -> str:
    return (
        f"| {label} | {m.get('n', 0)} | {m.get('n_1h', 0)} | "
        f"{_pct(m.get('up_1h_rate'))} | {_ret(m.get('median_1h'))} | "
        f"{m.get('n_4h', 0)} | {_pct(m.get('up_4h_rate'))} | {_ret(m.get('median_4h'))} |"
    )


def _render_dim_table(dim_name: str, dim: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    lines.append(f"### {dim_name}")
    lines.append("")
    edges = dim.get("edges")
    if edges:
        lines.append(f"分桶：`{edges}`")
    note = dim.get("note")
    if note:
        lines.append(f"说明：{note}")
    rule = dim.get("rule")
    if rule:
        lines.append(f"规则：{rule}")
    if dim.get("assigned") is not None:
        med = dim.get("baseline_median_change_1h_pct")
        med_s = _chg_pct(med) if med is not None else "—"
        lines.append(f"本期赋值：`{dim.get('assigned')}`（baseline med_1h={med_s}）")
    lines.append("")
    buckets = dim.get("buckets") or {}
    if not buckets:
        lines.append("_无分桶样本。_")
        lines.append("")
        return lines
    lines.append(_ATTR_TABLE_HEADER)
    for lab, m in buckets.items():
        lines.append(_attr_row(lab, m))
    lines.append("")
    return lines


def _fmt_scope_med(v: Any) -> str:
    """cex change_24h_pct / med_delta24h_new 已是百分比。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_scope_int(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return "—"


def _render_cex_scope_section(cex_scope: Optional[dict[str, Any]]) -> list[str]:
    """§6d CEX 新币视野：有快照出表，无数据写「暂无数据」，不失败。"""
    lines: list[str] = []
    lines.append("### 6d. CEX 新币视野监控（只观察）")
    lines.append("")
    lines.append(
        "> 数据：`prior.db.cex_scope_snapshots`（每日 1 条，upsert by `snap_date`）；"
        "「进入视野」= 进入 top50 成交量（近似，非精确上线）；只观察不调权、不进决策。"
    )
    lines.append("")
    payload = cex_scope or {}
    rows = payload.get("rows") or []
    if not payload.get("available") or not rows:
        lines.append("暂无数据")
        lines.append("")
    else:
        lines.append("| 日期 | 累计视野 | 新进 24h | 仍在视野 | 新进者 Δ24h 中位 |")
        lines.append("|---|---:|---:|---:|---:|")
        for row in rows:
            lines.append(
                f"| {row.get('snap_date') or '—'} | {_fmt_scope_int(row.get('n_total'))} | "
                f"{_fmt_scope_int(row.get('n_new_24h'))} | "
                f"{_fmt_scope_int(row.get('n_still_visible'))} | "
                f"{_fmt_scope_med(row.get('med_delta24h_new'))} |"
            )
        lines.append("")
    lines.append(
        "> 用途：CEX 侧新币表现基线，与链上 self_survival 对照；缺数据显示「暂无数据」不失败。"
    )
    lines.append("")
    return lines


def _render_attribution_section(
    attribution: Optional[dict[str, Any]],
    fill_attr: Optional[dict[str, Any]],
    backtest_summary: Optional[dict[str, Any]],
) -> list[str]:
    lines: list[str] = []
    lines.append("## 8. 信号层归因")
    lines.append("")
    # MUST nail caveat
    caveat = (attribution or {}).get("caveat") or (
        "归因观察、样本不足（当前约 40 信号 / 回测 ~0.51d）、不作为调参依据；"
        "不触发任何自动动作"
    )
    lines.append(f"> **Caveat（钉死）：** {caveat}")
    lines.append("")

    if not attribution:
        lines.append("_本期未计算归因。_")
        lines.append("")
    else:
        lines.append(
            f"全样本 n={attribution.get('n', 0)}。"
            "以下为观察性分桶（有 fwd 才计上涨率/中位），**不**改策略参数或闸门。"
        )
        lines.append("")
        dims = attribution.get("dimensions") or {}
        dim_titles = [
            ("strength", "strength（jev_strength）"),
            ("coverage", "coverage（jev_coverage，0–100）"),
            ("liq", "liq（liquidity_at_signal，USD）"),
            ("token_age", "token_age（raw_json 创建时间）"),
            ("market_regime", "market_regime（同期 hot 基准）"),
        ]
        for key, title in dim_titles:
            if key in dims:
                lines.extend(_render_dim_table(title, dims[key]))

    # fills subsection
    lines.append("### 成交归因")
    lines.append("")
    fa = fill_attr or {}
    if (fa.get("note") == "no_fills") or not fa.get("n_fills"):
        lines.append("待模拟盘开闸（当前 paper fills/positions 为空，`note=no_fills`）。")
        lines.append("")
    else:
        lines.append(
            f"n_fills={fa.get('n_fills')}；total_pnl_usd={fa.get('total_pnl_usd')}；"
            f"slippage={fa.get('slippage')}。"
        )
        lines.append("")
        by_s = fa.get("by_strategy") or {}
        if by_s:
            lines.append("| strategy | n | pnl_usd |")
            lines.append("|---|---:|---:|")
            for sid, m in by_s.items():
                lines.append(f"| `{sid}` | {m.get('n', 0)} | {m.get('pnl_usd', 0)} |")
            lines.append("")

    # optional backtest summary alongside attribution
    lines.append("### 回测对照摘要（只读，可选）")
    lines.append("")
    bt = backtest_summary
    if not bt:
        lines.append("_未挂载回测摘要。_")
        lines.append("")
    elif not bt.get("ok"):
        lines.append(f"_跳过：{bt.get('note', 'backtest unavailable')}_")
        lines.append("")
    else:
        lines.append(
            f"最近 done run：`{bt.get('run_id')}` · strategies=`{bt.get('strategy_id')}` · "
            f"closed={bt.get('n_closed')} · "
            f"equity { _num(bt.get('start_cash')) } → { _num(bt.get('end_equity')) }"
        )
        if bt.get("notes"):
            lines.append(f"notes: {bt.get('notes')}")
        lines.append("")
        by = bt.get("by_strategy") or {}
        if by:
            lines.append("| strategy_id | trades | win_rate | total_pnl_usd |")
            lines.append("|---|---:|---:|---:|")
            for sid, m in by.items():
                lines.append(
                    f"| `{sid}` | {m.get('trades', 0)} | {_pct(m.get('win_rate'))} | "
                    f"{_num(m.get('total_pnl_usd'))} |"
                )
            lines.append("")
        else:
            lines.append("_该 run 无 closed positions。_")
            lines.append("")
    return lines


def render_weekly_md(
    *,
    period_start: str,
    period_end: str,
    chain: list[SignalChainRow],
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    exec_summary: dict[str, Any],
    jev_slice_metrics: Optional[dict[str, Any]] = None,
    jev_slice_note: Optional[str] = None,
    attribution: Optional[dict[str, Any]] = None,
    fill_attr: Optional[dict[str, Any]] = None,
    backtest_summary: Optional[dict[str, Any]] = None,
    optimize_section: Optional[str] = None,
    evolution_suggestions: Optional[list[dict[str, Any]]] = None,
    cex_scope: Optional[dict[str, Any]] = None,
) -> str:
    """返回 markdown 字符串（§1–8 + §6d 只观察 + 可选§9 R3 + §10 进化建议）。"""
    n = len(chain)
    n_exec = sum(1 for r in chain if r.exec_id is not None)
    n_pos = sum(1 for r in chain if r.position_id is not None)
    n_fwd1 = sum(1 for r in chain if r.fwd_1h is not None)
    n_fwd4 = sum(1 for r in chain if r.fwd_4h is not None)
    strategies = sorted({r.strategy_id for r in chain})

    lines: list[str] = []
    lines.append("# 复盘周报 · R1 信号层绩效 + R2 归因")
    lines.append("")
    lines.append(f"**生成时间:** {_now_hk()}")
    lines.append(f"**期间:** `{period_start}` ≤ ts < `{period_end}` (UTC)")
    lines.append("**范围:** 信号 → 校验/执行留痕 →（可选）成交；只读四库，零侵入")
    lines.append("")

    # 1. 期间与数据覆盖
    lines.append("## 1. 期间与数据覆盖")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| decision 链路行数 | **{n}** |")
    lines.append(f"| 有 exec_id | **{n_exec}** ({_pct(n_exec / n if n else None)}) |")
    lines.append(f"| 有 position | {n_pos} |")
    lines.append(f"| 有 fwd_1h / fwd_4h | {n_fwd1} / {n_fwd4} |")
    lines.append(f"| 策略集合 | {', '.join(f'`{s}`' for s in strategies) or '—'} |")
    lines.append(
        f"| exec 重复 decision 数 | {exec_summary.get('exec_dup_count', 0)} |"
    )
    lines.append("")

    # 2. 全策略
    lines.append("## 2. 全策略信号绩效（1h / 4h）")
    lines.append("")
    lines.append("前向收益口径：`pair_history` 就近价格，容差默认 ±1800s（对齐 `jev_validity`）。")
    lines.append("")
    lines.append(_TABLE_HEADER)
    lines.append(_metrics_row("全策略", metrics))
    lines.append("")

    # 3. 分策略
    lines.append("## 3. 分策略对比")
    lines.append("")
    by_strategy = metrics.get("by_strategy") or {}
    if not by_strategy:
        lines.append("_本期无分策略样本。_")
    else:
        lines.append(_TABLE_HEADER)
        for sid, m in by_strategy.items():
            lines.append(_metrics_row(sid, m))
    lines.append("")

    # 4. JEV 切片
    lines.append("## 4. JEV 过滤切片（对齐周检过滤后）")
    lines.append("")
    if jev_slice_note:
        lines.append(f"切片：{jev_slice_note}。")
    else:
        lines.append(
            "切片：`strategy_id=jev_strength_v1` ∩ `action=buy` ∩ `jev_strength≥0.15` "
            "∩ `liq≥50k` ∩ `coverage≥10` ∩ `price≥1e-6`（对齐 `jev_validity.py` 过滤后口径）。"
        )
    lines.append(
        "数据来源：liq=history@t0±tol（缺则 raw_json 快照）；coverage=`jev_signals.coverage_pct`"
        "（缺则 `token_indicators.jev_confidence*100`）。"
    )
    lines.append("")
    if jev_slice_metrics is None:
        lines.append("_无 JEV 切片样本。_")
    else:
        lines.append(_TABLE_HEADER)
        lines.append(_metrics_row("JEV≥0.15", jev_slice_metrics))
        lines.append("")
        lines.append(
            f"对账提示：周检过滤后 1h/4h 上涨率见 "
            f"`DECISION_ENGINE_JEV_VALIDITY_FILTERED.md`（约 71.4% / 57.1%，n=14）；"
            f"本切片 up_1h={_pct(jev_slice_metrics.get('up_1h_rate'))}，"
            f"up_4h={_pct(jev_slice_metrics.get('up_4h_rate'))}，"
            f"n={jev_slice_metrics.get('n', 0)}。"
        )
    lines.append("")

    # 5. 执行层
    lines.append("## 5. 执行层状态")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 有执行留痕 | {exec_summary.get('n_with_exec', 0)} / {exec_summary.get('n', 0)} |")
    lines.append(f"| 校验通过率 | {_pct(exec_summary.get('validation_ok_rate'))} |")
    lines.append(f"| executed 率 | {_pct(exec_summary.get('executed_rate'))} |")
    lines.append(f"| dry_run 占比 | {_pct(exec_summary.get('dry_run_rate'))} |")
    lines.append(f"| exec_dup_count | {exec_summary.get('exec_dup_count', 0)} |")
    lines.append("")
    dist = exec_summary.get("executor_dist") or {}
    if dist:
        lines.append("### executor 分布")
        lines.append("")
        lines.append("| executor | n |")
        lines.append("|---|---:|")
        for k, v in dist.items():
            lines.append(f"| `{k}` | {v} |")
        lines.append("")
    buckets = exec_summary.get("validation_error_buckets") or {}
    if buckets:
        lines.append("### validation_errors 分桶")
        lines.append("")
        lines.append("| bucket | n |")
        lines.append("|---|---:|")
        for k, v in buckets.items():
            lines.append(f"| `{k}` | {v} |")
        lines.append("")
    else:
        lines.append("_本期无校验失败分桶（或 validation_ok 全通过）。_")
        lines.append("")

    # 6. 市场基准
    lines.append("## 6. 市场基准（同期 hot）")
    lines.append("")
    lines.append(
        "`indicator_history` 中 `tier='hot'` 的 `change_1h` / `change_4h`（已为百分比单位）。"
        "截面波动大，优先看中位数与 winsor(|x|≤500)。"
    )
    lines.append("")
    lines.append("| 指标 | 原始 | winsor(|x|≤500) |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| n / n_1h / n_4h | {baseline.get('n', 0)} / {baseline.get('n_1h', 0)} / "
        f"{baseline.get('n_4h', 0)} | "
        f"— / {baseline.get('n_1h_winsor', 0)} / {baseline.get('n_4h_winsor', 0)} |"
    )
    lines.append(
        f"| median change_1h | {_chg_pct(baseline.get('median_change_1h'))} | "
        f"{_chg_pct(baseline.get('median_change_1h_winsor'))} |"
    )
    lines.append(
        f"| median change_4h | {_chg_pct(baseline.get('median_change_4h'))} | "
        f"{_chg_pct(baseline.get('median_change_4h_winsor'))} |"
    )
    lines.append(
        f"| avg change_1h | {_chg_pct(baseline.get('avg_change_1h'))} | "
        f"{_chg_pct(baseline.get('avg_change_1h_winsor'))} |"
    )
    lines.append(
        f"| avg change_4h | {_chg_pct(baseline.get('avg_change_4h'))} | "
        f"{_chg_pct(baseline.get('avg_change_4h_winsor'))} |"
    )
    lines.append("")
    lines.extend(_render_cex_scope_section(cex_scope))

    # 7. 缺口
    lines.append("## 7. 缺口与下一步")
    lines.append("")
    lines.append("- **成交层空：** paper `positions`/`fills` 仍为 0；`pnl_usd` / fill 字段全 None，属预期（开闸前）。")
    lines.append(
        "- **executed=0：** 现网 `executor=step_a_noop`，校验可通过但未实成交；R1 报告仍完整。"
    )
    lines.append(
        "- **策略覆盖：** 当前库仅见 `jev_strength_v1`；B/C（`tech_momentum_v1` / "
        "`combined_jev_tech_v1`）待信号写入后分桶自动出现。"
    )
    lines.append(
        "- **R2 半块：** 第 8 节信号层归因已落地（只读分桶）；样本不足，不作为调参依据，不触发自动动作。"
    )
    lines.append(
        "- **回测对照：** 第 8 节旁挂最近 done run 摘要（只读）；失败则跳过并注明。"
    )
    lines.append(
        "- **R3 寻优：** 第 9 节为脚手架占位；历史≥7d 且周检跨门槛后再正式寻优；不自动改参（R4）。"
    )
    lines.append(
        "- **进化建议：** 第 10 节只观察/建议，不自动改参或开闸；落地走 R4。"
    )
    lines.append("")

    # 8. 信号层归因（R2）
    lines.extend(_render_attribution_section(attribution, fill_attr, backtest_summary))

    # 9. R3 参数寻优（脚手架占位）
    lines.append("## 9. R3 参数寻优 — 待历史≥7天且周检跨门槛")
    lines.append("")
    if optimize_section:
        lines.append(optimize_section)
    else:
        lines.append(
            "占位：历史不足（现 ~0.51d）或周检尚未跨门槛前不正式寻优；"
            "脚手架见 `review/optimizer.py`（`--optimize` 仅 dry_run / skip）。"
            "**不自动写配置**；正式改参需 R4 人工确认。"
        )
    lines.append("")

    # 10. 进化建议（只建议，不自动改参/开闸）
    lines.append("## 10. 进化建议")
    lines.append("")
    from review.evolution import format_evolution_section

    lines.append(format_evolution_section(evolution_suggestions or []))

    lines.append("---")
    lines.append("")
    lines.append(
        "*R1+R2+R3(scaffold)+§10进化建议 · review/ · 只读 · 报告仅写 exports/reports/*"
    )
    lines.append("")
    return "\n".join(lines)


def write_report(md: str, out_path: str) -> str:
    """写入 exports/reports/…，返回绝对路径。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return str(path.resolve())
