"""Strategy registry for decision_engine (Step 1 + Step 2 + plan 03 factor combos)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decision_engine.strategies.base_strategy import BaseStrategy

from decision_engine.strategies.combined_strategy import CombinedJevTechStrategy
from decision_engine.strategies.factor_strategy import FactorStrategy
from decision_engine.strategies.jev_strategy import JevStrengthStrategy
from decision_engine.strategies.selfbuilt_momentum_a import SelfbuiltMomentumAStrategy
from decision_engine.strategies.swing_established import SwingEstablishedStrategy
from decision_engine.strategies.rel_strength_m import RelStrengthMStrategy

from decision_engine.strategies.vol_spike_entry import VolSpikeEntryStrategy
from decision_engine.strategies.liq_bucket import LiqBucketHighStrategy, LiqBucketMidStrategy
from decision_engine.strategies.and_selfbuilt_macd import AndSelfbuiltMacdStrategy
from decision_engine.strategies.and_vol_squeeze_break import AndVolSqueezeBreakStrategy
from decision_engine.strategies.filt_killfirst_qf import FiltKillfirstQfStrategy
from decision_engine.strategies.filt_obs import FiltObsBsRatioStrategy, FiltObsBuyersNStrategy
from decision_engine.strategies.rel_strength_10d import RelStrength10dStrategy
from decision_engine.strategies.swing_pullback import SwingPullbackStrategy
from decision_engine.strategies.filt_liq_ladder import FiltLiqLadderStrategy
from decision_engine.strategies.vol_climax_mcap_band import VolClimaxMcapBandStrategy
from decision_engine.strategies.and_vol_climax_swing import AndVolClimaxSwingStrategy
from decision_engine.strategies.and_vol_climax_selfbuilt import AndVolClimaxSelfbuiltStrategy
from decision_engine.strategies.and_swing_selfbuilt import AndSwingSelfbuiltStrategy
from decision_engine.strategies.portfolio_risk_sleeves import PortfolioRiskSleevesStrategy
from decision_engine.strategies.portfolio_and_blend import PortfolioAndBlendStrategy
from decision_engine.strategies.portfolio_tight_sl import PortfolioTightSlStrategy
from decision_engine.strategies.cs_mom_1w import CsMom1wStrategy
from decision_engine.strategies.lifecycle_momentum import LifecycleMomentumStrategy
from decision_engine.strategies.funding_z_fade import FundingZFadeStrategy
from decision_engine.strategies.tech_strategy import TechMomentumStrategy
from decision_engine.strategies.tech_fade import TechFadeStrategy
from decision_engine.strategies.cs_fade_1w import CsFade1wStrategy
from decision_engine.strategies.rs_fade_10d import RsFade10dStrategy
from decision_engine.strategies.vol_spike_fade import VolSpikeFadeStrategy
from decision_engine.strategies.rsi_bounce_fade import RsiBounceFadeStrategy
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from decision_engine.strategies.rsi_boll_fade import RsiBollFadeStrategy
from decision_engine.strategies.vol_squeeze_fade import VolSqueezeFadeStrategy
from decision_engine.strategies.cs_mom_1w_r90 import CsMom1wR90Strategy
from decision_engine.strategies.rsi_bounce_r90 import RsiBounceR90Strategy

# Legacy strategies stay registered (behavior unchanged; regression baseline).
STRATEGY_REGISTRY: dict[str, type] = {
    JevStrengthStrategy.strategy_id: JevStrengthStrategy,
    TechMomentumStrategy.strategy_id: TechMomentumStrategy,
    CombinedJevTechStrategy.strategy_id: CombinedJevTechStrategy,
    SelfbuiltMomentumAStrategy.strategy_id: SelfbuiltMomentumAStrategy,
    SwingEstablishedStrategy.strategy_id: SwingEstablishedStrategy,
    RelStrengthMStrategy.strategy_id: RelStrengthMStrategy,
    VolSpikeEntryStrategy.strategy_id: VolSpikeEntryStrategy,
    LiqBucketHighStrategy.strategy_id: LiqBucketHighStrategy,
    LiqBucketMidStrategy.strategy_id: LiqBucketMidStrategy,
    AndSelfbuiltMacdStrategy.strategy_id: AndSelfbuiltMacdStrategy,
    AndVolSqueezeBreakStrategy.strategy_id: AndVolSqueezeBreakStrategy,
    FiltKillfirstQfStrategy.strategy_id: FiltKillfirstQfStrategy,
    FiltObsBsRatioStrategy.strategy_id: FiltObsBsRatioStrategy,
    FiltObsBuyersNStrategy.strategy_id: FiltObsBuyersNStrategy,
    RelStrength10dStrategy.strategy_id: RelStrength10dStrategy,
    SwingPullbackStrategy.strategy_id: SwingPullbackStrategy,
    FiltLiqLadderStrategy.strategy_id: FiltLiqLadderStrategy,
    VolClimaxMcapBandStrategy.strategy_id: VolClimaxMcapBandStrategy,
    AndVolClimaxSwingStrategy.strategy_id: AndVolClimaxSwingStrategy,
    AndVolClimaxSelfbuiltStrategy.strategy_id: AndVolClimaxSelfbuiltStrategy,
    AndSwingSelfbuiltStrategy.strategy_id: AndSwingSelfbuiltStrategy,
    PortfolioRiskSleevesStrategy.strategy_id: PortfolioRiskSleevesStrategy,  # portfolio_risk_v1
    "portfolio_risk_sleeves_v1": PortfolioRiskSleevesStrategy,  # legacy id → same class
    PortfolioTightSlStrategy.strategy_id: PortfolioTightSlStrategy,  # portfolio_tight_sl_v1
    PortfolioAndBlendStrategy.strategy_id: PortfolioAndBlendStrategy,
    CsMom1wStrategy.strategy_id: CsMom1wStrategy,
    LifecycleMomentumStrategy.strategy_id: LifecycleMomentumStrategy,
    FundingZFadeStrategy.strategy_id: FundingZFadeStrategy,
    TechFadeStrategy.strategy_id: TechFadeStrategy,
    CsFade1wStrategy.strategy_id: CsFade1wStrategy,
    RsFade10dStrategy.strategy_id: RsFade10dStrategy,
    VolSpikeFadeStrategy.strategy_id: VolSpikeFadeStrategy,
    RsiBounceFadeStrategy.strategy_id: RsiBounceFadeStrategy,
    TechBollFadeStrategy.strategy_id: TechBollFadeStrategy,
    RsiBollFadeStrategy.strategy_id: RsiBollFadeStrategy,
    VolSqueezeFadeStrategy.strategy_id: VolSqueezeFadeStrategy,
    CsMom1wR90Strategy.strategy_id: CsMom1wR90Strategy,
    RsiBounceR90Strategy.strategy_id: RsiBounceR90Strategy,
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
    # Synthesis AND arms (杨 2026-10-30：策略综合；非 OR 杂烩)
    "factor_rsi_macd_and_v1": (
        ["rsi_bounce", "macd_cross"],
        ["noop"],
        {},
        "and",
    ),
    "factor_macd_boll_and_v1": (
        ["macd_cross", "boll_breakout"],
        ["noop"],
        {},
        "and",
    ),
    "factor_rsi_boll_and_v1": (
        ["rsi_bounce", "boll_breakout"],
        ["noop"],
        {},
        "and",
    ),
    "factor_tech_macd_and_v1": (
        ["tech_momentum", "macd_cross"],
        ["noop"],
        {},
        "and",
    ),
    "factor_tech_rsi_and_v1": (
        ["tech_momentum", "rsi_bounce"],
        ["noop"],
        {},
        "and",
    ),
    "factor_tech_boll_and_v1": (
        ["tech_momentum", "boll_breakout"],
        ["noop"],
        {},
        "and",
    ),
    "factor_jev_macd_and_v1": (
        ["jev_strength", "macd_cross"],
        ["noop"],
        {},
        "and",
    ),
    "factor_jev_boll_and_v1": (
        ["jev_strength", "boll_breakout"],
        ["noop"],
        {},
        "and",
    ),
    "factor_rsi_macd_timeexit_v1": (
        ["rsi_bounce", "macd_cross"],
        ["time_exit"],
        {"time_exit": {"max_hold_seconds": 3600.0}},
        "and",
    ),
    "factor_macd_trail_v1": (
        ["macd_cross"],
        ["trail_stop"],
        {"trail_stop": {"trail_pct": 0.20}},
        "or",
    ),
    "factor_macd_timestop_48h_v1": (
        ["macd_cross"],
        ["time_exit"],
        {"time_exit": {"max_hold_seconds": 172800.0}},
        "or",
    ),
    "factor_macd_tier_72h_v1": (
        ["macd_cross"],
        ["time_exit"],
        {"time_exit": {"max_hold_seconds": 259200.0}},
        "or",
    ),
    "factor_a_trail_v1": (
        ["tech_momentum"],
        ["trail_stop"],
        {"trail_stop": {"trail_pct": 0.20}},
        "or",
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
    "swing_established": "swing_established_v1",
    "rel_strength_m": "rel_strength_m_v1",
    "race_swing_established": "swing_established_v1",
    "race_rel_strength_m": "rel_strength_m_v1",
    "vol_spike_entry": "vol_spike_entry_v1",
    "race_vol_spike_entry": "vol_spike_entry_v1",
    "liq_bucket_high": "liq_bucket_high_v1",
    "race_liq_bucket_high": "liq_bucket_high_v1",
    "liq_bucket_mid": "liq_bucket_mid_v1",
    "race_liq_bucket_mid": "liq_bucket_mid_v1",
    "and_selfbuilt_macd": "and_selfbuilt_macd_v1",
    "race_and_selfbuilt_macd": "and_selfbuilt_macd_v1",
    "and_vol_squeeze_break": "and_vol_squeeze_break_v1",
    "race_and_vol_squeeze_break": "and_vol_squeeze_break_v1",
    "filt_killfirst_qf": "filt_killfirst_qf_v1",
    "race_filt_killfirst_qf": "filt_killfirst_qf_v1",
    "filt_obs_bs_ratio": "filt_obs_bs_ratio_v1",
    "race_filt_obs_bs_ratio": "filt_obs_bs_ratio_v1",
    "filt_obs_buyers_n": "filt_obs_buyers_n_v1",
    "race_filt_obs_buyers_n": "filt_obs_buyers_n_v1",
    "rel_strength_10d": "rel_strength_10d_v1",
    "race_rel_strength_10d": "rel_strength_10d_v1",
    "swing_pullback": "swing_pullback_v1",
    "race_swing_pullback": "swing_pullback_v1",
    "filt_liq_ladder": "filt_liq_ladder_v1",
    "race_filt_liq_ladder": "filt_liq_ladder_v1",
    "vol_climax_mcap_band": "vol_climax_mcap_band_v1",
    "race_vol_climax_mcap_band": "vol_climax_mcap_band_v1",
    "cs_mom_1w": "cs_mom_1w_v1",
    "race_cs_mom_1w": "cs_mom_1w_v1",
    "lifecycle_momentum": "lifecycle_momentum_v1",
    "race_lifecycle_momentum": "lifecycle_momentum_v1",
    "funding_z_fade": "funding_z_fade_v1",
    "race_funding_z_fade": "funding_z_fade_v1",
    "and_vol_climax_swing": "and_vol_climax_swing_v1",
    "race_and_vol_climax_swing": "and_vol_climax_swing_v1",
    "and_vol_climax_selfbuilt": "and_vol_climax_selfbuilt_v1",
    "race_and_vol_climax_selfbuilt": "and_vol_climax_selfbuilt_v1",
    "and_swing_selfbuilt": "and_swing_selfbuilt_v1",
    "race_and_swing_selfbuilt": "and_swing_selfbuilt_v1",
    # Locked tonight: portfolio_risk_v1 / portfolio_tight_sl_v1
    "portfolio_risk_v1": "portfolio_risk_v1",
    "portfolio_risk": "portfolio_risk_v1",
    "race_portfolio_risk": "portfolio_risk_v1",
    "portfolio_risk_sleeves": "portfolio_risk_v1",
    "portfolio_risk_sleeves_v1": "portfolio_risk_v1",
    "race_portfolio_risk_sleeves": "portfolio_risk_v1",
    "port_top3_equal_v1": "portfolio_risk_v1",
    "sleeve_top_v1": "portfolio_risk_v1",
    "portfolio_tight_sl": "portfolio_tight_sl_v1",
    "portfolio_tight_sl_v1": "portfolio_tight_sl_v1",
    "race_portfolio_tight_sl": "portfolio_tight_sl_v1",
    "sleeve_tight_sl_v1": "portfolio_tight_sl_v1",
    "portfolio_and_blend": "portfolio_and_blend_v1",
    "race_portfolio_and_blend": "portfolio_and_blend_v1",
    "tech_fade": "tech_fade_v1",
    "race_tech_fade": "tech_fade_v1",
    "cs_fade_1w": "cs_fade_1w_v1",
    "race_cs_fade_1w": "cs_fade_1w_v1",
    "rs_fade_10d": "rs_fade_10d_v1",
    "race_rs_fade_10d": "rs_fade_10d_v1",
    "vol_spike_fade": "vol_spike_fade_v1",
    "race_vol_spike_fade": "vol_spike_fade_v1",
    "rsi_bounce_fade": "rsi_bounce_fade_v1",
    "race_rsi_bounce_fade": "rsi_bounce_fade_v1",
    "tech_boll_fade": "tech_boll_fade_v1",
    "race_tech_boll_fade": "tech_boll_fade_v1",
    "rsi_boll_fade": "rsi_boll_fade_v1",
    "race_rsi_boll_fade": "rsi_boll_fade_v1",
    "vol_squeeze_fade": "vol_squeeze_fade_v1",
    "race_vol_squeeze_fade": "vol_squeeze_fade_v1",
    "cs_mom_1w_r90": "cs_mom_1w_r90_v1",
    "race_cs_mom_1w_r90": "cs_mom_1w_r90_v1",
    "rsi_bounce_r90": "rsi_bounce_r90_v1",
    "race_rsi_bounce_r90": "rsi_bounce_r90_v1",
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
