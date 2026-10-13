"""RSI oversold-bounce buy factor (uses token_indicators.rsi_14)."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorBuyBase


class RsiBounceBuyFactor(FactorBuyBase):
    factor_name = "rsi_bounce"

    def _init_self(self, **kwargs: Any) -> None:
        self.oversold = float(kwargs.get("oversold", 30.0))
        self.min_change_5m = float(kwargs.get("min_change_5m", 0.0))  # 反弹确认：5m 转正
        self.position_pct = float(kwargs.get("position_pct", 0.05))

    def fit_tick(self, row: dict[str, Any]) -> dict[str, Any] | None:
        rsi = row.get("rsi_14")
        if rsi is None:
            return None
        try:
            r = float(rsi)
        except (TypeError, ValueError):
            return None
        if r > self.oversold:
            return None
        try:
            c5 = float(row["change_5m"]) if row.get("change_5m") is not None else None
        except (TypeError, ValueError):
            c5 = None
        if c5 is None or c5 < self.min_change_5m:
            return None
        return {
            "size_pct": self.position_pct,
            "reason": f"rsi_bounce rsi={r:.1f}<={self.oversold:.0f} c5={c5:+.3f}",
            "rsi_14": r,
        }
