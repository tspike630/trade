"""Read-only market table. Columns are change_* / liquidity / JEV / coverage / tier."""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

from dashboard.data import DataStore, MarketRow
from dashboard.format_util import fmt_coverage, fmt_liq, fmt_pct, fmt_strength, short_addr
from dashboard.panels.common import (
    col,
    data_table,
    empty_state,
    panel_box,
    retry_button,
    show_unavailable,
    slow_hint,
)

_CHAINS = ("all", "solana", "ethereum", "bsc", "base")
_CHANGE_FIELDS = (
    ("change_5m", "5m"),
    ("change_15m", "15m"),
    ("change_1h", "1h"),
    ("change_4h", "4h"),
    ("change_24h", "24h"),
)


def render_markets(
    store: DataStore,
    *,
    chain: str = "all",
    on_chain: Callable[[object], None] | None = None,
    on_retry: Callable[[], None] | None = None,
) -> None:
    selected = chain if chain in _CHAINS else "all"
    with panel_box("链上行情", "token_indicators 只读。change_* 已是百分数，不再乘 100。"):
        if on_chain is not None:
            ui.select(
                list(_CHAINS),
                value=selected,
                label="链",
                on_change=on_chain,
            ).props("dense outlined options-dense").classes("ft-chain")
        try:
            result = store.market_rows(chain=selected)
        except Exception:
            show_unavailable("链上行情读取失败")
            retry_button(on_retry)
            return
        slow_hint(result.elapsed_ms)
        if not result.available:
            show_unavailable(result.reason)
            retry_button(on_retry)
            return
        rows = tuple(result.data or ())
        if not rows:
            empty_state("暂无行情", "token_indicators 没有可显示的行。")
            return
        table_rows = [_row(item) for item in rows]
        data_table(
            [
                col("chain", "链"),
                col("token", "代币"),
                *[col(name, label, "right") for name, label in _CHANGE_FIELDS],
                col("liquidity", "流动性", "right"),
                col("jev", "JEV", "right"),
                col("coverage", "覆盖", "right"),
                col("tier", "档"),
            ],
            table_rows,
            row_key="token",
            color_fields=tuple(name for name, _label in _CHANGE_FIELDS),
        )


def _row(item: MarketRow) -> dict[str, object]:
    payload: dict[str, object] = {
        "chain": item.chain_id or "—",
        "token": short_addr(item.token_address, symbol=item.symbol or None),
        "liquidity": fmt_liq(item.liquidity_usd),
        "jev": fmt_strength(item.jev_strength),
        "coverage": fmt_coverage(item.coverage),
        "tier": item.tier or "—",
    }
    changes = {
        "change_5m": item.change_5m,
        "change_15m": item.change_15m,
        "change_1h": item.change_1h,
        "change_4h": item.change_4h,
        "change_24h": item.change_24h,
    }
    for name, value in changes.items():
        text, klass = _change_cell(value)
        payload[name] = text
        payload[f"{name}_class"] = klass
    return payload


def _change_cell(value: float | None) -> tuple[str, str]:
    if value is None:
        return "—", "pnl-flat"
    if value > 0:
        return fmt_pct(value), "pnl-pos"
    if value < 0:
        return fmt_pct(value), "pnl-neg"
    return fmt_pct(value), "pnl-flat"
