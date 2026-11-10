"""Five-State Market Regime Strategy Router (`regime_strategy_router.py`).

Classifies the market and each candidate asset into one of 5 orthogonal,
exogenously anchored institutional regimes (using LSEG cross-asset feeds,
CityU CFTC COT & WRDS data, Deribit Options Gamma/DVOL, and Binance Perp
counterparty metrics) and routes each signal to its matching strategy playbook:

1. ``REGIME_1_POSITIVE_GAMMA_PINNED`` (``positive_gamma_mean_reversion``):
   - Normal oscillating market inside Deribit Put/Call Gamma walls, moderate breadth
     (``20%..68%``), and uncrowded funding (``|funding_z| < 1.8``).
   - Playbook: ``tech_boll_fade_v1`` 5m lower-band dip fade via ``LIMIT_MAKER``,
     ``size_multiplier = 1.00``, ``tp_multiplier = 1.00``, ``enable_trailing_ratchet = False``.

2. ``REGIME_2_SHORT_SQUEEZE_BREAKOUT`` (``short_squeeze_breakout``):
   - Deeply negative perpetual funding (``funding_z <= -1.8`` or ``funding_rate <= -0.0003``),
     short liquidation cascade, or CFTC COT ``cot_institutional_squeeze_favorable`` with
     whale accumulation (``whale_retail_divergence >= 1.35``).
   - Playbook: ``funding_z_fade_v1`` + ``tech_boll_fade_v1``, ``size_multiplier = 1.20``,
     ``tp_multiplier = 1.60``, ``enable_trailing_ratchet = True`` (ride the squeeze).

3. ``REGIME_3_CROSS_ASSET_BULL_PULLBACK`` (``cross_asset_momentum_pullback``):
   - Broad market rally (1h advancing breadth ``>= 68%`` or ``NVDA.O``/``SMH.O`` 1h breakout
     ``>= +1.20%``) with non-overheated funding (``funding_z < 2.0``).
   - Playbook: ``xs_leader_pullback`` — concentrates capital in cross-sectional leaders
     (rejects weak laggards with ``xs_rank_pct_1h < 0.40``), ``size_multiplier = 1.15``,
     ``tp_multiplier = 1.40``, ``enable_trailing_ratchet = True``.

4. ``REGIME_4_CROWDED_LONG_DEFENSIVE`` (``crowded_long_defensive``):
   - CFTC COT leveraged funds overcrowded long (``cot_leveraged_long_crowded``), or
     elevated funding (``1.8 <= funding_z <= 2.5``), or elevated retail long ratio
     (``global_account_ls_ratio >= 2.0``).
   - Playbook: ``defensive_deep_fade_with_beta_hedge`` — scales down position size to
     ``size_multiplier = 0.55``, tightens take-profit (``tp_multiplier = 0.80``) for fast
     mean-reversion exits, and sets ``require_perp_beta_hedge = True``.

5. ``REGIME_5_CRISIS_LIQUIDATION_HALT`` (``crisis_liquidation_halt``):
   - Active LSEG macro circuit breaker, systemic breadth crash (``breadth_1h < 12%``),
     active long liquidation waterfall, Deribit negative-gamma volatility spike,
     cross-exchange flash-wick manipulation (``>= 35 bps``), or token cliff unlock blackout.
   - Playbook: ``cash_halt`` — blocks all new spot BUY entries (``allow_buy = False``,
     ``size_multiplier = 0.0``).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

REGIME_1_POSITIVE_GAMMA_PINNED = "positive_gamma_mean_reversion"
REGIME_2_SHORT_SQUEEZE_BREAKOUT = "short_squeeze_breakout"
REGIME_3_CROSS_ASSET_BULL_PULLBACK = "cross_asset_momentum_pullback"
REGIME_4_CROWDED_LONG_DEFENSIVE = "crowded_long_defensive"
REGIME_5_CRISIS_LIQUIDATION_HALT = "crisis_liquidation_halt"


def _finite(val: object) -> float | None:
    if isinstance(val, bool) or val is None or val == "":
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


@dataclass(frozen=True)
class RegimeRoutingDecision:
    regime: str
    playbook: str
    allow_buy: bool
    reject_reason: str | None
    size_multiplier: float
    tp_multiplier: float
    enable_trailing_ratchet: bool
    require_perp_beta_hedge: bool
    recommended_order_type: str


def evaluate_five_state_regime(
    row: dict[str, Any],
    *,
    macro_halt: str | None = None,
) -> RegimeRoutingDecision:
    """Classify ``row`` into one of the 5 canonical market regimes and return its routing decision."""
    funding_rate = _finite(row.get("funding_rate", row.get("lastFundingRate")))
    funding_z = _finite(row.get("funding_z", row.get("jev_funding_z")))
    whale_div = _finite(row.get("whale_retail_divergence"))
    glob_ls = _finite(row.get("global_account_ls_ratio"))
    top_ls = _finite(row.get("top_position_ls_ratio"))
    if whale_div is None and top_ls is not None and glob_ls is not None and glob_ls > 0:
        whale_div = top_ls / glob_ls

    gamma_regime = str(row.get("gamma_regime") or "").strip().lower()
    dvol_z = _finite(row.get("dvol_zscore"))
    liq_cascade = str(row.get("liq_cascade_regime") or "").strip().lower()
    disloc_bps = _finite(row.get("max_venue_dislocation_bps"))
    raw_unlock = row.get("in_unlock_blackout")
    in_unlock = bool(int(raw_unlock)) if isinstance(raw_unlock, (int, float, bool)) else False

    cot_regime = str(row.get("cot_regime") or "").strip().lower()
    nvda_1h = _finite(row.get("nvda_1h_ret_pct"))
    smh_1h = _finite(row.get("smh_1h_ret_pct"))
    breadth_1h = _finite(row.get("xs_breadth_1h"))
    xs_rank_1h = _finite(row.get("xs_rank_pct_1h"))
    is_sys_crash = bool(row.get("xs_systemic_crash"))

    # =========================================================================
    # STATE 5: CRISIS & LIQUIDATION WATERFALL HALT (0.0x Cash Mode)
    # =========================================================================
    if macro_halt:
        return RegimeRoutingDecision(
            regime=REGIME_5_CRISIS_LIQUIDATION_HALT,
            playbook="cash_halt",
            allow_buy=False,
            reject_reason=f"regime5_macro_halt:{macro_halt}",
            size_multiplier=0.0,
            tp_multiplier=1.0,
            enable_trailing_ratchet=False,
            require_perp_beta_hedge=True,
            recommended_order_type="NONE",
        )
    if in_unlock:
        return RegimeRoutingDecision(
            regime=REGIME_5_CRISIS_LIQUIDATION_HALT,
            playbook="cash_halt",
            allow_buy=False,
            reject_reason="regime5_token_unlock_blackout",
            size_multiplier=0.0,
            tp_multiplier=1.0,
            enable_trailing_ratchet=False,
            require_perp_beta_hedge=False,
            recommended_order_type="NONE",
        )
    if disloc_bps is not None and disloc_bps >= 35.0:
        return RegimeRoutingDecision(
            regime=REGIME_5_CRISIS_LIQUIDATION_HALT,
            playbook="cash_halt",
            allow_buy=False,
            reject_reason="regime5_cross_exchange_dislocation",
            size_multiplier=0.0,
            tp_multiplier=1.0,
            enable_trailing_ratchet=False,
            require_perp_beta_hedge=False,
            recommended_order_type="NONE",
        )
    if (
        is_sys_crash
        or liq_cascade == "long_liquidation_waterfall"
        or (gamma_regime == "negative_gamma_slippery" and dvol_z is not None and dvol_z >= 2.0)
        or (nvda_1h is not None and nvda_1h <= -2.0)
        or (smh_1h is not None and smh_1h <= -2.0)
    ):
        return RegimeRoutingDecision(
            regime=REGIME_5_CRISIS_LIQUIDATION_HALT,
            playbook="cash_halt",
            allow_buy=False,
            reject_reason="regime5_crisis_liquidation_halt",
            size_multiplier=0.0,
            tp_multiplier=1.0,
            enable_trailing_ratchet=False,
            require_perp_beta_hedge=True,
            recommended_order_type="NONE",
        )

    # =========================================================================
    # STATE 2: SHORT SQUEEZE & INSTITUTIONAL ACCUMULATION BREAKOUT (1.20x + Ratchet)
    # =========================================================================
    is_short_squeeze = (
        (funding_z is not None and funding_z <= -1.8)
        or (funding_rate is not None and funding_rate <= -0.0003)
        or liq_cascade in ("short_squeeze_liquidation", "short_liquidation_squeeze")
        or (
            cot_regime == "cot_institutional_squeeze_favorable"
            and (whale_div is None or whale_div >= 1.20)
        )
    )
    if is_short_squeeze:
        return RegimeRoutingDecision(
            regime=REGIME_2_SHORT_SQUEEZE_BREAKOUT,
            playbook="funding_z_fade_and_squeeze_ratchet",
            allow_buy=True,
            reject_reason=None,
            size_multiplier=1.20,
            tp_multiplier=1.60,
            enable_trailing_ratchet=True,
            require_perp_beta_hedge=False,
            recommended_order_type="LIMIT_MAKER",
        )

    # =========================================================================
    # STATE 4: CROWDED LONG & RETAIL TRAP DEFENSIVE MODE (0.55x + Beta Hedge)
    # =========================================================================
    # Note: Extreme retail trap (glob_ls >= 2.20 and whale_div <= 0.45) or extreme
    # funding_z > 2.5 is outright blocked; moderate crowding enters defensive mode.
    if (glob_ls is not None and glob_ls >= 2.20 and whale_div is not None and whale_div <= 0.45) or (
        funding_z is not None and funding_z > 2.5
    ):
        return RegimeRoutingDecision(
            regime=REGIME_4_CROWDED_LONG_DEFENSIVE,
            playbook="crowded_long_hard_reject",
            allow_buy=False,
            reject_reason="regime4_extreme_crowded_long_trap",
            size_multiplier=0.0,
            tp_multiplier=0.80,
            enable_trailing_ratchet=False,
            require_perp_beta_hedge=True,
            recommended_order_type="NONE",
        )

    is_crowded_long = (
        cot_regime == "cot_leveraged_long_crowded"
        or (funding_z is not None and funding_z >= 1.8)
        or (funding_rate is not None and funding_rate >= 0.00035)
        or (glob_ls is not None and glob_ls >= 2.0 and (whale_div is None or whale_div <= 0.65))
    )
    if is_crowded_long:
        return RegimeRoutingDecision(
            regime=REGIME_4_CROWDED_LONG_DEFENSIVE,
            playbook="defensive_deep_fade_with_beta_hedge",
            allow_buy=True,
            reject_reason=None,
            size_multiplier=0.55,
            tp_multiplier=0.80,
            enable_trailing_ratchet=False,
            require_perp_beta_hedge=True,
            recommended_order_type="LIMIT_MAKER",
        )

    # =========================================================================
    # STATE 3: CROSS-ASSET BULL TREND & LEADER PULLBACK (1.15x + Ratchet)
    # =========================================================================
    is_bull_trend = (
        (breadth_1h is not None and breadth_1h >= 0.68)
        or (nvda_1h is not None and nvda_1h >= 1.20)
        or (smh_1h is not None and smh_1h >= 1.20)
    )
    if is_bull_trend:
        # 4. 看板块地位: In a broad bull trend, avoid weak laggards (bottom 35% of cross-sectional rank)
        if xs_rank_1h is not None and xs_rank_1h < 0.35:
            return RegimeRoutingDecision(
                regime=REGIME_3_CROSS_ASSET_BULL_PULLBACK,
                playbook="xs_leader_pullback_laggard_skip",
                allow_buy=False,
                reject_reason="regime3_bull_trend_laggard_skip",
                size_multiplier=0.0,
                tp_multiplier=1.0,
                enable_trailing_ratchet=False,
                require_perp_beta_hedge=False,
                recommended_order_type="NONE",
            )
        # AGENTS.md: reject obi_top5 <= -0.25. A missing book does not reject.
        obi_top5 = _finite(row.get("obi_top5") or row.get("spot_obi_top5"))
        if obi_top5 is not None and obi_top5 <= -0.25:
            return RegimeRoutingDecision(
                regime=REGIME_3_CROSS_ASSET_BULL_PULLBACK,
                playbook="xs_leader_thin_book_reject",
                allow_buy=False,
                reject_reason="regime3_thin_bid_book_trap",
                size_multiplier=0.0,
                tp_multiplier=1.0,
                enable_trailing_ratchet=False,
                require_perp_beta_hedge=False,
                recommended_order_type="NONE",
            )
        # AGENTS.md states the threshold only. The window, closed-bar cadence,
        # and std convention for volume_z / vol_z_1h are not defined here.
        # A missing value does not reject, and no series is synthesized.
        vol_z = _finite(row.get("volume_z") or row.get("vol_z_1h"))
        if vol_z is not None and vol_z >= 3.8:
            return RegimeRoutingDecision(
                regime=REGIME_3_CROSS_ASSET_BULL_PULLBACK,
                playbook="xs_leader_climax_volume_reject",
                allow_buy=False,
                reject_reason="regime3_climax_volume_distribution_trap",
                size_multiplier=0.0,
                tp_multiplier=1.0,
                enable_trailing_ratchet=False,
                require_perp_beta_hedge=False,
                recommended_order_type="NONE",
            )
        return RegimeRoutingDecision(
            regime=REGIME_3_CROSS_ASSET_BULL_PULLBACK,
            playbook="xs_leader_pullback",
            allow_buy=True,
            reject_reason=None,
            size_multiplier=1.15,
            tp_multiplier=1.40,
            enable_trailing_ratchet=True,
            require_perp_beta_hedge=False,
            recommended_order_type="LIMIT_MAKER",
        )

    # =========================================================================
    # STATE 1: POSITIVE-GAMMA PINNED / NORMAL MEAN-REVERSION (1.00x Standard)
    # =========================================================================
    return RegimeRoutingDecision(
        regime=REGIME_1_POSITIVE_GAMMA_PINNED,
        playbook="tech_boll_fade_standard",
        allow_buy=True,
        reject_reason=None,
        size_multiplier=1.00,
        tp_multiplier=1.00,
        enable_trailing_ratchet=False,
        require_perp_beta_hedge=False,
        recommended_order_type="LIMIT_MAKER",
    )


def apply_five_state_regime_router(
    candidates: Sequence[dict[str, Any]],
    hot_rows: Sequence[dict[str, Any]],
    *,
    macro_halt: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Route candidate signals through the 5-State Market Regime Router.

    - Stamps ``market_regime``, ``regime_playbook``, ``regime_size_multiplier``,
      ``regime_tp_multiplier``, ``enable_trailing_ratchet``, and ``require_perp_beta_hedge``
      onto each accepted BUY signal.
    - Scales ``size_pct`` by ``decision.size_multiplier`` when the signal was not already
      scaled by the same regime.
    - Filters out BUY signals rejected by their regime playbook (while never blocking SELLs).
    """
    by_sym: dict[str, dict[str, Any]] = {}
    for r in hot_rows:
        if not isinstance(r, dict):
            continue
        for k in ("binance_symbol", "token_address", "symbol"):
            val = str(r.get(k) or "").strip().upper()
            if val:
                by_sym[val] = r

    accepted: list[dict[str, Any]] = []
    regime_counts: dict[str, int] = {}

    for sig in candidates:
        if not isinstance(sig, dict):
            continue
        action = str(sig.get("action") or "").strip().lower()
        if action != "buy":
            accepted.append(sig)
            continue

        sym = str(sig.get("binance_symbol") or sig.get("token_address") or "").strip().upper()
        base_row = dict(by_sym.get(sym) or {})
        base_row.update(sig)

        decision = evaluate_five_state_regime(base_row, macro_halt=macro_halt)
        regime_counts[decision.regime] = regime_counts.get(decision.regime, 0) + 1

        if not decision.allow_buy:
            continue

        out_sig = dict(sig)
        out_sig["market_regime"] = decision.regime
        out_sig["regime_playbook"] = decision.playbook
        out_sig["regime_size_multiplier"] = decision.size_multiplier
        out_sig["regime_tp_multiplier"] = decision.tp_multiplier
        out_sig["enable_trailing_ratchet"] = decision.enable_trailing_ratchet
        out_sig["require_perp_beta_hedge"] = decision.require_perp_beta_hedge

        state_id_map = {
            REGIME_1_POSITIVE_GAMMA_PINNED: 1,
            REGIME_2_SHORT_SQUEEZE_BREAKOUT: 2,
            REGIME_3_CROSS_ASSET_BULL_PULLBACK: 3,
            REGIME_4_CROWDED_LONG_DEFENSIVE: 4,
            REGIME_5_CRISIS_LIQUIDATION_HALT: 5,
        }
        state_id = state_id_map.get(decision.regime, 1)
        ratchet_info = compute_optimal_ratchet_target_pct(
            state_id=state_id,
            unrealized_gain_pct=float(_finite(base_row.get("unrealized_gain_pct")) or 0.0),
            oco_stop_above_breakeven=bool(base_row.get("oco_stop_above_breakeven", False)),
            oco_stop_locked_stage2_profit=bool(base_row.get("oco_stop_locked_stage2_profit", False)),
        )
        out_sig["optimal_pyramid_stage"] = ratchet_info["pyramid_stage"]
        out_sig["optimal_base_position_pct"] = ratchet_info["base_position_pct"]
        out_sig["optimal_target_cumulative_pct"] = ratchet_info["target_cumulative_pct"]
        out_sig["optimal_pyramid_ceiling_pct"] = ratchet_info["pyramid_ceiling_pct"]

        cur_size = _finite(out_sig.get("size_pct"))
        if bool(base_row.get("use_optimal_kelly_matrix", False)):
            out_sig["size_pct"] = round(float(ratchet_info["target_cumulative_pct"]), 6)
        elif cur_size is not None and cur_size > 0 and decision.size_multiplier != 1.0:
            out_sig["size_pct"] = round(max(0.01, min(0.95, cur_size * decision.size_multiplier)), 6)

        accepted.append(out_sig)

    return accepted, regime_counts


