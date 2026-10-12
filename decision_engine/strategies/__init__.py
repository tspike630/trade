"""Strategy registry for decision_engine (Step 1 + Step 2 + plan 03 factor combos)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decision_engine.strategies.base_strategy import BaseStrategy

from decision_engine.strategies.combined_strategy import CombinedJevTechStrategy
from decision_engine.strategies.factor_strategy import FactorStrategy
from decision_engine.strategies.jev_strategy import JevStrengthStrategy
from decision_engine.strategies.tech_strategy import TechMomentumStrategy

# Legacy strategies stay registered (behavior unchanged; regression baseline).
STRATEGY_REGISTRY: dict[str, type] = {
    JevStrengthStrategy.strategy_id: JevStrengthStrategy,
    TechMomentumStrategy.strategy_id: TechMomentumStrategy,
    CombinedJevTechStrategy.strategy_id: CombinedJevTechStrategy,
}

# Plan 03: factor_ combos (equivalent buy logic, factor chain, noop sell).
_FACTOR_COMBOS: dict[str, tuple[list[str], list[str]]] = {
    "factor_jev_strength_v1": (["jev_strength"], ["noop"]),
    "factor_tech_momentum_v1": (["tech_momentum"], ["noop"]),
    "factor_combined_jev_tech_v1": (["combined_jev_tech"], ["noop"]),
}


def _make_factor_strategy(sid: str) -> type:
    """Factory class so get_strategy(sid, **kwargs) builds the registered combo."""

    def _init(self: FactorStrategy, **kw: Any) -> None:
        buy_factors, sell_factors = _FACTOR_COMBOS[sid]
        FactorStrategy.__init__(
            self,
            strategy_id=sid,
            buy_factors=buy_factors,
            sell_factors=sell_factors,
            **kw,
        )

    cls = type(
        f"FactorStrategy_{sid}",
        (FactorStrategy,),
        {"__init__": _init, "strategy_id": sid},
    )
    return cls


for _sid in _FACTOR_COMBOS:
    STRATEGY_REGISTRY[_sid] = _make_factor_strategy(_sid)

# Convenience aliases used by backtest CLI --strategy all / A|B|C.
# a|b|c stay on the legacy strategies (do not retarget at factor_*).
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
