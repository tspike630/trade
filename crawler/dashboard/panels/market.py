"""M5 on-chain market table (read-only)."""

from __future__ import annotations

from nicegui import app, ui

from dashboard.data import DataStore, MarketRow
from dashboard.format_util import short_addr
from dashboard.panels.common import col, data_table, panel_box, show_unavailable, slow_hint
from dashboard.chat.page import set_chat_context


def render_market(store: DataStore) -> None:
    with panel_box("链上行情", "token_indicators + pair_history / jev_signals，Python 侧合并，只读。"):
        with ui.row().classes("w-full items-center gap-2"):
            for label, val in (("全部", None), ("solana", "solana"), ("ethereum", "ethereum"), ("bsc", "bsc"), ("base", "base")):
                ui.button(label, on_click=lambda _e=None, c=val: _reload(store, c)).props("flat dense")
        _body(store, None)


def _reload(store: DataStore, chain: str | None) -> None:
    # Simple full refresh via navigate; filter applied on next timer tick by storing preference
    ui.notify(f"链过滤：{chain or chr(20840)+chr(37096)}" if False else f"链过滤：{chain or '全部'}")
    # Re-render into a dedicated container is complex without refreshable; use query param style via storage
    try:
        app.storage.user["m5_chain"] = chain
    except Exception:
        pass
    ui.navigate.to("/")


def _body(store: DataStore, chain: str | None) -> None:
    try:
        stored = app.storage.user.get("m5_chain", chain)
        if stored is not None or "m5_chain" in app.storage.user:
            chain = stored
    except Exception:
        pass
    result = store.market_rows(chain=chain, limit=80)
    slow_hint(result.elapsed_ms)
    if not result.available:
        show_unavailable(result.reason)
        return
    rows_data: list[MarketRow] = result.data
    if not rows_data:
        ui.label("暂无行情行").classes("muted")
        return
    rows = []
    for r in rows_data:
        rows.append(
            {
                "id": f"{r.chain_id}:{r.token_address}",
                "chain": r.chain_id,
                "token": short_addr(r.token_address),
                "token_raw": r.token_address or "",
                "price": _num(r.price_usd),
                "liq": _num(r.liquidity_usd, money=True),
                "c5m": _pct(r.change_5m),
                "c1h": _pct(r.change_1h),
                "c4h": _pct(r.change_4h),
                "jev": _num(r.jev_strength),
                "cov": _num(r.coverage),
                "tier": r.tier or "—",
                "c5m_class": _tone(r.change_5m),
                "c1h_class": _tone(r.change_1h),
                "c4h_class": _tone(r.change_4h),
            }
        )
    columns = [
        col("chain", "链"),
        col("token", "token"),
        col("price", "价格", "right"),
        col("liq", "流动性", "right"),
        col("c5m", "5m%", "right"),
        col("c1h", "1h%", "right"),
        col("c4h", "4h%", "right"),
        col("jev", "JEV", "right"),
        col("cov", "coverage", "right"),
        col("tier", "tier"),
    ]
    def _click(row: dict) -> None:
        token = str(row.get("token_raw") or row.get("token") or "").strip()
        if token and token != "—":
            set_chat_context(token)
            ui.notify(f"已带入 AI 上下文 [Context: {token}]")

    data_table(
        columns,
        rows,
        row_key="id",
        color_fields=("c5m", "c1h", "c4h"),
        on_row_click=_click,
    )


def _num(v: float | None, money: bool = False) -> str:
    if v is None:
        return "—"
    if money:
        if abs(v) >= 1000:
            return f"${v:,.0f}"
        return f"${v:,.2f}"
    if abs(v) >= 100:
        return f"{v:.1f}"
    return f"{v:.4g}"


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _tone(v: float | None) -> str:
    if v is None or v == 0:
        return "pnl-flat"
    return "pnl-pos" if v > 0 else "pnl-neg"
