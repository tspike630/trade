"""P0 risk strip: gates, today's execution rates, failures, process heartbeats."""

from __future__ import annotations

from typing import assert_never

from nicegui import ui

from dashboard.data import DataStore, DecisionWindowStats, FailureRow, ProcessProbe, RateBlock
from dashboard.format_util import (
    Liveness,
    confirm_label,
    fmt_age,
    fmt_rate,
    fmt_ts,
    fmt_usd,
    liveness_label,
    short_addr,
)
from dashboard.panels.common import col, data_table, kpi, panel_box, show_unavailable, slow_hint
from dashboard.settings import GateStatus, read_gates


def render_risk(store: DataStore) -> None:
    gates = read_gates()
    extra = "risk-live" if gates.live_effective else ""
    with panel_box(
        "风险",
        f"闸门只读。{_live_hint(gates)} 这里没有开关。",
        extra,
        tags=gate_header_tags(gates),
    ):
        _today(store)
        _week(store)
        _processes(store)
        _failures(store)
        _backtest(store)
        _news(store)


def live_face(status: GateStatus) -> tuple[str, str]:
    """Short live-gate label and tone. Read-only."""
    return _live_face(status)


def gate_header_tags(status: GateStatus) -> tuple[tuple[str, str], ...]:
    """Three compact labels for the risk panel header, not a second status bar."""
    paper = ("模拟盘执行 开", "tone-warn") if status.paper_enabled else ("模拟盘执行 关", "")
    pm = ("仓位管理 开", "tone-warn") if status.pm_enabled else ("仓位管理 关", "")
    live_value, live_tone = _live_face(status)
    return (paper, pm, (f"实盘 {live_value}", live_tone))


def _live_face(status: GateStatus) -> tuple[str, str]:
    if status.live_effective:
        return "开启", "tone-bad"
    if status.live_env_enabled and status.kill_switch_present:
        return "阻断", "tone-warn"
    if status.live_env_enabled:
        return "未确认", "tone-warn"
    return "关闭", ""


def _live_hint(status: GateStatus) -> str:
    env = "开" if status.live_env_enabled else "关"
    switch = "存在" if status.kill_switch_present else "不存在"
    parts = [
        f"LIVE_TRADING_ENABLED {env}",
        f"杀开关 {status.kill_switch_name} {switch}",
        f"确认短语{confirm_label(status.confirm_state)}",
    ]
    if status.live_block_reason and status.live_block_reason not in parts:
        parts.append(status.live_block_reason)
    return " · ".join(parts)


def _today(store: DataStore) -> None:
    result = store.today_execution_stats()
    slow_hint(result.elapsed_ms)
    ui.label("今日 · execution_log 行（UTC 日）").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    block: RateBlock = result.data
    with ui.element("div").classes("kpi-grid"):
        kpi("今日信号数", str(block.n), "当日执行账本行数")
        kpi("校验通过率", fmt_rate(block.ok_rate, block.n_ok, block.n), "validation_ok = 1")
        kpi("已执行率", fmt_rate(block.executed_rate, block.n_executed, block.n), "executed = 1")


def _week(store: DataStore) -> None:
    result = store.week_decision_stats()
    slow_hint(result.elapsed_ms)
    ui.label("近 7 日 · 决策口径（滚动 168 小时，比率与 review 周报一致）").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    block: DecisionWindowStats = result.data
    with ui.element("div").classes("kpi-grid"):
        kpi("决策数", str(block.n_decisions), "decision_log")
        kpi("有执行", str(block.n_with_exec), "关联到 execution_log 的决策")
        kpi(
            "校验通过率",
            fmt_rate(block.ok_rate, block.n_ok, block.n_with_exec),
            "通过数 / 有执行的决策",
        )
        kpi(
            "已执行率",
            fmt_rate(block.executed_rate, block.n_executed, block.n_with_exec),
            "executed 数 / 有执行的决策",
        )


def _processes(store: DataStore) -> None:
    result = store.process_liveness()
    slow_hint(result.elapsed_ms)
    ui.label("进程心跳").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    with ui.element("div").classes("proc-row"):
        for probe in result.data:
            _process_chip(probe)


def _process_chip(probe: ProcessProbe) -> None:
    with ui.element("div").classes(f"proc {_process_class(probe.state)}"):
        ui.label(probe.name).classes("proc-name")
        ui.label(liveness_label(probe.state)).classes("proc-state")
        detail = fmt_age(probe.age_seconds)
        if probe.detail:
            detail = f"{detail} · {probe.detail}"
        ui.label(detail).classes("proc-detail")


def _process_class(state: Liveness) -> str:
    match state:
        case "alive":
            return "proc-alive"
        case "stale":
            return "proc-stale"
        case "unknown":
            return "proc-unknown"
        case _ as unreachable:
            assert_never(unreachable)


def _failures(store: DataStore) -> None:
    result = store.recent_failures(10)
    slow_hint(result.elapsed_ms)
    ui.label("最近失败").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    rows = [_failure_row(item) for item in result.data]
    if not rows:
        ui.label("暂无 validation_errors").classes("muted")
        return
    data_table(
        [
            col("ts", "时间"),
            col("decision", "决策"),
            col("strategy", "策略"),
            col("action", "动作"),
            col("token", "代币"),
            col("errors", "错误"),
        ],
        rows,
    )


def _failure_row(item: FailureRow) -> dict[str, object]:
    return {
        "id": item.id,
        "ts": fmt_ts(item.ts),
        "decision": item.decision_log_id,
        "strategy": item.strategy_id or "—",
        "action": item.action or "—",
        "token": short_addr(item.token_address),
        "errors": item.validation_errors or "—",
    }


def _backtest(store: DataStore) -> None:
    result = store.latest_backtest()
    slow_hint(result.elapsed_ms)
    ui.label("最近回测").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    run = result.data
    if run is None:
        ui.label("暂无回测记录").classes("muted")
        return
    bits = [
        run.run_id or "—",
        run.strategy_id or "—",
        run.status or "—",
        f"起始 {fmt_usd(run.start_cash)}",
        f"结束权益 {fmt_usd(run.end_equity)}",
        fmt_ts(run.started_at),
    ]
    if run.finished_at:
        bits.append(f"结束 {fmt_ts(run.finished_at)}")
    ui.label(" · ".join(bits)).classes("backtest-line")
    if run.notes:
        ui.label(run.notes).classes("muted")


def _news(store: DataStore) -> None:
    result = store.latest_news(5)
    with ui.expansion("新闻旁路", icon="newspaper").classes("news-box"):
        slow_hint(result.elapsed_ms)
        if not result.available:
            show_unavailable(result.reason)
            return
        if not result.data:
            ui.label("暂无新闻").classes("muted")
            return
        for item in result.data:
            title = item.title or "—"
            meta = " · ".join(
                part
                for part in (item.source, item.source_tier, item.onchain_status, fmt_ts(item.published_at))
                if part
            )
            with ui.element("div").classes("news-item"):
                _news_title(title, item.url)
                if meta:
                    ui.label(meta).classes("muted")


def _news_title(title: str, url: str) -> None:
    href = (url or "").strip()
    if href.startswith("https://") or href.startswith("http://"):
        ui.link(title, href, new_tab=True).classes("news-link")
        return
    ui.label(title)
