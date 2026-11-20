"""Initial prices for the eight cross-margin strategies.

Numbers below are the t447 ruling. They are not a backtest and not a
claim about returns. A missing or illegal coordinate refuses the order.

t494 addendum (contract text only; t447 geometry is unchanged):
    The live BUY 50bps minimum entry-to-stop gap is part of this contract.
    For action=buy with a stop_price present, LIMIT uses limit_price as the
    intended entry and MARKET uses the signal price. The stop must be at
    least LONG_MIN_ENTRY_STOP_GAP (50 bps, 0.005) below that entry. A gap
    equal to 50 bps passes; a tighter gap is refused as
    stop_too_close_to_entry. This floor is not replaced by strategy
    geometry. It applies only to buys; shorts are not placed under the
    live buy guard.

    Unchanged alongside this floor: the stop-limit remains 20 bps and not
    less than 2 ticks, long below and short above, with no re-anchoring,
    and the rest of the t447 formulas, odds floors, and spans.

    This addendum does not approve a position-size figure.
"""

from __future__ import annotations

import math
from typing import Any

CONTRACT_ID = "t447"
CONTRACT_ADDENDUM_ID = "t494_long_min_stop_gap"
# Formal contract floor for live BUY stops. Must stay equal to
# decision_engine.core.stop_gap.MIN_ENTRY_STOP_GAP. Do not lower it.
# Shorts are not covered by this buy-side guard.
LONG_MIN_ENTRY_STOP_GAP = 0.005
_LIMIT_ALPHA_CAP = 0.30
_S2_ALPHA_SPLIT = 0.10
_S2_LIMIT_ALPHA_FLOOR = 0.06
_STOP_SPAN = 0.10
_DEEP_ALPHA = 0.05
_DEEP_LIMIT_SPAN = 0.05
_DEEP_STOP_SPAN = 0.20
_REGIME3_MIN_ODDS = 1.2
_CARRY_MIN_EXTENSION = 0.0035
_CARRY_MIN_ODDS = 1.5
_CARRY_STOP_W = 0.10
_SHORT_STOP_W = 0.15
_UNLOCK_ENTRY_W = 0.65

