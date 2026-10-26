"""Strategy registry for decision_engine (Step 1 + Step 2 + plan 03 factor combos)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decision_engine.strategies.base_strategy import BaseStrategy

from decision_engine.strategies.combined_strategy import CombinedJevTechStrategy
from decision_engine.strategies.factor_strategy import FactorStrategy
from decision_engine.strategies.jev_strategy import JevStrengthStrategy
from decision_engine.strategies.selfbuilt_momentum_a import SelfbuiltMomentumAStrategy
from decision_engine.strategies.tech_strategy import TechMomentumStrategy

# Legacy strategies stay registered (behavior unchanged; regression baseline).
STRATEGY_REGISTRY: dict[str, type] = {
    JevStrengthStrategy.strategy_id: JevStrengthStrategy,
    TechMomentumStrategy.strategy_id: TechMomentumStrategy,
    CombinedJevTechStrategy.strategy_id: CombinedJevTechStrategy,
    SelfbuiltMomentumAStrategy.strategy_id: SelfbuiltMomentumAStrategy,
}

# Plan 03: factor_ combos. Tuple is (buy, sell, sell_params, mode).
# mode "or" = first hit; "and" = every buy factor must hit.
# Original three keep sell=noop / mode=or so previously verified behavior stays put.
# New combos are registry-only (not the live default).
_FACTOR_COMBOS: dict[
    str, tuple[list[str], list[str], dict[str, dict[str, float]], str]
] = {
    "factor_jev_strength_v1": (["jev_strength"], ["noop"], {}, "or"),
    "factor_tech_momentum_v1": (["tech_momentum"], ["noop"], {}, "or"),
    "factor_combined_jev_tech_v1": (["combined_jev_tech"], ["noop"], {}, "or"),
    "factor_jev_timeexit_v1": (
        ["jev_strength"],
        ["time_exit"],
        {"time_exit": {"max_hold_seconds": 3600.0}},
        "or",
    ),
    "factor_jev_trail_v1": (
        ["jev_strength"],
        ["trail_stop"],
        {"trail_stop": {"trail_pct": 0.20}},
        "or",
    ),
    "factor_rsi_bounce_v1": (["rsi_bounce"], ["noop"], {}, "or"),
    "factor_macd_cross_v1": (["macd_cross"], ["noop"], {}, "or"),
    "factor_boll_breakout_v1": (["boll_breakout"], ["noop"], {}, "or"),
    "factor_jev_rsi_and_v1": (
        ["jev_strength", "rsi_bounce"],
        ["noop"],
        {},
        "and",
    ),
}


def _make_factor_strategy(sid: str) -> type:
    """Factory class so get_strategy(sid, **kwargs) builds the registered combo."""

    def _init(self: FactorStrategy, **kw: Any) -> None:
        buy_factors, sell_factors, sell_params, mode = _FACTOR_COMBOS[sid]
        FactorStrategy.__init__(
            self,
            strategy_id=sid,
            buy_factors=buy_factors,
            sell_factors=sell_factors,
            sell_params=sell_params,
            mode=mode,
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
    "selfbuilt_a": "selfbuilt_momentum_a_v1",
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

# Buy factors that run the shared jev_gate (jev_strategy.jev_gate: jev_stale /
# jev_stance_not_bullish, emitting jev_age_min / jev_stance_raw extras).
JEV_GATED_BUY_FACTORS: frozenset[str] = frozenset({"jev_strength", "combined_jev_tech"})

# Canonical list of strategy ids whose buys are JEV-gated and carry the jev
# extras. execution/gateway.JEV_GUARD_STRATEGIES should equal this set.
# Derived from the registry so new jev combos are picked up automatically.
JEV_GATED_STRATEGIES: frozenset[str] = frozenset(
    {JevStrengthStrategy.strategy_id, CombinedJevTechStrategy.strategy_id}
    | {sid for sid, (buy, *_r) in _FACTOR_COMBOS.items() if JEV_GATED_BUY_FACTORS & set(buy)}
)
