"""JEV strength buy factor — logic identical to JevStrengthStrategy.on_tick."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorBuyBase


class JevStrengthBuyFactor(FactorBuyBase):
    factor_name = "jev_strength"

    def _init_self(self, **kwargs: Any) -> None:
        self.min_jev_strength = float(kwargs.get("min_jev_strength", 0.3))
        self.position_pct = float(kwargs.get("position_pct", 0.05))

    def fit_tick(self, row: dict[str, Any]) -> dict[str, Any] | None:
        strength = row.get("jev_strength")
        if strength is None:
            return None
        try:
            s = float(strength)
        except (TypeError, ValueError):
            return None
        # Same predicate as JevStrengthStrategy (`s >= min`), including NaN.
        if not (s >= self.min_jev_strength):
            return None
        return {
            "size_pct": self.position_pct,
            "reason": (
                f"jev_strength={s:.4f}>={self.min_jev_strength}"
                f" signal={row.get('jev_signal')} score={row.get('jev_score')}"
            ),
            "jev_strength": s,
            "jev_signal": row.get("jev_signal"),
            "jev_score": row.get("jev_score"),
        }
