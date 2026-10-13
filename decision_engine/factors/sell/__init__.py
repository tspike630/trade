"""Sell factor registry (plan 03)."""

from __future__ import annotations

from decision_engine.factors.sell.noop import NoopSellFactor
from decision_engine.factors.sell.time_exit import TimeExitSellFactor
from decision_engine.factors.sell.trail_stop import TrailStopSellFactor

SELL_FACTOR_REGISTRY: dict[str, type] = {
    NoopSellFactor.factor_name: NoopSellFactor,
    TimeExitSellFactor.factor_name: TimeExitSellFactor,
    TrailStopSellFactor.factor_name: TrailStopSellFactor,
}
