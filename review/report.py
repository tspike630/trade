"""Markdown weekly report renderer for R1 review layer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from review.data import SignalChainRow, load_ccxt_market_baseline
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

    # 先验标签自动分桶（get_prior_tag_buckets / prior_tagger 产物）
    ptb = joint.get("prior_tag_buckets") or []
    lines.append("### 先验标签自动分桶（prior_tagger 产物）")
    lines.append("")
    lines.append(
        "> 数据：`decision_prior_tags`（prior.db，只读）；"
        "`peak_window`=信号落在创建后 3 分钟内；"
        "分桶结果与上一小节全样本联合表对账。"
    )
    lines.append("")
    if not ptb:
        lines.append("_prior.db.decision_prior_tags 不可用/空；未生成自动分桶。_")
        lines.append("")
    else:
        lines.append(
            "| age_bucket | n | unique_token | 在见顶窗口内 | 有 1h 参考 | 有 4h 参考 |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|")
        order = ["<1h", "1-24h", "1-7d", ">7d", "unknown"]
        by = {str(r.get("age_bucket")): r for r in ptb}
        for lab in order:
            r = by.get(lab)
            if not r:
                continue
            lines.append(
                f"| {lab} | {r.get('n', 0)} | {r.get('unique_token', 0)} | "
                f"{r.get('in_peak', 0)} | {r.get('has_ref_1h', 0)} | "
                f"{r.get('has_ref_4h', 0)} |"
            )
        # any unexpected buckets
        for lab, r in by.items():
            if lab not in order:
                lines.append(
                    f"| {lab} | {r.get('n', 0)} | {r.get('unique_token', 0)} | "
                    f"{r.get('in_peak', 0)} | {r.get('has_ref_1h', 0)} | "
                    f"{r.get('has_ref_4h', 0)} |"
                )
        lines.append("")
        rec = joint.get("reconcile_notes") or []
        if rec:
            lines.append(f"对账不一致：{len(rec)} 项（联合表全样本 n vs prior_tag_buckets）")
            for m in rec[:8]:
                lines.append(f"- {m}")
            if len(rec) > 8:
                lines.append(f"- …共 {len(rec)} 条")
        else:
            lines.append("对账：与全样本联合表分桶 n **一致**。")
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


def _fmt_quote_vol(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v >= 1e9:
        return f"{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:.0f}"


def _render_ccxt_baseline_section(db_root: Optional[str | Path]) -> list[str]:
    """§6b: read-only CCXT sidechannel second market benchmark."""
    lines: list[str] = []
    lines.append("### 6b. CEX 市场基准第二参照（ccxt.db，只观察）")
    lines.append("")
    if db_root is None:
        lines.append("_未提供 db_root；跳过 ccxt.db 第二参照。_")
        lines.append("")
        return lines

    info = load_ccxt_market_baseline(db_root)
    if not info.get("ok"):
        lines.append(f"_{info.get('note') or 'ccxt.db 不可用；已跳过'}_")
        lines.append("")
        lines.append(
            "> 纪律：只观察不调权、不进决策/告警；缺库优雅跳过。"
        )
        lines.append("")
        return lines

    ex_list = info.get("exchanges") or []
    ex_s = ", ".join(f"`{e}`" for e in ex_list) or "—"
    tag = info.get("status_tag") or "SHORT"
    winsor = info.get("winsor_abs", 50)
    lines.append(
        f"**状态:** `{tag}` / 旁路 · **交易所:** {ex_s} · "
        f"**样本量:** 最近一轮/所 n={info.get('latest_round_n', 0)} "
        f"（change_24h n={info.get('n_change_24h', 0)}）· "
        f"全库 ticks={info.get('ticks_total', 0)} / "
        f"listings={info.get('listings_total', 0)} / "
        f"ohlcv={info.get('ohlcv_total', 0)}"
    )
    lines.append("")
    lines.append(
        f"`ccxt_ticks.change_24h_pct`（最近一轮/所）与 §6 hot 并列对照；"
        f"winsor(|x|≤{winsor:g})。**不**调权、不进决策/告警。"
    )
    lines.append("")

    # per-exchange counts
    by_ex = info.get("by_exchange") or {}
    if by_ex:
        lines.append("| exchange | ticks | listings | ohlcv |")
        lines.append("|---|---:|---:|---:|")
        for ex in sorted(by_ex.keys()):
            d = by_ex[ex]
            lines.append(
                f"| `{ex}` | {d.get('ticks', 0)} | "
                f"{d.get('listings', 0)} | {d.get('ohlcv', 0)} |"
            )
        lines.append("")

    lines.append("| 指标 | 原始 | winsor |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| n (change_24h) | {info.get('n_change_24h', 0)} | "
        f"{info.get('n_change_24h_winsor', 0)} |"
    )
    lines.append(
        f"| median change_24h | {_chg_pct(info.get('median_change_24h'))} | "
        f"{_chg_pct(info.get('median_change_24h_winsor'))} |"
    )
    lines.append(
        f"| avg change_24h | {_chg_pct(info.get('avg_change_24h'))} | "
        f"{_chg_pct(info.get('avg_change_24h_winsor'))} |"
    )
    lines.append("")

    top = info.get("top_by_quote") or []
    if top:
        bits = []
        for t in top:
            bits.append(
                f"`{t.get('exchange')}:{t.get('symbol')}` "
                f"qv={_fmt_quote_vol(t.get('quote_volume_24h'))} "
                f"Δ24h={_chg_pct(t.get('change_24h_pct'))}"
            )
        lines.append("**Top quote_volume（最近一轮）:** " + " · ".join(bits))
        lines.append("")

    lines.append(
        "> 纪律：SHORT/旁路第二参照；只观察不调权、不进决策/告警。"
    )
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
    db_root: Optional[str | Path] = None,
) -> str:
    """返回 markdown 字符串（§1–8 + 可选§9 R3 + §10 进化建议 + baseline_history）。"""
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

    # 6b. CEX 市场基准第二参照（ccxt.db，只观察）
    lines.extend(_render_ccxt_baseline_section(db_root))

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
    lines.extend(_render_attribution_section(attribution, fill_attr, backtest_summary, joint=joint))

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
    lines.append(
        format_evolution_section(
            evolution_suggestions or [],
            db_root=db_root,
        )
    )

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
