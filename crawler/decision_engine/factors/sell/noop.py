"""Noop sell factor — default, keeps current behavior (no sell signals)."""

from __future__ import annotations

from typing import Any

from decision_engine.factors.base import FactorSellBase


class NoopSellFactor(FactorSellBase):
    factor_name = "noop"

    def fit_position(
        self, position: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        return None