_ENTRY_ZONE_STRATEGIES = frozenset(
    {"tech_boll_fade_v1", "regime1_positive_gamma_fade"}
)
_SHORT_STRATEGIES = frozenset(
    {
        "regime4_crowded_long_carry_short",
        "regime5_crisis_contagion_short",
        "regime5_unlock_cliff_short",
    }
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _first(row: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        number = _finite(row.get(name))
        if number is not None:
            return number
    return None


def _pricing_bar_clock(row: dict[str, Any]) -> str | None:
    """900s applies whenever the caller supplied now. It does not depend on regime-v2."""
    now = _finite(row.get("_freshness_now_ms"))
    if now is None:
        return None
    close_ms = _finite(row.get("kline_close_time_ms"))
    if close_ms is None:
        close_ms = _finite(row.get("entry_kline_5m_close_ms"))
    if close_ms is None:
        return "kline_bar_missing"
    age = now - close_ms
    if age < -120_000 or age > 900_000:
        return "entry_5m_kline_stale"
    return None


def closed_bar(row: dict[str, Any]) -> tuple[dict[str, float] | None, str | None]:
    """P, L, M and, when present, H from one closed 5m bar. High is never the close."""
    price = _first(row, ("price_usd", "close", "price"))
    low = _first(row, ("kline_low", "low", "bar_low"))
    mid = _first(row, ("boll_mid",))
    if price is None or low is None or mid is None:
        return None, "closed_bar_coordinate_missing"
    span = mid - low
    if span <= 0 or not (low < price):
        return None, "closed_bar_span_invalid"
    high = _first(row, ("kline_high", "high"))
    # A column that exists but is empty stays missing. Do not copy close into it.
    if "kline_high" in row or "high" in row:
        raw_high = row.get("kline_high", row.get("high"))
        if _finite(raw_high) is None:
            high = None
    out = {"P": price, "L": low, "M": mid, "S": span}
    if high is not None:
        if high < price:
            return None, "high_below_close"
        width = high - low
        if width <= 0:
            return None, "bar_width_invalid"
        out["H"] = high
        out["W"] = width
    return out, None


def _pack(
    *,
    limit_price: float,
    take_profit: float,
    stop_price: float,
    bar: dict[str, float],
    raw_odds: float | None = None,
) -> dict[str, Any]:
    if min(limit_price, take_profit, stop_price) <= 0:
        return {}
    payload: dict[str, Any] = {
        "cross_margin_price_contract": CONTRACT_ID,
        "order_type": "LIMIT",
        "limit_price": limit_price,
        "take_profit_price": take_profit,
        "stop_price": stop_price,
        "closed_bar_close": bar["P"],
        "price_usd": bar["P"],
        "kline_low": bar["L"],
        "boll_mid": bar["M"],
    }
    if "H" in bar:
        payload["kline_high"] = bar["H"]
    if raw_odds is not None:
        payload["raw_odds"] = raw_odds
    return payload


def _entry_zone_order(row: dict[str, Any], bar: dict[str, float]) -> tuple[dict[str, Any] | None, str | None]:
    # strategies/__init__ imports the producers that call this module.
    from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone

    decision = evaluate_entry_zone(row, cross_margin_book=True)
    if not decision.emit:
        return None, decision.reject_reason or "entry_zone_rejected"
    limit = decision.limit_price
    if limit is None:
        if decision.order_type == "MARKET":
            limit = decision.price if decision.price is not None else bar["P"]
        else:
            return None, "entry_zone_limit_missing"
    stop = decision.stop_price
    take_profit = decision.take_profit_price
    if limit is None or stop is None or take_profit is None:
        return None, "entry_zone_price_missing"
    if not (stop < limit and take_profit > limit):
        return None, "long_price_geometry_invalid"
    return _pack(limit_price=limit, take_profit=take_profit, stop_price=stop, bar=bar), None


def _require_high(bar: dict[str, float]) -> str | None:
    if "H" not in bar or "W" not in bar:
        return "native_high_missing"
    return None


def order_for_strategy(
    strategy_id: str,
    row: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Structural limit, take-profit, and stop. The plan applies tp_multiplier once."""
    coord_error = row.get("entry_zone_coord_error")
    if isinstance(coord_error, str) and coord_error.strip():
        return None, coord_error.strip()
    clock_reason = _pricing_bar_clock(row)
    if clock_reason is not None:
        return None, clock_reason
    bar, reason = closed_bar(row)
    if bar is None:
        return None, reason
    assert bar is not None
    price, low, mid, span = bar["P"], bar["L"], bar["M"], bar["S"]
    alpha = (price - low) / span

    if strategy_id in _ENTRY_ZONE_STRATEGIES:
        return _entry_zone_order(row, bar)

    if strategy_id == "regime2_short_squeeze_breakout":
        limit_alpha = (
            min(alpha - 0.02, _LIMIT_ALPHA_CAP) if alpha >= _S2_ALPHA_SPLIT else _S2_LIMIT_ALPHA_FLOOR
        )
        limit = low + limit_alpha * span
        stop = low - _STOP_SPAN * span
        if not (0 < stop < limit < price):
            return None, "regime2_limit_geometry_invalid"
        return _pack(limit_price=limit, take_profit=mid, stop_price=stop, bar=bar), None

    if strategy_id == "regime3_xs_leader_pullback":
        stop = low - _STOP_SPAN * span
        denominator = mid - stop
        if not (price > mid > low and denominator > 0):
            return None, "regime3_geometry_invalid"
        odds = (price - mid) / denominator
        if odds < _REGIME3_MIN_ODDS:
            return None, "regime3_odds_below_min"
        return _pack(
            limit_price=mid, take_profit=price, stop_price=stop, bar=bar, raw_odds=odds
        ), None

    if strategy_id == "regime4_defensive_deep_fade":
        if not (alpha > _DEEP_ALPHA):
            return None, "regime4_alpha_not_deep"
        limit = low + _DEEP_LIMIT_SPAN * span
        stop = low - _DEEP_STOP_SPAN * span
        if not (0 < stop < limit < price < mid):
            return None, "regime4_deep_geometry_invalid"
        return _pack(limit_price=limit, take_profit=mid, stop_price=stop, bar=bar), None

    high_reason = _require_high(bar)
    if strategy_id in _SHORT_STRATEGIES and high_reason:
        return None, high_reason
    if strategy_id not in _SHORT_STRATEGIES:
        return None, "strategy_price_unruled"

    high, width = bar["H"], bar["W"]
    if strategy_id == "regime4_crowded_long_carry_short":
        if not (price > mid):
            return None, "carry_close_not_above_mid"
        extension = (price - mid) / price
        if extension < _CARRY_MIN_EXTENSION:
            return None, "carry_extension_below_min"
        stop = high + _CARRY_STOP_W * width
        denominator = stop - price
        if denominator <= 0:
            return None, "carry_stop_not_above_close"
        odds = (price - mid) / denominator
        if odds < _CARRY_MIN_ODDS:
            return None, "carry_odds_below_min"
        if not (mid < price < stop):
            return None, "carry_geometry_invalid"
        return _pack(
            limit_price=price, take_profit=mid, stop_price=stop, bar=bar, raw_odds=odds
        ), None

    if strategy_id == "regime5_crisis_contagion_short":
        if not (low < price < high):
            return None, "contagion_geometry_invalid"
        stop = high + _SHORT_STOP_W * width
        if not (low < price < stop):
            return None, "contagion_stop_invalid"
        return _pack(limit_price=price, take_profit=low, stop_price=stop, bar=bar), None

    entry = max(price, low + _UNLOCK_ENTRY_W * width)
    stop = high + _SHORT_STOP_W * width
    if not (low < entry < stop):
        return None, "unlock_geometry_invalid"
    return _pack(limit_price=entry, take_profit=low, stop_price=stop, bar=bar), None
