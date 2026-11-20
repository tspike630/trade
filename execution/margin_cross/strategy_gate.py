"""Approved 5-state strategy routing, real-time operator-configured sizing, and freshness gates.

Wires the cross-margin track to:
1. Data-staleness fail-closed clocks (indicators 3600s, L2 OBI 3600s, perp
   counterparty 3600s, Deribit gamma 3600s, 1h volume_z source close 3600s,
   whitelist news ``fetched_at`` 90m, LSEG 5m cross-asset bars 90m, and CFTC COT
   publication calendar ``due_cftc_asof``).
2. Required exogenous classifier input completeness (blocks silent fallthrough to
   State 1 when market distinguishers are absent).
3. Approved 5-state directional & defensive routing (anti-squeeze hard veto on
   shorts; State 4/5 short playbooks; State 1/3 long playbooks with State 2/4
   gated off unless explicitly enabled).
4. Zero-default operator-approved Cross Margin net-asset sizing:
   requires ``CROSS_MARGIN_PARAMETERS_APPROVED=1`` plus an explicit zero-default
   ``ApprovedRiskConfig`` (no pre-installed leverage, margin, borrow, or sizing numbers).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
import json
import sqlite3
from pathlib import Path
from typing import Any

from decision_engine.core.binance_indicators import binance_indicators_fresh_sec
from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from decision_engine.core.conviction_whitelist import get_conviction_tier
from decision_engine.core.macro_circuit_breaker import MAX_CROSS_ASSET_AGE_MS
from decision_engine.core.news_shock_gate import NEWS_WINDOW_MIN
from decision_engine.core.perp_short_alpha_engine import is_short_squeeze_veto
from decision_engine.core.portfolio_clusters import cluster_exposure_key
from decision_engine.core.regime_v2_policy import (
    DEFAULT_BASE_POSITION_PCT,
    DEFAULT_MAX_SINGLE_EXPOSURE_PCT,
    DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
    DEFAULT_STATE4_MAX_TOTAL_EXPOSURE_PCT,
)
from decision_engine.core.regime_strategy_router import (
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    REGIME_5_CRISIS_LIQUIDATION_HALT,
    evaluate_five_state_regime,
)
from decision_engine.core.volume_z_1h import source_close_is_fresh
from execution.margin_cross.money import api_amount, on_grid, parse_decimal
from execution.margin_cross.policy import (
    APPROVED_CONTRACT_LABEL,
    ApprovedRiskConfig,
    fill_tp_formula_b_enabled,
    load_approved_risk_config,
    parameters_approved_by_env,
    parse_approved_risk_config,
    state2_long_enabled,
    state4_long_enabled,
    validate_approved_config_window,
)
from execution.margin_cross.risk import PURPOSE_CROSS_MARGIN_TRACK, Snapshot, parse_snapshot

UNRULED_KELLY_FRACTIONS = (
    Decimal("0.05"),
    Decimal("0.185"),
    Decimal("0.20"),
    Decimal("0.30"),
    Decimal("0.35"),
    Decimal("0.45"),
    Decimal("0.60"),
    Decimal("0.70"),
    Decimal("0.85"),
    Decimal("0.95"),
)


# Stable missing-input keys for diagnostics. External reason_code stays
# "regime_required_inputs_missing"; these names are additive only.
MISSING_INPUT_KEYS: tuple[str, ...] = (
    "funding",
    "breadth",
    "obi_top5",
    "volume_z",
    "cot_state",
    "options_gamma_dvol",
    "lseg_returns",
    "lseg_market_open",
)

_MISSING_INPUT_CODE_MAP: dict[str, str] = {
    "funding_missing": "funding",
    "xs_breadth_1h_missing": "breadth",
    "obi_top5_missing": "obi_top5",
    "volume_z_missing": "volume_z",
    "cot_regime_missing": "cot_state",
    "options_gamma_dvol_missing": "options_gamma_dvol",
    "lseg_cross_asset_returns_missing": "lseg_returns",
    "lseg_market_open_missing": "lseg_market_open",
}

REGIME_REQUIRED_INPUTS_REASON = "regime_required_inputs_missing"


def canonical_missing_inputs(internal_codes: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Map internal *_missing reason fragments to the stable diagnostic keys."""
    out: list[str] = []
    seen: set[str] = set()
    for code in internal_codes:
        key = _MISSING_INPUT_CODE_MAP.get(code)
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return tuple(out)


def missing_inputs_histogram(rows: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]) -> dict[str, int]:
    """Per-tick field histogram over a universe of rows (unique row counts)."""
    hist = {key: 0 for key in MISSING_INPUT_KEYS}
    symbols_missing = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        keys = canonical_missing_inputs(check_required_regime_inputs(row))
        if keys:
            symbols_missing += 1
        for key in keys:
            hist[key] += 1
    hist["symbols_missing"] = symbols_missing
    return hist


@dataclass(frozen=True)
class StrategyGateDecision:
    allowed: bool
    regime: str
    strategy_id: str
    action: str
    size_multiplier: Decimal
    tp_multiplier: Decimal
    reasons: tuple[str, ...]
    missing_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovedPositionPlan:
    allowed: bool
    reasons: tuple[str, ...]
    regime: str
    strategy_id: str
    net_asset_quote: Decimal
    effective_leverage_after: Decimal
    target_notional_quote: Decimal
    quantity: Decimal
    intent: dict[str, Any] | None
    contract: dict[str, Any] | None