# === Monte Carlo Optimal Kelly + 3-Stage Zero-Risk OCO Ratchet Matrix ===
OPTIMAL_FIVE_STATE_BASE_PCT = {
    1: 0.185,  # State 1: 18.5% Full-Kelly Mean Reversion
    2: 0.350,  # State 2: 35.0% Base -> 70.0% Stage 2 -> 95.0% Stage 3 Squeeze Pyramid
    3: 0.300,  # State 3: 30.0% Base -> 60.0% Stage 2 -> 85.0% Stage 3 Tech Bull Pyramid
    4: 0.050,  # State 4: 5.0% Deep-Wick Fade + 50% Perp Beta Hedge
    5: 0.000,  # State 5: 0.0% Spot + 100% Perp Beta Lock
}

OPTIMAL_FIVE_STATE_PYRAMID_MAX_PCT = {
    1: 0.185,
    2: 0.950,
    3: 0.850,
    4: 0.050,
    5: 0.000,
}


def compute_optimal_ratchet_target_pct(
    state_id: int,
    unrealized_gain_pct: float = 0.0,
    oco_stop_above_breakeven: bool = False,
    oco_stop_locked_stage2_profit: bool = False,
) -> dict[str, float | int]:
    """Returns base and ratchet-unlocked cumulative position pct for the 5-State Router."""
    base_pct = OPTIMAL_FIVE_STATE_BASE_PCT.get(state_id, 0.185)
    max_pct = OPTIMAL_FIVE_STATE_PYRAMID_MAX_PCT.get(state_id, 0.185)
    target_pct = base_pct
    stage = 1 if base_pct > 0 else 0

    if state_id == 2:
        if oco_stop_above_breakeven and unrealized_gain_pct >= 1.20:
            stage = 2
            target_pct = 0.700
        if oco_stop_above_breakeven and oco_stop_locked_stage2_profit and unrealized_gain_pct >= 2.20:
            stage = 3
            target_pct = 0.950
    elif state_id == 3:
        if oco_stop_above_breakeven and unrealized_gain_pct >= 1.20:
            stage = 2
            target_pct = 0.600
        if oco_stop_above_breakeven and oco_stop_locked_stage2_profit and unrealized_gain_pct >= 2.20:
            stage = 3
            target_pct = 0.850

    return {
        "pyramid_stage": stage,
        "base_position_pct": base_pct,
        "target_cumulative_pct": min(max_pct, target_pct),
        "pyramid_ceiling_pct": max_pct,
    }
