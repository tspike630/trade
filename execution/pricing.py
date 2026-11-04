"""Shared paper price helpers: liquidity slippage and fill prices.

TP/SL triggers compare the mark (mid) to the entry fill. Entry fill is
mid × (1 + slip). Signal sells and tick closes both fill at mid × (1 − slip).
Equity marks use mid, not the slipped fill.

Take-profit tick closes additionally clamp the fill to the TP limit price
(see ``limit_exit_fill_price``): a limit sell cannot fill above its trigger.
"""

from __future__ import annotations

import math
import os
from typing import Any

PAPER_TP_CLAMP_ENV = "EXECUTION_PAPER_TP_CLAMP"
PAPER_CLOSE_MIN_LIQ_ENV = "EXECUTION_PAPER_CLOSE_MIN_LIQ_USD"
DEFAULT_CLOSE_MIN_LIQ_USD = 1000.0


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


def paper_tp_clamp_enabled() -> bool:
    """EXECUTION_PAPER_TP_CLAMP: default on. 0/false/no/off disables."""
    raw = os.environ.get(PAPER_TP_CLAMP_ENV)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def paper_close_min_liq_usd() -> float:
    """Min liquidity_usd required for a non-flat paper close. Default 1000.

    Env ``EXECUTION_PAPER_CLOSE_MIN_LIQ_USD``. Unset/blank/invalid → 1000.
    Values ≤ 0 disable the dead-pool flatten (threshold never binds on
    non-negative liquidity).
    """
    raw = os.environ.get(PAPER_CLOSE_MIN_LIQ_ENV)
    if raw is None or not str(raw).strip():
        return float(DEFAULT_CLOSE_MIN_LIQ_USD)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(DEFAULT_CLOSE_MIN_LIQ_USD)
    if not math.isfinite(value):
        return float(DEFAULT_CLOSE_MIN_LIQ_USD)
    return float(value)


def limit_exit_fill_price(
    *,
    entry_price: float,
    tp_pct: float | None,
    mid: float,
    slip: float,
) -> tuple[float, bool, float | None]:
    """TP limit-sell fill: never better than the trigger limit.

    Semantics: take_profit is a limit sell at ``entry * (1 + tp_pct/100)``.
    Fill is ``min(mid * (1 - slip), limit)``. When the slipped mid is already
    at/below the limit, behavior matches ``sell_fill_price`` unchanged.

    Returns ``(fill_price, clamped, limit_price)``. ``limit_price`` is None
    when ``tp_pct`` is missing/unusable (no clamp applied).
    """
    raw = sell_fill_price(mid, slip)
    if tp_pct is None:
        return raw, False, None
    try:
        tp = float(tp_pct)
        entry = float(entry_price)
    except (TypeError, ValueError):
        return raw, False, None
    if not math.isfinite(tp) or not math.isfinite(entry) or entry <= 0:
        return raw, False, None
    limit = entry * (1.0 + tp / 100.0)
    if not math.isfinite(limit) or limit <= 0:
        return raw, False, None
    if raw > limit + 1e-15:
        return float(limit), True, float(limit)
    return float(raw), False, float(limit)


def is_dead_pool_close(
    *,
    naive_fill: float,
    entry_price: float,
    liquidity_usd: float | None,
    min_liq_usd: float | None = None,
) -> bool:
    """Whether a tick close should flatten at entry for a dead pool.

    Rules:
    - ``liquidity_usd is None`` → never (unknown is not dead).
    - ``liquidity_usd < min_liq`` (default env/1000) → candidate.
    - Only when ``naive_fill > entry_price`` (unrealized *profit* is not
      tradable). Loss-side marks keep the worse fill — do not beautify.
    """
    if liquidity_usd is None:
        return False
    min_liq = (
        float(DEFAULT_CLOSE_MIN_LIQ_USD)
        if min_liq_usd is None
        else float(min_liq_usd)
    )
    if min_liq <= 0:
        return False
    try:
        liq = float(liquidity_usd)
        fill = float(naive_fill)
        entry = float(entry_price)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) for x in (liq, fill, entry)):
        return False
    if entry <= 0 or liq >= min_liq:
        return False
    # Profit-direction gate only.
    return fill > entry

