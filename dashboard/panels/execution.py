"""P3 execution log and 7-day rejection buckets."""

from __future__ import annotations

from nicegui import ui

from dashboard.data import DataStore, ExecutionRow, RejectionReport
from dashboard.format_util import flag_text, fmt_ts, short_addr
from dashboard.panels.common import col, data_table, panel_box, show_unavailable, slow_hint


def render_execution(store: DataStore) -> None:
    with panel_box(
        "执行记录",
        "execution_log 最近 50 条。拒绝分布含校验失败，以及 status / 回执中的 PM、风控、paper 拒绝。",
    ):
        _buckets(store)
        _rows(store)


def _buckets(store: DataStore) -> None:
    result = store.rejection_buckets(7)
    slow_hint(result.elapsed_ms)
    ui.label("近 7 日拒绝分布").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    report: RejectionReport = result.data
    if report.total == 0:
        ui.label("近 7 日无拒绝").classes("muted")
        return
    ui.label(f"共 {report.total} 条").classes("muted")
    shown = report.buckets[:8]
    rest = sum(item.count for item in report.buckets[8:])
    peak = max(item.count for item in shown)
    for item in shown:
        with ui.element("div").classes("bucket"):
            with ui.element("div").classes("bucket-meta"):
                ui.label(item.key).classes("bucket-key")
                ui.label(str(item.count)).classes("bucket-count")
            ui.linear_progress(value=item.count / peak, show_value=False).classes("bucket-bar")
    if rest:
        ui.label(f"其他 {rest} 条").classes("muted")


def _rows(store: DataStore) -> None:
    result = store.recent_executions(50)
    slow_hint(result.elapsed_ms)
    ui.label("最近执行").classes("section-label")
    if not result.available:
        show_unavailable(result.reason)
        return
    data_table(
        [
            col("ts", "时间"),
            col("decision", "决策"),
            col("strategy", "策略"),
            col("action", "动作"),
            col("token", "代币"),
            col("done", "已执行"),
            col("executor", "执行器"),
            col("receipt", "回执摘要"),
        ],
        [_row(item) for item in result.data],
        color_fields=("done",),
    )


def _row(item: ExecutionRow) -> dict[str, object]:
    done, done_class = flag_text(item.executed, yes="是", no="否")
    return {
        "id": item.id,
        "ts": fmt_ts(item.ts),
        "decision": item.decision_log_id,
        "strategy": item.strategy_id or "—",
        "action": item.action or "—",
        "token": short_addr(item.token_address),
        "done": done,
        "done_class": done_class,
        "executor": item.executor or "—",
        "receipt": item.receipt_summary,
    }
