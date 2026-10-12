"""Factor registries for decision_engine (plan 03, buy/sell factor decoupling)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decision_engine.factors.base import FactorBuyBase, FactorSellBase

from decision_engine.factors.buy import BUY_FACTOR_REGISTRY
from decision_engine.factors.sell import SELL_FACTOR_REGISTRY

# Unified registry (buy factor names + sell factor names).
FACTOR_REGISTRY: dict[str, type] = {**BUY_FACTOR_REGISTRY, **SELL_FACTOR_REGISTRY}


def get_buy_factor(factor_name: str, **params) -> "FactorBuyBase":
    cls = BUY_FACTOR_REGISTRY.get(factor_name)
    if cls is None:
        known = ", ".join(sorted(BUY_FACTOR_REGISTRY)) or "(none)"
        raise KeyError(f"unknown buy_factor={factor_name!r}; known: {known}")
    return cls(**params)


def get_sell_factor(factor_name: str, **params) -> "FactorSellBase":
    cls = SELL_FACTOR_REGISTRY.get(factor_name)
    if cls is None:
        known = ", ".join(sorted(SELL_FACTOR_REGISTRY)) or "(none)"
        raise KeyError(f"unknown sell_factor={factor_name!r}; known: {known}")
    return cls(**params)


def list_buy_factors() -> list[str]:
    return sorted(BUY_FACTOR_REGISTRY)


def list_sell_factors() -> list[str]:
    return sorted(SELL_FACTOR_REGISTRY)
