"""Technical momentum buy factor — identical to TechMomentumStrategy.on_tick."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorBuyBase


class TechMomentumBuyFactor(FactorBuyBase):
    factor_name = "tech_momentum"

    def _init_self(self, **kwargs: Any) -> None:
        self.min_change_5m = float(kwargs.get("min_change_5m", 2.0))
        self.min_volume_strength = float(kwargs.get("min_volume_strength", 0.5))
        self.position_pct = float(kwargs.get("position_pct", 0.05))

    def fit_tick(self, row: dict[str, Any]) -> dict[str, Any] | None:
        try:
            c5 = float(row["change_5m"]) if row.get("change_5m") is not None else None
            vs = (
                float(row["volume_strength"])
                if row.get("volume_strength") is not None
                else None
            )
        except (TypeError, ValueError):
            return None
        if c5 is None or vs is None:
            return None
        # Same conjunction as TechMomentumStrategy (NaN fails `>=`).
        if not (c5 >= self.min_change_5m and vs >= self.min_volume_strength):
            return None
        return {
            "size_pct": self.position_pct,
            "reason": (
                f"change_5m={c5:.3f}>={self.min_change_5m}"
                f" volume_strength={vs:.3f}>={self.min_volume_strength}"
            ),
            "change_5m": c5,
            "volume_strength": vs,
        }
