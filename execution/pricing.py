"""Shared paper price helpers: liquidity slippage and fill prices.

TP/SL triggers compare the mark (mid) to the entry fill. Entry fill is
mid × (1 + slip). Signal sells and tick closes both fill at mid × (1 − slip).
Equity marks use mid, not the slipped fill.
"""

from __future__ import annotations

from typing import Any


def slippage_rate(liquidity_usd: float | None) -> float:
    """Liquidity tiers → slip fraction (not percent points).

    <50k → 2%, 50k~500k → 1%, ≥500k → 0.3%, unknown → 2%.
    """
    if liquidity_usd is None:
        return 0.02
    try:
        liq = float(liquidity_usd)
    except (TypeError, ValueError):
        return 0.02
    if liq < 50_000:
        return 0.02
    if liq < 500_000:
        return 0.01
    return 0.003


def liquidity_usd_from_raw(raw: dict[str, Any] | None) -> float | None:
    if not raw or not isinstance(raw, dict):
        return None
    li = raw.get("liquidity")
    if isinstance(li, dict):
        val = li.get("usd")
    elif isinstance(li, (int, float)):
        val = li
    else:
        val = None
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def buy_fill_price(mid: float, slip: float) -> float:
    return float(mid) * (1.0 + float(slip))


def sell_fill_price(mid: float, slip: float) -> float:
    return float(mid) * (1.0 - float(slip))