def _whole_ms(raw: object) -> int | None:
    if isinstance(raw, bool) or raw is None or raw == "":
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return None


def _age_invalid(ts_ms: int | None, now_ms: int, window_ms: int) -> bool:
    if ts_ms is None:
        return True
    age = now_ms - ts_ms
    return age < 0 or age > window_ms


def _parse_iso_date(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def check_data_freshness(
    clocks: Mapping[str, Any],
    *,
    now_ms: int,
    max_snapshot_age_ms: int,
) -> list[str]:
    """Fail-closed freshness gate across account snapshot and all 5-state feature clocks."""
    reasons: list[str] = []
    if max_snapshot_age_ms <= 0:
        reasons.append("max_snapshot_age_ms_invalid")
        return reasons

    snap_ms = _whole_ms(clocks.get("snapshot_observed_at_ms"))
    if _age_invalid(snap_ms, now_ms, max_snapshot_age_ms):
        reasons.append("snapshot_stale_or_absent")

    indicator_window_ms = int(binance_indicators_fresh_sec()) * 1000
    for field, code in (
        ("indicators_ts_ms", "indicators_stale_or_absent"),
        ("obi_top5_source_ts_ms", "obi_top5_source_stale_or_absent"),
        ("perp_counterparty_ts_ms", "perp_counterparty_stale_or_absent"),
        ("deribit_gamma_ts_ms", "deribit_gamma_stale_or_absent"),
    ):
        if _age_invalid(_whole_ms(clocks.get(field)), now_ms, indicator_window_ms):
            reasons.append(code)

    vol_close_ms = _whole_ms(clocks.get("volume_z_source_close_ms"))
    if vol_close_ms is None or vol_close_ms > now_ms or not source_close_is_fresh(vol_close_ms, now_ms):
        reasons.append("volume_z_close_stale_or_absent")

    news_window_ms = int(NEWS_WINDOW_MIN) * 60_000
    if _age_invalid(_whole_ms(clocks.get("news_fetched_at_ms")), now_ms, news_window_ms):
        reasons.append("news_fetched_at_stale_or_absent")

    lseg_open = clocks.get("lseg_market_open", True) is not False
    if lseg_open and _age_invalid(
        _whole_ms(clocks.get("lseg_cross_asset_ts_ms")), now_ms, int(MAX_CROSS_ASSET_AGE_MS)
    ):
        reasons.append("lseg_cross_asset_bars_stale_or_absent")

    cot_dt = _parse_iso_date(clocks.get("cot_asof_date"))
    as_of_utc = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    expected_cot = due_cftc_asof(as_of_utc)
    if cot_dt is None or cot_dt < expected_cot or cot_dt > as_of_utc.date():
        reasons.append("cot_publication_stale_or_absent")

    return reasons


def check_required_regime_inputs(row: Mapping[str, Any]) -> list[str]:
    """Refuse silent State-1 fallthrough when exogenous classifier inputs are missing."""
    missing: list[str] = []
    has_funding = (
        _has_finite(row, "funding_z")
        or _has_finite(row, "jev_funding_z")
        or _has_finite(row, "funding_rate")
        or _has_finite(row, "lastFundingRate")
    )
    if not has_funding:
        missing.append("funding_missing")
    if not _has_finite(row, "xs_breadth_1h"):
        missing.append("xs_breadth_1h_missing")
    if not (_has_finite(row, "obi_top5") or _has_finite(row, "spot_obi_top5")):
        missing.append("obi_top5_missing")
    if not (_has_finite(row, "volume_z") or _has_finite(row, "vol_z_1h")):
        missing.append("volume_z_missing")
    if not str(row.get("cot_regime") or "").strip():
        missing.append("cot_regime_missing")
    if not str(row.get("gamma_regime") or "").strip() or not _has_finite(row, "dvol_zscore"):
        missing.append("options_gamma_dvol_missing")
    # Calendar status explicitly None: fail-closed and name the gap.
    # Missing key still defaults to True (do not flip the default to False).
    if "lseg_market_open" in row and row.get("lseg_market_open") is None:
        missing.append("lseg_market_open_missing")
    elif row.get("lseg_market_open", True) is not False:
        if not (_has_finite(row, "nvda_1h_ret_pct") and _has_finite(row, "smh_1h_ret_pct")):
            missing.append("lseg_cross_asset_returns_missing")
    return missing


def _has_finite(row: Mapping[str, Any], key: str) -> bool:
    raw = row.get(key)
    if isinstance(raw, bool) or raw is None or raw == "":
        return False
    parsed, err = parse_decimal(str(raw), key)
    return err is None and parsed is not None


def _dec_or_none(row: Mapping[str, Any], key: str) -> Decimal | None:
    raw = row.get(key)
    if isinstance(raw, bool) or raw is None or raw == "":
        return None
    parsed, err = parse_decimal(str(raw), key)
    return None if err else parsed


def evaluate_approved_regime_intent(
    row: Mapping[str, Any],
    *,
    symbol: str,
    requested_action: str,
    macro_halt: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> StrategyGateDecision:
    """Evaluate 5-state market regime and return the approved cross-margin routing decision."""
    if requested_action not in {"OPEN_SHORT", "OPEN_LONG"}:
        return StrategyGateDecision(
            False, "invalid", "", requested_action, Decimal(0), Decimal(1), ("action_invalid",)
        )
    if "size_pct" in row and row.get("size_pct") not in (None, ""):
        supplied, err = parse_decimal(str(row["size_pct"]), "size_pct")
        if err or supplied is None:
            return StrategyGateDecision(
                False, "invalid", "", requested_action, Decimal(0), Decimal(1), ("size_pct_invalid",)
            )
        for kelly in UNRULED_KELLY_FRACTIONS:
            if abs(supplied - kelly) <= Decimal("0.000000001"):
                return StrategyGateDecision(
                    False,
                    "invalid",
                    "",
                    requested_action,
                    Decimal(0),
                    Decimal(1),
                    ("unruled_kelly_fraction_rejected",),
                )

    missing = check_required_regime_inputs(row)
    if missing:
        return StrategyGateDecision(
            False,
            "unconfirmed",
            "",
            requested_action,
            Decimal(0),
            Decimal(1),
            (REGIME_REQUIRED_INPUTS_REASON, *missing),
            missing_inputs=canonical_missing_inputs(missing),
        )

    plain_row = {str(k): v for k, v in row.items()}
    routed = evaluate_five_state_regime(plain_row, macro_halt=macro_halt)

    if requested_action == "OPEN_SHORT":
        return _evaluate_short_regime(plain_row, symbol=symbol, routed_regime=routed.regime, reject_reason=routed.reject_reason)
    return _evaluate_long_regime(plain_row, symbol=symbol, routed=routed, environ=environ)


def _evaluate_short_regime(
    row: dict[str, Any],
    *,
    symbol: str,
    routed_regime: str,
    reject_reason: str | None,
) -> StrategyGateDecision:
    veto, veto_reason = is_short_squeeze_veto(row)
    if veto:
        return StrategyGateDecision(
            False,
            routed_regime,
            "",
            "OPEN_SHORT",
            Decimal(0),
            Decimal(1),
            (str(veto_reason or "short_squeeze_veto"),),
        )
    if reject_reason == "regime5_cross_exchange_dislocation":
        return StrategyGateDecision(
            False,
            routed_regime,
            "",
            "OPEN_SHORT",
            Decimal(0),
            Decimal(1),
            ("regime5_cross_exchange_dislocation",),
        )
    if routed_regime in {
        REGIME_1_POSITIVE_GAMMA_PINNED,
        REGIME_2_SHORT_SQUEEZE_BREAKOUT,
        REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    }:
        return StrategyGateDecision(
            False,
            routed_regime,
            "",
            "OPEN_SHORT",
            Decimal(0),
            Decimal(1),
            ("short_forbidden_in_bull_or_neutral_regime",),
        )

    taker_buy = _dec_or_none(row, "taker_buy_ratio") or _dec_or_none(row, "spot_taker_buy_ratio")
    obi_top5 = _dec_or_none(row, "obi_top5") or _dec_or_none(row, "spot_obi_top5")
    conv_tier = get_conviction_tier(symbol)

    if routed_regime == REGIME_5_CRISIS_LIQUIDATION_HALT:
        if reject_reason == "regime5_token_unlock_blackout":
            if (
                conv_tier == "SPECULATIVE"
                and obi_top5 is not None
                and obi_top5 <= Decimal("-0.10")
                and (taker_buy is None or taker_buy <= Decimal("0.46"))
            ):
                return StrategyGateDecision(
                    True,
                    routed_regime,
                    "regime5_unlock_cliff_short",
                    "OPEN_SHORT",
                    Decimal("0.35"),
                    Decimal("1.00"),
                    (),
                )
            return StrategyGateDecision(
                False,
                routed_regime,
                "",
                "OPEN_SHORT",
                Decimal(0),
                Decimal(1),
                ("unlock_short_microstructure_unconfirmed",),
            )
        if taker_buy is not None and taker_buy > Decimal("0.45"):
            return StrategyGateDecision(
                False,
                routed_regime,
                "",
                "OPEN_SHORT",
                Decimal(0),
                Decimal(1),
                ("crisis_short_taker_buy_too_strong",),
            )
        return StrategyGateDecision(
            True,
            routed_regime,
            "regime5_crisis_contagion_short",
            "OPEN_SHORT",
            Decimal("0.50"),
            Decimal("1.00"),
            (),
        )

    if routed_regime == REGIME_4_CROWDED_LONG_DEFENSIVE:
        if taker_buy is not None and taker_buy > Decimal("0.48"):
            return StrategyGateDecision(
                False,
                routed_regime,
                "",
                "OPEN_SHORT",
                Decimal(0),
                Decimal(1),
                ("crowded_long_short_taker_buy_too_strong",),
            )
        return StrategyGateDecision(
            True,
            routed_regime,
            "regime4_crowded_long_carry_short",
            "OPEN_SHORT",
            Decimal("0.55"),
            Decimal("0.85"),
            (),
        )

    return StrategyGateDecision(
        False,
        routed_regime,
        "",
        "OPEN_SHORT",
        Decimal(0),
        Decimal(1),
        ("short_regime_unmatched",),
    )


def _evaluate_long_regime(
    row: dict[str, Any],
    *,
    symbol: str = "BTCUSDT",
    routed: Any,
    environ: Mapping[str, str] | None = None,
) -> StrategyGateDecision:
    if not routed.allow_buy:
        return StrategyGateDecision(
            False,
            routed.regime,
            "",
            "OPEN_LONG",
            Decimal(0),
            Decimal(1),
            (str(routed.reject_reason or "regime_blocks_long"),),
        )
    if routed.regime == REGIME_1_POSITIVE_GAMMA_PINNED:
        return StrategyGateDecision(
            True,
            routed.regime,
            "regime1_positive_gamma_fade",
            "OPEN_LONG",
            Decimal("1.00"),
            Decimal("1.00"),
            (),
        )
    if routed.regime == REGIME_3_CROSS_ASSET_BULL_PULLBACK:
        return StrategyGateDecision(
            True,
            routed.regime,
            "regime3_xs_leader_pullback",
            "OPEN_LONG",
            Decimal("1.15"),
            Decimal("1.25"),
            (),
        )
    if routed.regime == REGIME_2_SHORT_SQUEEZE_BREAKOUT:
        if not state2_long_enabled(environ):
            return StrategyGateDecision(
                False,
                routed.regime,
                "",
                "OPEN_LONG",
                Decimal(0),
                Decimal(1),
                ("state2_entry_module_not_live",),
            )
        tbr_s2 = _dec_or_none(row, "taker_buy_ratio") or _dec_or_none(row, "spot_taker_buy_ratio")
        obi_s2 = _dec_or_none(row, "obi_top5") or _dec_or_none(row, "spot_obi_top5")
        if tbr_s2 is None or tbr_s2 < Decimal("0.48"):
            return StrategyGateDecision(
                False, routed.regime, "", "OPEN_LONG", Decimal(0), Decimal(1), ("state2_taker_buy_below_min",),
            )
        if obi_s2 is None or obi_s2 < Decimal("-0.05"):
            return StrategyGateDecision(
                False, routed.regime, "", "OPEN_LONG", Decimal(0), Decimal(1), ("state2_obi_top5_below_min",),
            )
        return StrategyGateDecision(
            True,
            routed.regime,
            "regime2_short_squeeze_breakout",
            "OPEN_LONG",
            Decimal("1.20"),
            Decimal("1.35"),
            (),
        )
    if routed.regime == REGIME_4_CROWDED_LONG_DEFENSIVE:
        if not state4_long_enabled(environ):
            return StrategyGateDecision(
                False,
                routed.regime,
                "",
                "OPEN_LONG",
                Decimal(0),
                Decimal(1),
                ("state4_long_closed_on_margin",),
            )
        if get_conviction_tier(symbol) == "SPECULATIVE":
            return StrategyGateDecision(
                False, routed.regime, "", "OPEN_LONG", Decimal(0), Decimal(1), ("state4_speculative_symbol_rejected",),
            )
        bz_s4 = _dec_or_none(row, "boll_z") or _dec_or_none(row, "boll_z_1h")
        if bz_s4 is None or bz_s4 > Decimal("-2.20"):
            return StrategyGateDecision(
                False, routed.regime, "", "OPEN_LONG", Decimal(0), Decimal(1), ("state4_boll_z_not_deep_enough",),
            )
        alpha_s4 = _dec_or_none(row, "alpha")
        if alpha_s4 is not None and alpha_s4 > Decimal("0.15"):
            return StrategyGateDecision(
                False, routed.regime, "", "OPEN_LONG", Decimal(0), Decimal(1), ("state4_dip_not_deep_enough",),
            )
        tbr_s4 = _dec_or_none(row, "taker_buy_ratio") or _dec_or_none(row, "spot_taker_buy_ratio")
        obi_s4 = _dec_or_none(row, "obi_top5") or _dec_or_none(row, "spot_obi_top5")
        if tbr_s4 is None or tbr_s4 < Decimal("0.38"):
            return StrategyGateDecision(
                False, routed.regime, "", "OPEN_LONG", Decimal(0), Decimal(1), ("state4_taker_buy_below_min",),
            )
        if obi_s4 is None or obi_s4 < Decimal("-0.15"):
            return StrategyGateDecision(
                False, routed.regime, "", "OPEN_LONG", Decimal(0), Decimal(1), ("state4_obi_top5_below_min",),
            )
        return StrategyGateDecision(
            True,
            routed.regime,
            "regime4_defensive_deep_fade",
            "OPEN_LONG",
            Decimal("0.30"),
            Decimal("0.85"),
            (),
        )
    return StrategyGateDecision(
        False,
        routed.regime,
        "",
        "OPEN_LONG",
        Decimal(0),
        Decimal(1),
        ("regime5_long_halted",),
    )


def account_quote_valuation(
    snapshot: Snapshot,
    *,
    base_asset: str,
    quote_asset: str,
) -> tuple[Decimal, Decimal, Decimal]:
    """Compute ``(total_asset_quote, total_liability_quote, net_asset_quote)`` from live rows."""
    quote_row = snapshot.assets.get(quote_asset)
    base_row = snapshot.assets.get(base_asset)
    quote_gross = (quote_row.free + quote_row.locked) if quote_row else Decimal(0)
    quote_liab = (quote_row.borrowed + quote_row.interest) if quote_row else Decimal(0)
    base_gross = (
        (base_row.free + base_row.locked) * snapshot.reference_price
        if base_row
        else Decimal(0)
    )
    base_liab = (
        (base_row.borrowed + base_row.interest) * snapshot.reference_price
        if base_row
        else Decimal(0)
    )
    total_asset = quote_gross + base_gross
    total_liab = quote_liab + base_liab
    net_asset = total_asset - total_liab
    return total_asset, total_liab, net_asset


def apply_formula_b_take_profit(
    *,
    action: str,
    planned_entry: Decimal,
    planned_tp: Decimal,
    actual_fill: Decimal,
    tick_size: Decimal,
    tp_multiplier: Decimal = Decimal("1"),
) -> Decimal | None:
    """First-hang fill-anchored take-profit Formula B. Stop-loss is never modified."""
    if min(planned_entry, planned_tp, actual_fill, tick_size, tp_multiplier) <= 0:
        return None
    if action == "OPEN_SHORT":
        if planned_tp >= planned_entry:
            return None
        raw = actual_fill * (Decimal(1) - (Decimal(1) - planned_tp / planned_entry) * tp_multiplier)
        if raw <= 0:
            return None
        units = (raw / tick_size).to_integral_value(rounding=ROUND_DOWN)
        snapped = units * tick_size
        return snapped if snapped > 0 else None
    if planned_tp <= planned_entry:
        return None
    raw = actual_fill * (Decimal(1) + (planned_tp / planned_entry - Decimal(1)) * tp_multiplier)
    units = (raw / tick_size).to_integral_value(rounding=ROUND_CEILING)
    snapped = units * tick_size
    return snapped if snapped > 0 else None


def _floor_to_grid(value: Decimal, step: Decimal) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _snap_price_down(value: Decimal, tick: Decimal) -> Decimal:
    units = (value / tick).to_integral_value(rounding=ROUND_DOWN)
    return units * tick


def _snap_price_up(value: Decimal, tick: Decimal) -> Decimal:
    units = (value / tick).to_integral_value(rounding=ROUND_CEILING)
    return units * tick


def _blocked_plan(reason: str) -> ApprovedPositionPlan:
    return ApprovedPositionPlan(
        allowed=False,
        reasons=(reason,),
        regime="blocked",
        strategy_id="",
        net_asset_quote=Decimal(0),
        effective_leverage_after=Decimal(0),
        target_notional_quote=Decimal(0),
        quantity=Decimal(0),
        intent=None,
        contract=None,
    )


def _ruled_stop_limit(action: str, snapped_stop: Decimal, tick: Decimal) -> Decimal | None:
    """20 bps and at least two ticks beyond the snapped stop. Not a fill promise."""
    if tick <= 0 or snapped_stop <= 0:
        return None
    if action == "OPEN_LONG":
        buffered = min(snapped_stop - (Decimal(2) * tick), _snap_price_down(snapped_stop * Decimal("0.9980"), tick))
        if buffered <= 0 or buffered > snapped_stop:
            return None
    else:
        buffered = max(snapped_stop + (Decimal(2) * tick), _snap_price_up(snapped_stop * Decimal("1.0020"), tick))
        if buffered < snapped_stop:
            return None
    if buffered <= 0:
        return None
    return buffered


CASH_BASE_POSITION_PCT = Decimal(str(DEFAULT_BASE_POSITION_PCT))
SINGLE_NAME_EXPOSURE_CAP = Decimal(str(DEFAULT_MAX_SINGLE_EXPOSURE_PCT))
TOTAL_GROSS_EXPOSURE_CAP = Decimal(str(DEFAULT_MAX_TOTAL_EXPOSURE_PCT))
STATE4_TOTAL_GROSS_EXPOSURE_CAP = Decimal(str(DEFAULT_STATE4_MAX_TOTAL_EXPOSURE_PCT))
_STATE4_STRATEGY_PREFIX = "regime4_"
_EXPOSURE_TERMINAL_STATES = frozenset(
    {
        "clean_flat",
        "protection_failed_flattened",
        "entry_rejected_unwound",
        "risk_dropped_unwound",
        "emergency_unwound",
        "locked",
        "borrow_rejected",
        "entry_rejected",
        "otoco_not_submitted",
        "live_network_disabled",
        "emergency_stop_blocks_new_risk",
        "intent_conflict",
    }
)


def verified_empty_exposure() -> dict[str, Any]:
    """A caller-verified book with no open or resting exposure. Not a default."""
    return {
        "verified": True,
        "gross_notional_quote": "0",
        "symbol_notional_quote": "0",
        "open_count": 0,
        "state4_open_count": 0,
        "cluster_open_count": 0,
    }


def read_cross_margin_exposure_book(
    ledger_path: str | None,
    *,
    symbol: str,
    intent_id: str,
) -> dict[str, Any]:
    """Sum other working intents. Unreadable or unpriced rows are unverified, not zero."""
    if not ledger_path:
        return {"verified": False, "reason": "exposure_ledger_absent"}
    path = Path(ledger_path)
    if not path.is_file():
        return {"verified": False, "reason": "exposure_ledger_absent"}
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT intent_id, state, payload_json FROM intents").fetchall()
        conn.close()
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return {"verified": False, "reason": "exposure_ledger_unreadable"}
    gross = Decimal(0)
    symbol_notional = Decimal(0)
    open_count = 0
    state4_open = 0
    cluster_open = 0
    this_cluster = cluster_exposure_key(symbol) or ""
    seen: set[str] = set()
    target = str(symbol or "").strip().upper()
    for item in rows:
        other_id = str(item["intent_id"])
        if other_id == intent_id or other_id in seen:
            continue
        seen.add(other_id)
        if str(item["state"]) in _EXPOSURE_TERMINAL_STATES:
            continue
        try:
            stored = json.loads(str(item["payload_json"]))
            intent = stored["intent"]
            qty, qty_err = parse_decimal(str(intent.get("quantity")), "quantity")
            px, px_err = parse_decimal(str(intent.get("limit_price") or ""), "limit_price")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {"verified": False, "reason": "exposure_intent_unreadable"}
        if qty_err or px_err or qty is None or px is None or qty < 0 or px <= 0:
            return {"verified": False, "reason": "exposure_intent_unpriced"}
        notional = qty * px
        other_symbol = str(intent.get("symbol") or "").strip().upper()
        strategy_id = str(intent.get("strategy_id") or "")
        gross += notional
        open_count += 1
        if other_symbol == target:
            symbol_notional += notional
        if strategy_id.startswith(_STATE4_STRATEGY_PREFIX):
            state4_open += 1
        other_cluster = cluster_exposure_key(other_symbol) or ""
        if this_cluster and other_cluster == this_cluster:
            cluster_open += 1
    return {
        "verified": True,
        "gross_notional_quote": api_amount(gross),
        "symbol_notional_quote": api_amount(symbol_notional),
        "open_count": open_count,
        "state4_open_count": state4_open,
        "cluster_open_count": cluster_open,
    }


def _exposure_rooms(
    book: Mapping[str, Any] | None,
    *,
    net_asset_q: Decimal,
    is_state4: bool,
) -> tuple[Decimal, Decimal] | str:
    if not isinstance(book, Mapping) or book.get("verified") is not True:
        return "exposure_unverified"
    needed = (
        "gross_notional_quote",
        "symbol_notional_quote",
        "open_count",
        "state4_open_count",
        "cluster_open_count",
    )
    if any(key not in book for key in needed):
        return "exposure_unverified"
    gross, gross_err = parse_decimal(str(book.get("gross_notional_quote")), "gross_notional_quote")
    named, named_err = parse_decimal(str(book.get("symbol_notional_quote")), "symbol_notional_quote")
    try:
        open_count = int(book.get("open_count"))
        state4_count = int(book.get("state4_open_count"))
        cluster_count = int(book.get("cluster_open_count"))
    except (TypeError, ValueError):
        return "exposure_unverified"
    if (
        gross_err
        or named_err
        or gross is None
        or named is None
        or gross < 0
        or named < 0
        or open_count < 0
        or state4_count < 0
        or cluster_count < 0
    ):
        return "exposure_unverified"
    if is_state4 and (state4_count >= 1 or cluster_count >= 1):
        return "state4_exposure_cap"
    single_room = net_asset_q * SINGLE_NAME_EXPOSURE_CAP - named
    total_cap = STATE4_TOTAL_GROSS_EXPOSURE_CAP if is_state4 else TOTAL_GROSS_EXPOSURE_CAP
    total_room = net_asset_q * total_cap - gross
    return max(Decimal(0), single_room), max(Decimal(0), total_room)


def build_approved_position_plan(
    *,
    intent_id: str,
    symbol: str,
    base_asset: str,
    quote_asset: str,
    requested_action: str,
    snapshot_raw: Mapping[str, Any],
    market_row: Mapping[str, Any],
    clocks: Mapping[str, Any],
    now_ms: int,
    entry_tier: Decimal = Decimal("1.00"),
    limit_price: Decimal | None = None,
    planned_take_profit: Decimal,
    stop_price: Decimal,
    stop_limit_price: Decimal | None = None,
    derive_ruled_stop_limit: bool = False,
    borrow_step: Decimal | None = None,
    max_borrow_quantity_cap: Decimal | None = None,
    risk_config: ApprovedRiskConfig | Mapping[str, Any] | None = None,
    macro_halt: str | None = None,
    environ: Mapping[str, str] | None = None,
    exposure_book: Mapping[str, Any] | None = None,
) -> ApprovedPositionPlan:
    """Validate operator approval, zero-default risk config, freshness, and 5-state sizing."""
    if not parameters_approved_by_env(environ):
        return ApprovedPositionPlan(
            allowed=False,
            reasons=("parameters_not_approved",),
            regime="blocked",
            strategy_id="",
            net_asset_quote=Decimal(0),
            effective_leverage_after=Decimal(0),
            target_notional_quote=Decimal(0),
            quantity=Decimal(0),
            intent=None,
            contract=None,
        )
    raw_snap_uid = (
        str(snapshot_raw.get("uid") or "").strip()
        if isinstance(snapshot_raw, Mapping)
        else ""
    )
    if isinstance(risk_config, ApprovedRiskConfig):
        cfg = risk_config
        cfg_reasons = validate_approved_config_window(
            cfg,
            now_ms=now_ms,
            expected_account_uid=raw_snap_uid or None,
        )
    elif isinstance(risk_config, Mapping):
        cfg, cfg_reasons = parse_approved_risk_config(risk_config)
        if cfg is not None and not cfg_reasons:
            cfg_reasons = validate_approved_config_window(
                cfg,
                now_ms=now_ms,
                expected_account_uid=raw_snap_uid or None,
            )
    else:
        cfg, cfg_reasons = load_approved_risk_config(
            environ,
            now_ms=now_ms,
            expected_account_uid=raw_snap_uid or None,
        )
    if cfg is None or cfg_reasons:
        return ApprovedPositionPlan(
            allowed=False,
            reasons=tuple(cfg_reasons or ["approved_config_missing"]),
            regime="blocked",
            strategy_id="",
            net_asset_quote=Decimal(0),
            effective_leverage_after=Decimal(0),
            target_notional_quote=Decimal(0),
            quantity=Decimal(0),
            intent=None,
            contract=None,
        )

    snapshot, snap_reasons = parse_snapshot(snapshot_raw)
    freshness_reasons = check_data_freshness(
        clocks, now_ms=now_ms, max_snapshot_age_ms=cfg.max_snapshot_age_ms
    )
    if snap_reasons or snapshot is None or freshness_reasons:
        return ApprovedPositionPlan(
            allowed=False,
            reasons=tuple(snap_reasons + freshness_reasons),
            regime="blocked",
            strategy_id="",
            net_asset_quote=Decimal(0),
            effective_leverage_after=Decimal(0),
            target_notional_quote=Decimal(0),
            quantity=Decimal(0),
            intent=None,
            contract=None,
        )

    if entry_tier <= 0 or entry_tier > Decimal(1):
        return ApprovedPositionPlan(
            allowed=False,
            reasons=("entry_tier_out_of_range",),
            regime="blocked",
            strategy_id="",
            net_asset_quote=Decimal(0),
            effective_leverage_after=Decimal(0),
            target_notional_quote=Decimal(0),
            quantity=Decimal(0),
            intent=None,
            contract=None,
        )

    strat = evaluate_approved_regime_intent(
        market_row,
        symbol=symbol,
        requested_action=requested_action,
        macro_halt=macro_halt,
        environ=environ,
    )
    if not strat.allowed:
        return ApprovedPositionPlan(
            allowed=False,
            reasons=strat.reasons,
            regime=strat.regime,
            strategy_id=strat.strategy_id,
            net_asset_quote=Decimal(0),
            effective_leverage_after=Decimal(0),
            target_notional_quote=Decimal(0),
            quantity=Decimal(0),
            intent=None,
            contract=None,
        )
    if strat.strategy_id not in cfg.approved_strategies:
        return ApprovedPositionPlan(
            allowed=False,
            reasons=("strategy_not_in_approved_config",),
            regime=strat.regime,
            strategy_id=strat.strategy_id,
            net_asset_quote=Decimal(0),
            effective_leverage_after=Decimal(0),
            target_notional_quote=Decimal(0),
            quantity=Decimal(0),
            intent=None,
            contract=None,
        )

    total_asset_q, total_liab_q, net_asset_q = account_quote_valuation(
        snapshot, base_asset=base_asset, quote_asset=quote_asset
    )
    if net_asset_q <= 0:
        return ApprovedPositionPlan(
            allowed=False,
            reasons=("net_asset_not_positive",),
            regime=strat.regime,
            strategy_id=strat.strategy_id,
            net_asset_quote=net_asset_q,
            effective_leverage_after=Decimal(0),
            target_notional_quote=Decimal(0),
            quantity=Decimal(0),
            intent=None,
            contract=None,
        )

    # Cash base is the ruled equity fraction, not the approved file's full-cash
    # base and not size_pct. The name cap and the gross long+short cap are
    # ceilings. State 4 keeps its tighter total cap. Neither cap is a target.
    is_state4 = strat.regime == REGIME_4_CROWDED_LONG_DEFENSIVE or strat.strategy_id.startswith(
        _STATE4_STRATEGY_PREFIX
    )
    rooms = _exposure_rooms(exposure_book, net_asset_q=net_asset_q, is_state4=is_state4)
    if isinstance(rooms, str):
        return ApprovedPositionPlan(
            allowed=False,
            reasons=(rooms,),
            regime=strat.regime,
            strategy_id=strat.strategy_id,
            net_asset_quote=net_asset_q,
            effective_leverage_after=Decimal(0),
            target_notional_quote=Decimal(0),
            quantity=Decimal(0),
            intent=None,
            contract=None,
        )
    single_room, total_room = rooms
    if requested_action == "OPEN_SHORT":
        raw_notional = net_asset_q * cfg.base_position_pct * entry_tier * strat.size_multiplier
    else:
        raw_notional = net_asset_q * CASH_BASE_POSITION_PCT * entry_tier * strat.size_multiplier
    max_single_borrow_notional = net_asset_q * cfg.max_single_borrow_net_ratio
    max_additional_liab = max(
        Decimal(0), net_asset_q * cfg.max_total_liability_net_ratio - total_liab_q
    )
    max_lev_headroom = max(
        Decimal(0), net_asset_q * cfg.max_effective_leverage - total_asset_q
    )

    chosen_entry = snapshot.reference_price if limit_price is None else limit_price
    tick = snapshot.filters.tick_size
    step = snapshot.filters.step_size
    eff_borrow_step = step if borrow_step is None else max(step, borrow_step)

    if requested_action == "OPEN_SHORT":
        capped_notional = min(
            raw_notional,
            max_single_borrow_notional,
            max_additional_liab,
            max_lev_headroom,
            single_room,
            total_room,
        )
        borrowable = snapshot.max_borrowable.get(base_asset, Decimal(0))
        pool_cap_qty = _floor_to_grid(borrowable * cfg.max_borrowable_pool_ratio, eff_borrow_step)
        contract_cap_qty = (
            pool_cap_qty
            if max_borrow_quantity_cap is None
            else min(pool_cap_qty, _floor_to_grid(max_borrow_quantity_cap, eff_borrow_step))
        )
        qty_from_notional = _floor_to_grid(capped_notional / chosen_entry, eff_borrow_step)
        quantity = min(qty_from_notional, contract_cap_qty)
        snapped_entry = _snap_price_up(chosen_entry, tick)
        scaled_tp_raw = snapped_entry * (
            Decimal(1) - (Decimal(1) - planned_take_profit / snapped_entry) * strat.tp_multiplier
        )
        snapped_tp = _snap_price_down(scaled_tp_raw, tick)
        snapped_stop = _snap_price_up(stop_price, tick)
        if derive_ruled_stop_limit:
            snapped_stop_limit = _ruled_stop_limit("OPEN_SHORT", snapped_stop, tick)
            if snapped_stop_limit is None:
                return _blocked_plan("stop_limit_invalid")
        else:
            snapped_stop_limit = _snap_price_up(stop_limit_price or Decimal(0), tick)
        borrow_asset = base_asset
        new_borrow_quote = quantity * snapped_entry
    else:
        quote_free = snapshot.assets.get(quote_asset).free if quote_asset in snapshot.assets else Decimal(0)
        capped_notional = min(
            raw_notional,
            quote_free / (Decimal(1) + snapshot.fee_rate),
            single_room,
            total_room,
        )
        quantity = _floor_to_grid(capped_notional / chosen_entry, step)
        contract_cap_qty = max(quantity, step)
        snapped_entry = _snap_price_down(chosen_entry, tick)
        scaled_tp_raw = snapped_entry * (
            Decimal(1) + (planned_take_profit / snapped_entry - Decimal(1)) * strat.tp_multiplier
        )
        snapped_tp = _snap_price_up(scaled_tp_raw, tick)
        snapped_stop = _snap_price_down(stop_price, tick)
        if derive_ruled_stop_limit:
            snapped_stop_limit = _ruled_stop_limit("OPEN_LONG", snapped_stop, tick)
            if snapped_stop_limit is None:
                return _blocked_plan("stop_limit_invalid")
        else:
            snapped_stop_limit = _snap_price_down(stop_limit_price or Decimal(0), tick)
        borrow_asset = base_asset
        new_borrow_quote = Decimal(0)

    eff_lev_after = (total_asset_q + new_borrow_quote) / net_asset_q
    if eff_lev_after > cfg.max_effective_leverage:
        return ApprovedPositionPlan(
            allowed=False,
            reasons=("effective_leverage_exceeded",),
            regime=strat.regime,
            strategy_id=strat.strategy_id,
            net_asset_quote=net_asset_q,
            effective_leverage_after=eff_lev_after,
            target_notional_quote=capped_notional,
            quantity=quantity,
            intent=None,
            contract=None,
        )

    order_notional = quantity * snapped_entry
    min_floor = max(cfg.min_order_notional_usdt, snapshot.filters.min_notional)
    if quantity <= 0 or quantity < snapshot.filters.min_qty or not on_grid(quantity, step):
        return ApprovedPositionPlan(
            allowed=False,
            reasons=("quantity_below_min",),
            regime=strat.regime,
            strategy_id=strat.strategy_id,
            net_asset_quote=net_asset_q,
            effective_leverage_after=eff_lev_after,
            target_notional_quote=order_notional,
            quantity=quantity,
            intent=None,
            contract=None,
        )
    if order_notional < min_floor:
        return ApprovedPositionPlan(
            allowed=False,
            reasons=("notional_below_approved_floor",),
            regime=strat.regime,
            strategy_id=strat.strategy_id,
            net_asset_quote=net_asset_q,
            effective_leverage_after=eff_lev_after,
            target_notional_quote=order_notional,
            quantity=quantity,
            intent=None,
            contract=None,
        )

    intent_dict: dict[str, Any] = {
        "intent_id": intent_id,
        "purpose": PURPOSE_CROSS_MARGIN_TRACK,
        "strategy_id": strat.strategy_id,
        "account_uid": snapshot.uid,
        "mode": "CROSS",
        "action": requested_action,
        "symbol": symbol,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "quantity": api_amount(quantity),
        "limit_price": api_amount(snapped_entry),
        "take_profit_price": api_amount(snapped_tp),
        "stop_price": api_amount(snapped_stop),
        "stop_limit_price": api_amount(snapped_stop_limit),
    }
    contract_dict: dict[str, Any] = {
        "account_uid": snapshot.uid,
        "mode": "CROSS",
        "symbol": symbol,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "borrow_asset": borrow_asset,
        "parameters_approved": True,
        "unapproved_label": APPROVED_CONTRACT_LABEL,
        "max_leverage": api_amount(cfg.max_leverage),
        "max_effective_leverage": api_amount(cfg.max_effective_leverage),
        "min_margin_level": api_amount(cfg.min_entry_margin_level),
        "warn_margin_level": api_amount(cfg.warn_margin_level),
        "emergency_margin_level": api_amount(cfg.emergency_margin_level),
        "max_total_liability_net_ratio": api_amount(cfg.max_total_liability_net_ratio),
        "max_borrowable_pool_ratio": api_amount(cfg.max_borrowable_pool_ratio),
        "min_order_notional_usdt": api_amount(cfg.min_order_notional_usdt),
        "max_borrow_quantity": api_amount(max(contract_cap_qty, eff_borrow_step)),
        "max_snapshot_age_ms": cfg.max_snapshot_age_ms,
        "interest_horizon_hours": api_amount(cfg.interest_horizon_hours),
        "max_base_over_liability": api_amount(step),
        "strategy_id": strat.strategy_id,
        "borrow_step": api_amount(eff_borrow_step),
        "formula_b_enabled": fill_tp_formula_b_enabled(environ),
    }
    return ApprovedPositionPlan(
        allowed=True,
        reasons=(),
        regime=strat.regime,
        strategy_id=strat.strategy_id,
        net_asset_quote=net_asset_q,
        effective_leverage_after=eff_lev_after,
        target_notional_quote=order_notional,
        quantity=quantity,
        intent=intent_dict,
        contract=contract_dict,
    )
