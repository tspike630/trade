"""Buy factor registry (plan 03)."""

from __future__ import annotations

from decision_engine.factors.buy.combined_jev_tech import CombinedJevTechBuyFactor
from decision_engine.factors.buy.jev_strength import JevStrengthBuyFactor
from decision_engine.factors.buy.tech_momentum import TechMomentumBuyFactor

BUY_FACTOR_REGISTRY: dict[str, type] = {
    JevStrengthBuyFactor.factor_name: JevStrengthBuyFactor,
    TechMomentumBuyFactor.factor_name: TechMomentumBuyFactor,
    CombinedJevTechBuyFactor.factor_name: CombinedJevTechBuyFactor,
}
