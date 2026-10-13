"""Bollinger upper-band breakout buy factor (needs price; backtest-only in practice)."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorBuyBase


class BollBreakoutBuyFactor(FactorBuyBase):
    factor_name = "boll_breakout"

    def _init_self(self, **kwargs: Any) -> None:
        self.min_vol_strength = float(kwargs.get("min_vol_strength", 0.0))
        self.position_pct = float(kwargs.get("position_pct", 0.05))

    def fit_tick(self, row: dict[str, Any]) -> dict[str, Any] | None:
        up = row.get("boll_upper")
        if up is None:
            return None
        px = row.get("price_usd")
        if px is None:
            px = row.get("price")  # 回测/注入行可能用 price
        if px is None:
            return None  # 现网 hot 行无价 → 降级 None（诚实标注）
        try:
            u = float(up)
            p = float(px)
        except (TypeError, ValueError):
            return None
        if p <= u:
            return None
        vs = row.get("volume_strength")
        if vs is not None:
            try:
                if float(vs) < self.min_vol_strength:
                    return None
            except (TypeError, ValueError):
                pass
        return {
            "size_pct": self.position_pct,
            "reason": f"boll_breakout px={p:.8g}>{u:.8g}",
            "boll_upper": u,
        }
