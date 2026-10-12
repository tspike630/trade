"""Time-based exit sell factor (skeleton, off by default via FactorStrategy config)."""

from __future__ import annotations

import time
from typing import Any

from decision_engine.factors.base import FactorSellBase


class TimeExitSellFactor(FactorSellBase):
    factor_name = "time_exit"

    def _init_self(self, **kwargs: Any) -> None:
        self.max_hold_seconds = float(kwargs.get("max_hold_seconds", 3600.0))

    def fit_position(
        self, position: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        opened_ts = position.get("opened_ts")
        if opened_ts is None:
            return None
        now_ts = data.get("now_ts") or time.time()
        try:
            age = float(now_ts) - float(opened_ts)
        except (TypeError, ValueError):
            return None
        if age < self.max_hold_seconds:
            return None
        return {
            "size_pct": position.get("size_pct", 1.0),
            "reason": f"time_exit age={age:.0f}s>={self.max_hold_seconds:.0f}s",
        }
