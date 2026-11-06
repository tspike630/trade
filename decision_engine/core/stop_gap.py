"""Absolute stop distance for a live BUY.

Entry-zone odds are a ratio, ``(mid - entry) / (entry - stop)``. A tight
band can clear 1.5 or 2.5 while the stop sits only a few basis points under
the signal. Decision 148592 (``live_bnb_boll_fade`` BUY DASHUSDT) did that:
signal 48.08, planned stop 48.02605 (11.2 bps under the signal). The market
fill averaged about 48.02, already through the stop, and the exchange
rejected the OCO. The ledger still stored the signal price as the entry.

This guard refuses a BUY unless the stop is at least
``MIN_ENTRY_STOP_GAP`` (50 bps) under the intended entry. MARKET uses the
signal price. LIMIT uses the limit. 50 bps is four times that DASH slip and
stays under the classic 5% shell and the entry-zone fixtures (about 1.8% at
alpha 0.08 with a span of 10). It does not change TP/SL percents, the
span fraction, or the odds floors.

Tick rounding and a second check against the fill price stay on execution.
"""

from __future__ import annotations

import math
from typing import Any, Literal

# 50 bps. Pinned by decision_engine.tests.test_live_bnb_stop_gap_halt.
MIN_ENTRY_STOP_GAP = 0.005
_GAP_EPS = 1e-9

OrderKind = Literal["MARKET", "LIMIT"]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def stop_distance_reason(
    entry: float,
    stop: float,
    *,
    min_gap: float = MIN_ENTRY_STOP_GAP,
) -> str | None:
    """``stop_too_close_to_entry`` when ``stop`` is not ``min_gap`` under ``entry``.

    A gap equal to ``min_gap`` is accepted. ``stop_gap_invalid`` is an
    unusable entry or stop.
    """
    if not math.isfinite(entry) or not math.isfinite(stop) or entry <= 0 or min_gap < 0:
        return "stop_gap_invalid"
    gap = (entry - stop) / entry
    if not math.isfinite(gap) or gap + _GAP_EPS < min_gap:
        return "stop_too_close_to_entry"
    return None


def _order_kind(raw: Any) -> OrderKind | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "MARKET"
    text = str(raw).strip().upper()
    if text == "MARKET":
        return "MARKET"
    if text == "LIMIT":
        return "LIMIT"
    return None


def intended_entry(signal: dict[str, Any]) -> tuple[float | None, str | None]:
    """``(price, error)``. LIMIT uses ``limit_price``. MARKET uses the signal."""
    kind = _order_kind(signal.get("order_type"))
    if kind is None:
        return None, "stop_gap_invalid"
    if kind == "LIMIT":
        limit = _finite(signal.get("limit_price"))
        if limit is None or limit <= 0:
            return None, "stop_gap_invalid"
        return limit, None
    if kind == "MARKET":
        price = _finite(signal.get("price_usd"))
        if price is None:
            price = _finite(signal.get("price"))
        if price is None or price <= 0:
            return None, "stop_gap_invalid"
        return price, None
    unexpected: never = kind
    return None, f"stop_gap_invalid:{unexpected}"


def buy_stop_gap_reason(signal: dict[str, Any]) -> str | None:
    """Why this BUY must not be emitted. No stop means the check does not apply.

    Paper emits omit ``stop_price``. A present stop that cannot be compared
    fails closed.
    """
    if str(signal.get("action") or "").lower() != "buy":
        return None
    if "stop_price" not in signal or signal.get("stop_price") is None:
        return None
    stop = _finite(signal.get("stop_price"))
    if stop is None:
        return "stop_gap_invalid"
    entry, error = intended_entry(signal)
    if error is not None or entry is None:
        return error or "stop_gap_invalid"
    return stop_distance_reason(entry, stop)
