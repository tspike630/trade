"""MACD bullish cross buy factor (single-bar approximation: macd>signal & macd>0)."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorBuyBase


class MacdCrossBuyFactor(FactorBuyBase):
    factor_name = "macd_cross"

    def _init_self(self, **kwargs: Any) -> None:
        self.min_macd = float(kwargs.get("min_macd", 0.0))
        self.position_pct = float(kwargs.get("position_pct", 0.05))

    def fit_tick(self, row: dict[str, Any]) -> dict[str, Any] | None:
        macd = row.get("macd")
        signal = row.get("macd_signal")
        if macd is None or signal is None:
            return None
        try:
            m = float(macd)
            s = float(signal)
        except (TypeError, ValueError):
            return None
        # 单 bar 近似金叉：macd 在零轴上方且高于 signal（缺前值历史，诚实标注）
        if m < self.min_macd or m <= s:
            return None
        return {
            "size_pct": self.position_pct,
            "reason": f"macd_cross macd={m:.6g}>sig={s:.6g}",
            "macd": m,
            "macd_signal": s,
        }
