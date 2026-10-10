"""P1 paper-trading positions. Mark price is the latest pair_history.price_usd."""

from __future__ import annotations

from nicegui import ui

from dashboard.data import DataStore, PositionRow, PositionsPayload
from dashboard.format_util import fmt_hold, fmt_pct_raw, fmt_pnl, fmt_price, fmt_ratio, fmt_usd, short_addr
from dashboard.panels.common import col, data_table, empty_state, kpi, panel_box, show_unavailable, slow_hint


def render_positions(store: DataStore) -> None:
    with panel_box("持仓总览", "模拟盘当前持仓。现价取 pair_history 最新一条；无价为「—」。"):
        result = store.open_positions()
        slow_hint(result.elapsed_ms)
        if not result.available:
            show_unavailable(result.reason)
            return
        payload: PositionsPayload = result.data
        _summary(payload)
        if payload.price_note:
            show_unavailable(payload.price_note)
        if payload.cash_note and payload.cash_usd is None:
            ui.label(payload.cash_note).classes("muted")
        if not payload.rows:
            empty_state("当前无持仓", "模拟盘没有未平仓记录。上方权益仍按现金计算。")
            return
        if payload.missing_price:
            ui.label(f"{payload.missing_price} 笔没有现价，盯市回退到成本，浮盈显示为「—」。").classes("muted")
        data_table(
            [
                col("token", "代币"),
                col("pair", "交易对"),
                col("chain", "链"),
                col("entry", "成本价", "right"),
                col("mark", "现价", "right"),
                col("pnl", "浮盈亏", "right"),
                col("tp", "TP", "right"),
                col("sl", "SL", "right"),
                col("hold", "持有时长"),
                col("status", "状态"),
            ],
            [_row(item) for item in payload.rows],
            color_fields=("pnl",),
        )


def _summary(payload: PositionsPayload) -> None:
    cash_hint = "account.cash_usd" if not payload.cash_note else payload.cash_note
    with ui.element("div").classes("kpi-grid"):
        kpi("总权益", fmt_usd(payload.equity_usd), "现金 + 持仓盯市")
        kpi("现金", fmt_usd(payload.cash_usd), cash_hint)
        kpi("持仓盯市", fmt_usd(payload.invested_usd), "无现价时用成本")
        kpi("总仓位占比", fmt_ratio(payload.position_ratio), "盯市 / 总权益")
        kpi("持仓数", str(payload.open_count), "未平仓")


def _row(item: PositionRow) -> dict[str, object]:
    pnl, pnl_class = fmt_pnl(item.pnl_pct, item.pnl_usd)
    return {
        "id": item.id,
        "token": short_addr(item.token_address, symbol=item.symbol or None),
        "pair": short_addr(item.pair_address),
        "chain": item.chain_id or "—",
        "entry": fmt_price(item.entry_price),
        "mark": fmt_price(item.mark_price),
        "pnl": pnl,
        "pnl_class": pnl_class,
        "tp": fmt_pct_raw(item.tp_pct),
        "sl": fmt_pct_raw(item.sl_pct),
        "hold": fmt_hold(item.hold_seconds, item.max_hold_min),
        "status": item.status or "—",
    }
