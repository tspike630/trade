"""Markdown weekly report renderer for R1 review layer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from review.data import SignalChainRow
from review.evolution import format_evolution_section

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



_JOINT_TABLE_HEADER = (
    "| token_age | n | up_1h | med_1h | up_4h | 全网存活率(先验) | 备注 |\n"
    "|---|---:|---:|---:|---:|---:|---|"
)


def _joint_row(label: str, m: dict[str, Any]) -> str:
    prior_disp = m.get("prior_display") or "N/A"
    reason = m.get("prior_reason")
    note = reason if reason else (m.get("prior_label") or "—")
    # shorten long reasons for table
    if isinstance(note, str) and len(note) > 80:
        note = note[:77] + "…"
    return (
        f"| {label} | {m.get('n', 0)} | "
        f"{_pct(m.get('up_1h') if m.get('up_1h') is not None else m.get('up_1h_rate'))} | "
        f"{_ret(m.get('med_1h') if m.get('med_1h') is not None else m.get('median_1h'))} | "
        f"{_pct(m.get('up_4h') if m.get('up_4h') is not None else m.get('up_4h_rate'))} | "
        f"{prior_disp} | {note} |"
    )


def _render_joint_section(joint: Optional[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    lines.append("### JEV×币龄×先验（细桶联合表）")
    lines.append("")
    if not joint:
        lines.append("_本期未计算联合表。_")
        lines.append("")
        return lines

    caveat = joint.get("caveat") or (
        "联合归因观察：n 仍小；不作为调参依据；不调权"
    )
    lines.append(f"> **Caveat：** {caveat}")
    lines.append("")
    src = joint.get("bucket_source") or "computed"
    lines.append(f"分桶源=`{src}`")
    if joint.get("prior_tags_period"):
        lines.append(
            f"prior_tags 本期覆盖：`{joint.get('prior_tags_period')}`"
            + (f"（表总行={joint.get('prior_tags_n')}）" if joint.get("prior_tags_n") is not None else "")
        )
    lines.append(f"细桶：`{joint.get('edges')}`")
    if joint.get("hypothesis"):
        lines.append(f"假说：{joint.get('hypothesis')}")
    if joint.get("note"):
        lines.append(f"说明：{joint.get('note')}")
    mismatches = joint.get("mismatch_notes") or []
    if mismatches:
        lines.append(f"交叉校验不一致：{len(mismatches)} 条（prior_tagger vs 现算 token_age）")
        for m in mismatches[:5]:
            lines.append(f"- {m}")
        if len(mismatches) > 5:
            lines.append(f"- …共 {len(mismatches)} 条")
    pm = joint.get("prior_meta") or {}
    if pm:
        bits = []
        if pm.get("report"):
            bits.append(f"报告=`{pm.get('report')}`")
        if pm.get("source"):
            bits.append(f"源=`{pm.get('source')}`")
        if pm.get("crawler_window_h") is not None:
            bits.append(f"crawler窗≈{pm.get('crawler_window_h')}h")
        if pm.get("n") is not None:
            bits.append(f"self_db n={pm.get('n')}")
        if bits:
            lines.append("先验旁挂：" + " · ".join(bits))
    lines.append("")

    for key, title in (
        ("jev_filtered", "过滤后 JEV 切片（周检同口径）"),
        ("full_sample", "全样本"),
    ):
        block = joint.get(key) or {}
        lines.append(f"#### {title}（n={block.get('n', 0)}）")
        lines.append("")
        buckets = block.get("buckets") or {}
        if not buckets:
            lines.append("_无分桶样本。_")
            lines.append("")
            continue
        lines.append(_JOINT_TABLE_HEADER)
        for lab in joint.get("labels") or buckets.keys():
            if lab in buckets:
                lines.append(_joint_row(lab, buckets[lab]))
        lines.append("")

    lines.append(
        "口径提醒：全网存活率来自 self_db 观测跨度（`last_seen−first_seen > t`），"
        "不是链上真实寿命；窗不足时写 N/A + 原因，勿把 0% 当真死亡。"
    )
    lines.append("")
    return lines


def _render_attribution_section(
    attribution: Optional[dict[str, Any]],
    fill_attr: Optional[dict[str, Any]],
    backtest_summary: Optional[dict[str, Any]],
    joint: Optional[dict[str, Any]] = None,
) -> list[str]:
    lines: list[str] = []
    lines.append("## 8. 信号层归因")
    lines.append("")
    # MUST nail caveat
    caveat = (attribution or {}).get("caveat") or (
        "归因观察、样本不足时不作为调参依据；不触发任何自动动作"
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
        if attribution.get("bucket_source"):
            lines.append(
                f"token_age 分桶源=`{attribution.get('bucket_source')}`"
                + (
                    f"（prior_tags={attribution.get('prior_tags_period')}）"
                    if attribution.get("prior_tags_period")
                    else ""
                )
            )
        lines.append("")
        dims = attribution.get("dimensions") or {}
        dim_titles = [
            ("strength", "strength（jev_strength）"),
            ("coverage", "coverage（jev_coverage，0–100）"),
            ("liq", "liq（liquidity_at_signal，USD）"),
            ("token_age", "token_age（pairs.pair_created_at）"),
            ("market_regime", "market_regime（同期 hot 基准）"),
        ]
        for key, title in dim_titles:
            if key in dims:
                lines.extend(_render_dim_table(title, dims[key]))

    # joint JEV × token_age_fine × prior
    lines.extend(_render_joint_section(joint))

    # fills subsection
    lines.append("### 成交归因")
    lines.append("")
    fa = fill_attr or {}
    if (fa.get("note") == "no_fills") or not fa.get("n_fills"):
        lines.append(
            "本期无匹配成交（`note=no_fills`）。"
            "已实现 PnL 只在 closed `positions.pnl_usd`；"
            "信号层 mark-to-market 见第 2 节前向收益，两者不要混读。"
        )
        lines.append("")
    else:
        lines.append(
            f"n_fills={fa.get('n_fills')}；n_closed={fa.get('n_closed', 0)}；"
            f"n_open={fa.get('n_open', 0)}；"
            f"已实现 PnL（realized）={fa.get('realized_pnl_usd', fa.get('total_pnl_usd'))}；"
            f"未平仓盯市（mtm，不并进已实现）={fa.get('mtm_pnl_usd')}；"
            f"slippage={fa.get('slippage')}。"
        )
        lines.append(f"{fa.get('mtm_note') or ''}")
        lines.append("")
        by_s = fa.get("by_strategy") or {}
        if by_s:
            lines.append("| strategy | n | realized_pnl_usd | mtm_pnl_usd |")
            lines.append("|---|---:|---:|---:|")
            for sid, m in by_s.items():
                lines.append(
                    f"| `{sid}` | {m.get('n', 0)} | "
                    f"{_num(m.get('realized_pnl_usd', m.get('pnl_usd')))} | "
                    f"{_num(m.get('mtm_pnl_usd'))} |"
                )
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
        lines.append(
            "PnL 口径：下表为回测已实现（closed `positions.pnl_usd`），"
            "不是第 2 节信号 mark-to-market 前向收益。"
        )
        lines.append("")
        by = bt.get("by_strategy") or {}
        if by:
            lines.append("| strategy_id | trades | win_rate | realized_pnl_usd |")
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


def _render_factor_layer_section(
    factor_layers: Optional[dict[str, Any]],
) -> list[str]:
    """§11 分位数分层 + rank IC + 绩效矩阵。None → 不输出（默认关，周报逐字节不变）。

    §8 已是信号层归因，这里用下一个空号 §11，不覆盖既有章节。
    """
    if factor_layers is None:
        return []
    lines: list[str] = []
    lines.append("")
    lines.append("## 11. 因子分层（分位数 Q1-Q5 + rank IC）")
    lines.append("")
    lines.append(
        "> **Caveat（钉死）：** 观察用；样本 <10 的因子不出表；IC/单调性不作调参依据；不触发任何自动动作。"
    )
    lines.append("")
    layers = factor_layers.get("layers") or {}
    if not layers:
        lines.append("_本期无足够样本的因子。_")
        lines.append("")
    for fname, res in sorted(layers.items()):
        lines.append(
            f"### factor: `{fname}`（n={res.get('n', 0)}，rank IC={_num(res.get('ic'), 3)}，{res.get('monotonic', '')}）"
        )
        lines.append("")
        lines.append("| 层 | n | mean_ret_1h | up_1h | median_ret_1h |")
        lines.append("|---|---:|---:|---:|---:|")
        for layer in res.get("layers") or []:
            lines.append(
                f"| {layer['layer']} | {layer['n']} | {_ret(layer.get('mean_ret'))} | "
                f"{_pct(layer.get('up_rate'))} | {_ret(layer.get('median_ret'))} |"
            )
        lines.append("")
    matrix = factor_layers.get("matrix") or []
    if matrix:
        lines.append("### 绩效矩阵（策略 × 指标）")
        lines.append("")
        lines.append("| strategy | n | up_1h | med_1h | up_4h | med_4h | 校验通过 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for row in matrix:
            lines.append(
                f"| `{row['strategy']}` | {row['n']} | {_pct(row.get('up_1h'))} | "
                f"{_ret(row.get('med_1h'))} | {_pct(row.get('up_4h'))} | "
                f"{_ret(row.get('med_4h'))} | {_pct(row.get('validation_ok'))} |"
            )
        lines.append("")
    lines.append("> 只观察不调权；分层结论待样本累计；落地须走 R4。")
    lines.append("")
    ic_hist = factor_layers.get("ic_history") or {}
    summary = ic_hist.get("summary") or {}
    if summary:
        lines.append("### IC 历史序列（多期，因子有效性）")
        lines.append("")
        lines.append(
            "> **Caveat（钉死）：** IC mean/IR 观察用；单期 IC 噪音大，连续多期 IR≥0.5 才谈「有效候选」；不触发任何自动动作。"
        )
        lines.append("")
        lines.append("| factor | 期数 | IC mean | IC std | IR | 稳定性 |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for fname, s in sorted(summary.items()):
            lines.append(
                f"| `{fname}` | {s['n_periods']} | {_num(s.get('ic_mean'), 3)} | "
                f"{_num(s.get('ic_std'), 3)} | {_num(s.get('ir'), 2)} | {s.get('stability', '')} |"
            )
        lines.append("")
        lines.append(f"> 历史文件：`{ic_hist.get('path', '')}`（幂等追加，同期重跑覆盖）")
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
    joint: Optional[dict[str, Any]] = None,
    optimize_section: Optional[str] = None,
    evolution_suggestions: Optional[list[dict[str, Any]]] = None,
    factor_layers: Optional[dict[str, Any]] = None,
) -> str:
    """返回 markdown 字符串（§1–8 + 可选§9 R3 + §10 进化建议 + 可选§11 因子分层 + §12 因子进化指针）。"""
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
    lines.append(
        "前向收益口径：`pair_history` 就近价格，容差默认 ±1800s（对齐 `jev_validity`）。"
        "这是信号层 **mark-to-market** 前向收益，不是第 8 节已实现 PnL。"
    )
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
            "对账提示：与当次周检 `DECISION_ENGINE_JEV_VALIDITY_FILTERED.md` 对照，"
            "不引用冻结样本。"
            f"本切片 up_1h={_pct(jev_slice_metrics.get('up_1h_rate'))}，"
            f"up_4h={_pct(jev_slice_metrics.get('up_4h_rate'))}，"
            f"n={jev_slice_metrics.get('n', 0)}，"
            f"unique_token={jev_slice_metrics.get('unique_token', 0)}。"
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

    # 7. 缺口
    lines.append("## 7. 缺口与下一步")
    lines.append("")
    n_fills = int((fill_attr or {}).get("n_fills") or 0)
    executed_n = exec_summary.get("executed", 0)
    strat_list = ", ".join(f"`{s}`" for s in strategies) or "—"
    lines.append(
        f"- **成交层：** 本期匹配 position={n_pos}，fills={n_fills}。"
        "已实现 PnL 见第 8 节；无匹配时成交字段为 None。"
    )
    lines.append(
        f"- **executed：** 本期 executed={executed_n}（分布见第 5 节）。"
    )
    lines.append(f"- **策略覆盖：** 本期见 {strat_list}。")
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
    lines.extend(_render_attribution_section(attribution, fill_attr, backtest_summary, joint=joint))

    # 9. R3 参数寻优（脚手架占位）
    lines.append("## 9. R3 参数寻优 — 待历史≥7天且周检跨门槛")
    lines.append("")
    if optimize_section:
        lines.append(optimize_section)
    else:
        lines.append(
            "占位：历史未达 ≥7d 或周检尚未跨门槛前不正式寻优；"
            "脚手架见 `review/optimizer.py`（`--optimize` 仅 dry_run / skip）。"
            "**不自动写配置**；正式改参需 R4 人工确认。"
        )
    lines.append("")

    # 10. 进化建议（只建议，不自动改参/开闸）
    lines.append("## 10. 进化建议")
    lines.append("")
    lines.append(format_evolution_section(evolution_suggestions or []))

    # 11. 因子分层（默认关；§8 归因保持原样）
    lines.extend(_render_factor_layer_section(factor_layers))

    # 12. 因子进化引用（不改写 §1–§11 正文；不自动激活）
    lines.append("## 12. 因子进化（候选，不自动激活）")
    lines.append("")
    lines.append(
        "本周候选因子、回测与门槛见 `python -m review.factor_loop` 产物："
        "`review/factor_candidates.db` 与 `exports/reports/factor_loop_*.md`。"
        "过门槛只记 status=accepted（待人工激活），不自动启用策略、不自动调参。"
    )
    lines.append("")

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
