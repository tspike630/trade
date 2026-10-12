"""Time-based exit sell factor (skeleton, off by default via FactorStrategy config)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from decision_engine.factors.base import FactorSellBase


def _as_epoch(value: Any) -> float | None:
    """Epoch seconds, or an ISO timestamp (ledger rows use opened_at)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class TimeExitSellFactor(FactorSellBase):
    factor_name = "time_exit"

    def _init_self(self, **kwargs: Any) -> None:
        self.max_hold_seconds = float(kwargs.get("max_hold_seconds", 3600.0))

    def fit_position(
        self, position: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        opened_ts = _as_epoch(position.get("opened_ts"))
        if opened_ts is None:
            # Backtest ledger positions carry opened_at (ISO), not opened_ts.
            opened_ts = _as_epoch(position.get("opened_at"))
        if opened_ts is None:
            return None
        now_raw = data.get("now_ts")
        if now_raw is None:
            # Replay ticks stamp the bar, not the wall clock.
            now_raw = data.get("bar_ts")
        if now_raw is None:
            now_raw = time.time()
        now_ts = _as_epoch(now_raw)
        if now_ts is None:
            return None
        age = now_ts - opened_ts
        if age < self.max_hold_seconds:
            return None
        return {
            "size_pct": position.get("size_pct", 1.0),
            "reason": f"time_exit age={age:.0f}s>={self.max_hold_seconds:.0f}s",
        }
