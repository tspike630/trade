"""JEV strength buy factor — logic identical to JevStrengthStrategy.on_tick."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorBuyBase
from decision_engine.strategies.jev_strategy import gate_defaults, jev_gate


class JevStrengthBuyFactor(FactorBuyBase):
    factor_name = "jev_strength"

    def _init_self(self, **kwargs: Any) -> None:
        self.min_jev_strength = float(kwargs.get("min_jev_strength", 0.3))
        self.position_pct = float(kwargs.get("position_pct", 0.05))
        self.max_age_min, self.require_bullish_stance = gate_defaults(
            kwargs.get("max_age_min"), kwargs.get("require_bullish_stance"))
        self.rejected = {"jev_stale": 0, "jev_stance_not_bullish": 0}

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
        why, jx = jev_gate(row, self.max_age_min, self.require_bullish_stance)
        if why is not None:
            self.rejected[why] += 1
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
            **jx,
        }
