"""Trailing stop sell factor (skeleton; current price via data['prices'])."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorSellBase


class TrailStopSellFactor(FactorSellBase):
    factor_name = "trail_stop"

    def _init_self(self, **kwargs: Any) -> None:
        self.trail_pct = float(kwargs.get("trail_pct", 0.20))  # 20% drawdown trigger

    def fit_position(
        self, position: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        peak = position.get("peak_price")
        token = position.get("token_address")
        prices = data.get("prices") or {}
        cur = prices.get(token) if isinstance(prices, dict) else None
        if peak is None or cur is None:
            return None
        try:
            drawdown = (float(peak) - float(cur)) / float(peak)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        if drawdown < self.trail_pct:
            return None
        return {
            "size_pct": position.get("size_pct", 1.0),
            "reason": f"trail_stop dd={drawdown:.2%}>={self.trail_pct:.0%}",
        }
