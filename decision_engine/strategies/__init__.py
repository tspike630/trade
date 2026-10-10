"""Strategy registry for decision_engine (Step 1 + Step 2 scaffolds)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decision_engine.strategies.base_strategy import BaseStrategy

from decision_engine.strategies.combined_strategy import CombinedJevTechStrategy
from decision_engine.strategies.jev_strategy import JevStrengthStrategy
from decision_engine.strategies.tech_strategy import TechMomentumStrategy

STRATEGY_REGISTRY: dict[str, type] = {
    JevStrengthStrategy.strategy_id: JevStrengthStrategy,
    TechMomentumStrategy.strategy_id: TechMomentumStrategy,
    CombinedJevTechStrategy.strategy_id: CombinedJevTechStrategy,
}

# Convenience aliases used by backtest CLI --strategy all / A|B|C
STRATEGY_ALIASES: dict[str, str] = {
    "a": "jev_strength_v1",
    "b": "tech_momentum_v1",
    "c": "combined_jev_tech_v1",
    "jev": "jev_strength_v1",
    "tech": "tech_momentum_v1",
    "combined": "combined_jev_tech_v1",
}


def resolve_strategy_id(name: str) -> str:
    key = (name or "").strip()
    low = key.lower()
    if low in STRATEGY_ALIASES:
        return STRATEGY_ALIASES[low]
    return key


def get_strategy(strategy_id: str, **kwargs) -> "BaseStrategy":
    sid = resolve_strategy_id(strategy_id)
    cls = STRATEGY_REGISTRY.get(sid)
    if cls is None:
        known = ", ".join(sorted(STRATEGY_REGISTRY)) or "(none)"
        raise KeyError(f"unknown strategy_id={strategy_id!r}; known: {known}")
    return cls(**kwargs)


def list_strategies() -> list[str]:
    return sorted(STRATEGY_REGISTRY)
