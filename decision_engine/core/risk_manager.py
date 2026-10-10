"""Risk manager Step 1 — passthrough placeholder (no real checks)."""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

log = get_logger("decision_engine.risk")


class RiskManager:
    """Passthrough: mark risk_passed=True and return signal unchanged."""

    def check(self, signal: dict[str, Any]) -> dict[str, Any]:
        out = dict(signal)
        out["risk_passed"] = True
        return out

    def filter(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.check(s) for s in signals]
