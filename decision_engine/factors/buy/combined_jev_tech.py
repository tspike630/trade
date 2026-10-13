"""Combined JEV + tech AND buy factor — identical to CombinedJevTechStrategy.on_tick."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorBuyBase


class CombinedJevTechBuyFactor(FactorBuyBase):
    factor_name = "combined_jev_tech"

    def _init_self(self, **kwargs: Any) -> None:
        self.min_jev_strength = float(kwargs.get("min_jev_strength", 0.15))
        self.min_change_5m = float(kwargs.get("min_change_5m", 1.0))
        self.min_volume_strength = float(kwargs.get("min_volume_strength", 0.3))
        self.position_pct = float(kwargs.get("position_pct", 0.05))

    def fit_tick(self, row: dict[str, Any]) -> dict[str, Any] | None:
        try:
            s = (
                float(row["jev_strength"])
                if row.get("jev_strength") is not None
                else None
            )
            c5 = float(row["change_5m"]) if row.get("change_5m") is not None else None
            vs = (
                float(row["volume_strength"])
                if row.get("volume_strength") is not None
                else None
            )
        except (TypeError, ValueError):
            return None
        if s is None or c5 is None or vs is None:
            return None
        if not (
            s >= self.min_jev_strength
            and c5 >= self.min_change_5m
            and vs >= self.min_volume_strength
        ):
            return None
        return {
            "size_pct": self.position_pct,
            "reason": (
                f"jev={s:.4f}>={self.min_jev_strength}"
                f" change_5m={c5:.3f}>={self.min_change_5m}"
                f" vs={vs:.3f}>={self.min_volume_strength}"
            ),
            "jev_strength": s,
            "change_5m": c5,
            "volume_strength": vs,
        }
