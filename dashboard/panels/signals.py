"""P2 signal stream: latest decision_log rows joined to the newest execution."""

from __future__ import annotations

from dashboard.data import DataStore, SignalRow
from dashboard.format_util import (
    flag_text,
    fmt_coverage,
    fmt_liq,
    fmt_strength,
    fmt_ts,
    short_addr,
)
from dashboard.panels.common import col, data_table, panel_box, show_unavailable, slow_hint
from dashboard.chat.page import set_chat_context
from nicegui import ui
from dashboard.settings import refresh_seconds


def render_signals(store: DataStore) -> None:
    interval = int(refresh_seconds())
    with panel_box("信号流", f"decision_log 最近 50 条，关联最新一条 execution。每 {interval} 秒刷新。"):
        result = store.recent_signals(50)
        slow_hint(result.elapsed_ms)
        if not result.available:
            show_unavailable(result.reason)
            return
        if result.data.exec_note:
            show_unavailable(f"执行结果无法关联：{result.data.exec_note}")
        rows = [_row(item) for item in result.data.rows]
        def _click(row: dict) -> None:
            token = str(row.get("token_raw") or row.get("token") or "").strip()
            if token and token != "—":
                set_chat_context(token)
                ui.notify(f"已带入 AI 上下文 [Context: {token}]")

        data_table(
            [
                col("ts", "时间"),
                col("strategy", "策略"),
                col("action", "动作"),
                col("chain", "链"),
                col("token", "代币"),
                col("strength", "JEV", "right"),
                col("coverage", "覆盖", "right"),
                col("liq", "流动性", "right"),
                col("reason", "原因"),
                col("check", "校验"),
                col("done", "已执行"),
                col("executor", "执行器"),
                col("link", "关联"),
                col("errors", "错误"),
            ],
            rows,
            row_key="decision_id",
            color_fields=("check", "done"),
            on_row_click=_click,
        )


def _row(item: SignalRow) -> dict[str, object]:
    check, check_class = flag_text(item.validation_ok, yes="通过", no="拒绝")
    done, done_class = flag_text(item.executed, yes="是", no="否")
    if item.validation_ok is None and item.executed is None:
        link = "未关联"
    elif item.exec_dup:
        link = "多条取最新"
    else:
        link = "单条"
    return {
        "decision_id": item.decision_id,
        "ts": fmt_ts(item.ts),
        "strategy": item.strategy_id or "—",
        "action": item.action or "—",
        "chain": item.chain_id or "—",
        "token": short_addr(item.token_address),
        "token_raw": item.token_address or "",
        "strength": fmt_strength(item.jev_strength),
        "coverage": fmt_coverage(item.coverage),
        "liq": fmt_liq(item.liquidity_usd),
        "reason": item.reason or "—",
        "check": check,
        "check_class": check_class,
        "done": done,
        "done_class": done_class,
        "executor": item.executor or "—",
        "link": link,
        "errors": item.validation_errors or "—",
    }
