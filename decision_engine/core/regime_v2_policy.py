"""Ruled Regime v2 Phase-2 strategy policy: S2 squeeze entry, S4 deep-dip entry,
real-time equity & concurrency/exposure caps, and 9-datasource fail-closed clocks.

Source defaults stay off: nothing in this module alters legacy behavior unless
``REGIME_V2_CONTRACT=1`` is active.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from decision_engine.core.binance_indicators import binance_indicators_fresh_sec
from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from decision_engine.core.conviction_whitelist import get_conviction_tier
from decision_engine.core.macro_circuit_breaker import MAX_CROSS_ASSET_AGE_MS
from decision_engine.core.news_shock_gate import NEWS_WINDOW_MIN
from decision_engine.core.perp_microstructure_and_beta_hedge import symbol_beta_vs_btc
from decision_engine.core.portfolio_clusters import (
    cluster_exposure_key,
    symbol_sector_cluster,
)
from decision_engine.core.protection_halt import read_protection_book
from decision_engine.core.stop_gap import stop_distance_reason
from decision_engine.core.regime_v2_reservation import (
    RELEASE_INTERFACE_BLOCK,
    active_reservations,
    reservation_db_path,
    signal_reservation_key,
    try_reserve,
)
from decision_engine.core.volume_z_1h import source_close_is_fresh

CONTRACT_ENV = "REGIME_V2_CONTRACT"
STALE_DATA_POLICY_ENV = "REGIME_V2_STALE_DATA_POLICY"
EQUITY_SOURCE_ENV = "REGIME_V2_EQUITY_SOURCE"
ENFORCE_LIVE_EQUITY_ENV = "REGIME_V2_ENFORCE_LIVE_EQUITY"
DRY_RUN_EQUITY_ENV = "REGIME_V2_DRY_RUN_EQUITY_USDT"
DRY_RUN_FREE_USDT_ENV = "REGIME_V2_DRY_RUN_FREE_USDT"
SNAPSHOT_MAX_AGE_ENV = "REGIME_V2_ACCOUNT_SNAPSHOT_MAX_AGE_SEC"
ENTRY_KLINE_5M_MAX_AGE_ENV = "REGIME_V2_ENTRY_KLINE_5M_MAX_AGE_SEC"

STATE2_ENTRY_STATUS_ENV = "REGIME_V2_STATE2_ENTRY_STATUS"
STATE4_ENTRY_STATUS_ENV = "REGIME_V2_STATE4_ENTRY_STATUS"
RULED_STATE2_ENTRY_STATUS = "live_squeeze_limit_maker_gated"
RULED_STATE4_ENTRY_STATUS = "live_deep_dip_limit_maker_gated"
RULED_STALE_DATA_POLICY = "fail_closed"
RULED_EQUITY_SOURCE = "signed_account_snapshot"

# S2 Short-Squeeze Real Entry Rulings
S2_FUNDING_Z_MAX = -1.80
S2_FUNDING_RATE_MAX = -0.0003
S2_WHALE_DIV_MIN = 1.35
S2_MIN_TAKER_BUY_RATIO = 0.48
S2_LARGE_TAKER_BUY_RATIO = 0.52
S2_MIN_OBI_TOP5 = -0.05
S2_MIN_TRADE_SIZE_RATIO = 0.85
S2_LARGE_TRADE_SIZE_RATIO = 1.30
S2_ALPHA_FLOOR = 0.08
S2_ALPHA_CAP = 0.55
S2_LIMIT_ALPHA_CAP = 0.30
S2_STOP_SPAN_FRAC = 0.10
S2_MIN_ODDS = 1.50
S2_MAX_OI_DROP_1H_PCT = 5.0
S2_SQUEEZE_ENTRY_REASONS = frozenset(
    {"state2_squeeze_limit_maker", "state2_squeeze_large_limit_maker"}
)

# S4 Crowded-Long Defensive Deep-Dip Rulings
S4_BOLL_Z_MAX = -2.20
S4_ALPHA_CAP = 0.15
S4_ALPHA_MIN_WICK = -0.20
S4_MIN_TAKER_BUY_RATIO = 0.38
S4_LARGE_TAKER_BUY_RATIO = 0.45
S4_MIN_OBI_TOP5 = -0.15
S4_MIN_TRADE_SIZE_RATIO = 0.85
S4_LARGE_TRADE_SIZE_RATIO = 1.30
S4_MIN_ODDS = 3.00
S4_DEEP_ENTRY_REASONS = frozenset({"state4_deep_limit_maker"})

# Real-time equity and cap numbers from commit f06cfb1c, approved by t307
# (2026-11-19 17:31 HKT). Concurrency 3 (state 4 = 1), total exposure 0.72
# (state 4 = 0.30), one name per cluster, and 0.30 per name replace the
# earlier 2 / 0.70 proposals. Beta 0.85 / state 4 0.35 stays as submitted.
DEFAULT_BASE_POSITION_PCT = 0.24
DEFAULT_ACCOUNT_SNAPSHOT_MAX_AGE_SEC = 300
DEFAULT_ENTRY_KLINE_5M_MAX_AGE_SEC = 900
DEFAULT_MAX_CONCURRENT_POSITIONS = 3
DEFAULT_STATE4_MAX_CONCURRENT_POSITIONS = 1
DEFAULT_MAX_PER_CLUSTER = 1
DEFAULT_MAX_SINGLE_EXPOSURE_PCT = 0.30
DEFAULT_MAX_TOTAL_EXPOSURE_PCT = 0.72
DEFAULT_STATE4_MAX_TOTAL_EXPOSURE_PCT = 0.30
DEFAULT_MAX_PORTFOLIO_BETA = 0.85
DEFAULT_STATE4_MAX_PORTFOLIO_BETA = 0.35
FREE_USDT_BUFFER_RATIO = 0.98
MIN_EXCHANGE_ORDER_USDT = 20.0
MCP_FORBIDDEN_UID = "1289917075"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})
_CMP_EPS = 1e-9


def _finite(val: object) -> float | None:
    if isinstance(val, bool) or val is None or val == "":
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _as_int(val: object) -> int | None:
    if isinstance(val, bool) or val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _env_float(name: str, default: float, environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    parsed = _finite(env.get(name))
    return float(parsed) if parsed is not None else default


def _env_int(name: str, default: int, environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    parsed = _as_int(env.get(name))
    return int(parsed) if parsed is not None and parsed > 0 else default


def v2_contract_active(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(CONTRACT_ENV, "")).strip().lower() in _TRUTHY


def stale_data_fail_closed_active(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if not v2_contract_active(env):
        return False
    policy = str(env.get(STALE_DATA_POLICY_ENV, "")).strip().lower()
    return policy == RULED_STALE_DATA_POLICY


def s2_entry_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if not v2_contract_active(env):
        return False
    status = str(env.get(STATE2_ENTRY_STATUS_ENV, RULED_STATE2_ENTRY_STATUS)).strip()
    if status in {"0", "off", "false", "module_not_called"}:
        return False
    return status == RULED_STATE2_ENTRY_STATUS


def s4_entry_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if not v2_contract_active(env):
        return False
    status = str(env.get(STATE4_ENTRY_STATUS_ENV, RULED_STATE4_ENTRY_STATUS)).strip()
    if status in {"0", "off", "false", "deep_tier_undefined"}:
        return False
    return status == RULED_STATE4_ENTRY_STATUS


def resolve_boll_z(row: Mapping[str, Any]) -> float | None:
    """Resolve explicit ``boll_z`` / ``boll_z_1h`` or derive from Bollinger bands when valid."""
    for key in ("boll_z", "boll_z_1h"):
        val = _finite(row.get(key))
        if val is not None:
            return val
    price = _finite(row.get("price_usd") or row.get("close") or row.get("price"))
    mid = _finite(row.get("boll_mid"))
    upper = _finite(row.get("boll_upper"))
    lower = _finite(row.get("boll_lower"))
    if price is None or mid is None:
        return None
    half_width: float | None = None
    if upper is not None and upper > mid:
        half_width = upper - mid
    elif lower is not None and mid > lower:
        half_width = mid - lower
    if half_width is None or half_width <= _CMP_EPS:
        return None
    sigma = half_width / 2.0
    return (price - mid) / sigma if sigma > _CMP_EPS else None


def is_state5_crisis_row(row: Mapping[str, Any], *, macro_halt: str | None = None) -> bool:
    if macro_halt:
        return True
    raw_unlock = row.get("in_unlock_blackout")
    if isinstance(raw_unlock, (int, float, bool)) and bool(int(raw_unlock)):
        return True
    disloc = _finite(row.get("max_venue_dislocation_bps"))
    if disloc is not None and disloc >= 35.0:
        return True
    if bool(row.get("xs_systemic_crash")):
        return True
    liq = str(row.get("liq_cascade_regime") or "").strip().lower()
    if liq == "long_liquidation_waterfall":
        return True
    gamma = str(row.get("gamma_regime") or "").strip().lower()
    dvol_z = _finite(row.get("dvol_zscore"))
    if gamma == "negative_gamma_slippery" and dvol_z is not None and dvol_z >= 2.0:
        return True
    nvda = _finite(row.get("nvda_1h_ret_pct"))
    smh = _finite(row.get("smh_1h_ret_pct"))
    if (nvda is not None and nvda <= -2.0) or (smh is not None and smh <= -2.0):
        return True
    return False


def is_s2_squeeze_candidate_row(
    row: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True when ``row`` carries State-2 short-squeeze fuel and is not in State 5."""
    if is_state5_crisis_row(row):
        return False
    fz_max = _env_float("REGIME_V2_S2_FUNDING_Z_MAX", S2_FUNDING_Z_MAX, environ)
    fr_max = _env_float("REGIME_V2_S2_FUNDING_RATE_MAX", S2_FUNDING_RATE_MAX, environ)
    funding_z = _finite(row.get("funding_z", row.get("jev_funding_z")))
    funding_rate = _finite(row.get("funding_rate", row.get("lastFundingRate")))
    liq = str(row.get("liq_cascade_regime") or "").strip().lower()
    cot = str(row.get("cot_regime") or "").strip().lower()
    whale_div = _finite(row.get("whale_retail_divergence"))
    if whale_div is None:
        top_ls = _finite(row.get("top_position_ls_ratio"))
        glob_ls = _finite(row.get("global_account_ls_ratio"))
        if top_ls is not None and glob_ls is not None and glob_ls > 0:
            whale_div = top_ls / glob_ls
    return bool(
        (funding_z is not None and funding_z <= fz_max)
        or (funding_rate is not None and funding_rate <= fr_max)
        or liq in {"short_squeeze_liquidation", "short_liquidation_squeeze"}
        or (
            cot == "cot_institutional_squeeze_favorable"
            and whale_div is not None
            and whale_div >= S2_WHALE_DIV_MIN
        )
    )


def is_s4_crowded_long_candidate_row(row: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Return ``(is_state4, hard_trap_reason)`` matching ``evaluate_five_state_regime``."""
    if is_state5_crisis_row(row) or is_s2_squeeze_candidate_row(row):
        return False, None
    funding_z = _finite(row.get("funding_z", row.get("jev_funding_z")))
    funding_rate = _finite(row.get("funding_rate", row.get("lastFundingRate")))
    glob_ls = _finite(row.get("global_account_ls_ratio"))
    top_ls = _finite(row.get("top_position_ls_ratio"))
    whale_div = _finite(row.get("whale_retail_divergence"))
    if whale_div is None and top_ls is not None and glob_ls is not None and glob_ls > 0:
        whale_div = top_ls / glob_ls
    if (
        glob_ls is not None and glob_ls >= 2.20 and whale_div is not None and whale_div <= 0.45
    ) or (funding_z is not None and funding_z > 2.5):
        return True, "regime4_extreme_crowded_long_trap"
    cot = str(row.get("cot_regime") or "").strip().lower()
    prov = str(row.get("put_wing_residual_provenance") or "").strip().lower()
    put_res = _finite(row.get("put_wing_iv_residual_pct")) if prov == "leave_one_out" else None
    is_s4 = bool(
        (put_res is not None and put_res >= 4.0)
        or cot == "cot_leveraged_long_crowded"
        or (funding_z is not None and funding_z >= 1.8)
        or (funding_rate is not None and funding_rate >= 0.00035)
        or (glob_ls is not None and glob_ls >= 2.0 and (whale_div is None or whale_div <= 0.65))
    )
    return is_s4, None


@dataclass(frozen=True)
class RuledZoneAdmission:
    admitted: bool
    reject_reason: str | None
    tier: str
    entry_reason: str
    order_type: str
    limit_price: float | None
    stop_price: float | None
    zone_min: float
    zone_max: float
    multiplier: float
    size_note: str
    odds: float | None


def evaluate_s2_squeeze_zone(
    row: Mapping[str, Any],
    *,
    low: float,
    mid: float,
    price: float,
    span: float,
    alpha: float,
    ratio: float,
    taker_buy_ratio: float | None,
    environ: Mapping[str, str] | None = None,
) -> RuledZoneAdmission:
    """Evaluate State-2 short-squeeze ``LIMIT_MAKER`` entry admission."""
    alpha_cap = _env_float("REGIME_V2_S2_ALPHA_CAP", S2_ALPHA_CAP, environ)
    min_tbr = _env_float("REGIME_V2_S2_MIN_TAKER_BUY_RATIO", S2_MIN_TAKER_BUY_RATIO, environ)
    min_obi = _env_float("REGIME_V2_S2_MIN_OBI_TOP5", S2_MIN_OBI_TOP5, environ)
    min_odds = _env_float("REGIME_V2_S2_MIN_ODDS", S2_MIN_ODDS, environ)

    zone_min = low + S2_ALPHA_FLOOR * span
    zone_max = low + alpha_cap * span

    if ratio < S2_MIN_TRADE_SIZE_RATIO - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state2_retail_crowded_skip", "retail_crowded", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if taker_buy_ratio is None:
        return RuledZoneAdmission(
            False, "state2_taker_buy_ratio_missing", "squeeze", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if taker_buy_ratio < min_tbr - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state2_taker_buy_below_min", "squeeze", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    obi = _finite(row.get("obi_top5") or row.get("spot_obi_top5"))
    if obi is None:
        return RuledZoneAdmission(
            False, "state2_obi_top5_missing", "squeeze", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if obi < min_obi - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state2_obi_top5_below_min", "squeeze", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    oi_chg = _finite(row.get("open_interest_change_1h") or row.get("oi_change_1h_pct"))
    if oi_chg is not None and oi_chg <= -S2_MAX_OI_DROP_1H_PCT:
        return RuledZoneAdmission(
            False, "state2_oi_collapsing", "squeeze", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if price >= mid - _CMP_EPS:
        return RuledZoneAdmission(
            False, "price_above_mid", "squeeze", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if alpha < S2_ALPHA_FLOOR - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state2_alpha_below_floor", "squeeze", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if alpha > alpha_cap + _CMP_EPS:
        return RuledZoneAdmission(
            False, "state2_alpha_above_cap", "squeeze", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )

    limit_alpha = min(alpha - 0.02, S2_LIMIT_ALPHA_CAP) if alpha >= 0.10 else 0.06
    limit_price = low + limit_alpha * span
    if not (0.0 < limit_price < price - _CMP_EPS):
        limit_price = price * 0.999
    stop_price = low - S2_STOP_SPAN_FRAC * span
    if stop_price <= 0.0 or stop_price >= limit_price - _CMP_EPS:
        return RuledZoneAdmission(
            False, "stop_not_below_price", "squeeze", "", "NONE",
            None, stop_price, zone_min, zone_max, 0.0, "", None,
        )
    odds = (mid - limit_price) / (limit_price - stop_price)
    if odds < min_odds - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state2_odds_below_min", "squeeze", "", "NONE",
            None, stop_price, zone_min, zone_max, 0.0, "", odds,
        )
    if stop_distance_reason(limit_price, stop_price):
        return RuledZoneAdmission(
            False, "stop_too_close_to_entry", "squeeze", "", "NONE",
            None, stop_price, zone_min, zone_max, 0.0, "", odds,
        )

    is_large = (
        ratio >= S2_LARGE_TRADE_SIZE_RATIO - _CMP_EPS
        and taker_buy_ratio >= S2_LARGE_TAKER_BUY_RATIO - _CMP_EPS
    )
    entry_reason = (
        "state2_squeeze_large_limit_maker" if is_large else "state2_squeeze_limit_maker"
    )
    mult = 1.00 if is_large else 0.75
    tier_name = "large_trade" if is_large else "neutral"
    return RuledZoneAdmission(
        admitted=True,
        reject_reason=None,
        tier=tier_name,
        entry_reason=entry_reason,
        order_type="LIMIT_MAKER",
        limit_price=limit_price,
        stop_price=stop_price,
        zone_min=zone_min,
        zone_max=zone_max,
        multiplier=mult,
        size_note="state2_squeeze_maker_tier",
        odds=odds,
    )


def evaluate_s4_deep_dip_zone(
    row: Mapping[str, Any],
    *,
    symbol: str,
    low: float,
    mid: float,
    price: float,
    span: float,
    alpha: float,
    deep_zone_price: float,
    ratio: float,
    taker_buy_ratio: float | None,
    trap_reason: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuledZoneAdmission:
    """Evaluate State-4 defensive deep-dip ``LIMIT_MAKER`` entry admission."""
    alpha_cap = _env_float("REGIME_V2_S4_ALPHA_CAP", S4_ALPHA_CAP, environ)
    boll_z_max = _env_float("REGIME_V2_S4_BOLL_Z_MAX", S4_BOLL_Z_MAX, environ)
    min_tbr = _env_float("REGIME_V2_S4_MIN_TAKER_BUY_RATIO", S4_MIN_TAKER_BUY_RATIO, environ)
    min_obi = _env_float("REGIME_V2_S4_MIN_OBI_TOP5", S4_MIN_OBI_TOP5, environ)
    min_odds = _env_float("REGIME_V2_S4_MIN_ODDS", S4_MIN_ODDS, environ)

    zone_min = low
    zone_max = low + alpha_cap * span

    if trap_reason:
        return RuledZoneAdmission(
            False, trap_reason, "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    whitelist_only = (
        str((os.environ if environ is None else environ).get("REGIME_V2_S4_WHITELIST_ONLY", "1"))
        .strip()
        .lower()
        not in _FALSY
    )
    if whitelist_only and get_conviction_tier(symbol) == "SPECULATIVE":
        return RuledZoneAdmission(
            False, "state4_speculative_symbol_rejected", "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if ratio < S4_MIN_TRADE_SIZE_RATIO - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state4_retail_crowded_skip", "retail_crowded", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    boll_z = resolve_boll_z(row)
    if boll_z is None:
        return RuledZoneAdmission(
            False, "state4_boll_z_missing", "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if boll_z > boll_z_max + _CMP_EPS:
        return RuledZoneAdmission(
            False, "state4_boll_z_not_deep_enough", "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if alpha > alpha_cap + _CMP_EPS or alpha < S4_ALPHA_MIN_WICK - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state4_dip_not_deep_enough", "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if taker_buy_ratio is None:
        return RuledZoneAdmission(
            False, "state4_taker_buy_ratio_missing", "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if taker_buy_ratio < min_tbr - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state4_taker_buy_below_min", "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    obi = _finite(row.get("obi_top5") or row.get("spot_obi_top5"))
    if obi is None:
        return RuledZoneAdmission(
            False, "state4_obi_top5_missing", "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )
    if obi < min_obi - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state4_obi_top5_below_min", "deep_fade", "", "NONE",
            None, None, zone_min, zone_max, 0.0, "", None,
        )

    if alpha > 0.05 + _CMP_EPS:
        limit_price = low + 0.05 * span
        stop_price = low - 0.20 * span
    else:
        limit_price = max(deep_zone_price, min(price * 0.998, low - 0.05 * span))
        stop_price = limit_price - 0.25 * span

    if not (0.0 < stop_price < limit_price < price - _CMP_EPS < mid):
        return RuledZoneAdmission(
            False, "state4_limit_geometry_invalid", "deep_fade", "", "NONE",
            None, stop_price, zone_min, zone_max, 0.0, "", None,
        )
    odds = (mid - limit_price) / (limit_price - stop_price)
    if odds < min_odds - _CMP_EPS:
        return RuledZoneAdmission(
            False, "state4_odds_below_min", "deep_fade", "", "NONE",
            None, stop_price, zone_min, zone_max, 0.0, "", odds,
        )
    if stop_distance_reason(limit_price, stop_price):
        return RuledZoneAdmission(
            False, "stop_too_close_to_entry", "deep_fade", "", "NONE",
            None, stop_price, zone_min, zone_max, 0.0, "", odds,
        )

    is_large = (
        ratio >= S4_LARGE_TRADE_SIZE_RATIO - _CMP_EPS
        and taker_buy_ratio >= S4_LARGE_TAKER_BUY_RATIO - _CMP_EPS
    )
    mult = 1.00 if is_large else 0.50
    return RuledZoneAdmission(
        admitted=True,
        reject_reason=None,
        tier="deep_fade",
        entry_reason="state4_deep_limit_maker",
        order_type="LIMIT_MAKER",
        limit_price=limit_price,
        stop_price=stop_price,
        zone_min=zone_min,
        zone_max=zone_max,
        multiplier=mult,
        size_note="state4_defensive_deep_maker",
        odds=odds,
    )


def verify_s2_router_admission(
    signal: Mapping[str, Any] | None,
    row: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
    now_ms: int | None = None,
) -> tuple[bool, str | None]:
    """Re-run the squeeze zone on the source row. A pasted reason is not enough."""
    if not s2_entry_enabled(environ):
        return False, "state2_entry_module_not_live"
    if row is None:
        return False, "state2_squeeze_entry_unconfirmed"
    if not is_s2_squeeze_candidate_row(row, environ=environ):
        return False, "state2_squeeze_entry_unconfirmed"
    if now_ms is not None:
        fresh = check_row_and_tick_freshness(
            row,
            now_ms=now_ms,
            require_all_clocks=True,
            environ=environ,
        )
        if fresh:
            return False, fresh[0]
    low = _finite(row.get("kline_low") or row.get("low"))
    mid = _finite(row.get("boll_mid"))
    price = _finite(row.get("price_usd") or row.get("price") or row.get("close"))
    ratio = _finite(row.get("trade_size_ratio"))
    taker = _finite(row.get("taker_buy_ratio") or row.get("spot_taker_buy_ratio"))
    if low is None or mid is None or price is None or ratio is None or mid <= low:
        return False, "state2_squeeze_entry_unconfirmed"
    span = mid - low
    alpha = (price - low) / span
    admission = evaluate_s2_squeeze_zone(
        row,
        low=low,
        mid=mid,
        price=price,
        span=span,
        alpha=alpha,
        ratio=ratio,
        taker_buy_ratio=taker,
        environ=environ,
    )
    if not admission.admitted:
        return False, admission.reject_reason or "state2_squeeze_entry_unconfirmed"
    if signal is not None:
        order_type = str(
            signal.get("order_type") or signal.get("recommended_order_type") or ""
        ).strip().upper()
        if order_type and order_type != "LIMIT_MAKER":
            return False, "state2_requires_limit_maker"
        posted_limit = _finite(signal.get("limit_price"))
        if (
            posted_limit is not None
            and admission.limit_price is not None
            and abs(posted_limit - admission.limit_price) > 1e-6
        ):
            return False, "state2_limit_price_invalid"
    return True, None


def verify_s4_router_admission(
    signal: Mapping[str, Any] | None,
    row: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[bool, str | None]:
    """Router-side verification that a State-4 buy satisfies S4 deep-dip ``LIMIT_MAKER`` rules."""
    if not s4_entry_enabled(environ):
        return False, "state4_deep_entry_undefined"
    if signal is None or row is None:
        return False, "state4_deep_dip_conditions_not_met"
    merged: dict[str, Any] = dict(row)
    merged.update(signal)
    sym = str(merged.get("binance_symbol") or merged.get("symbol") or merged.get("token_address") or "").strip().upper()
    whitelist_only = (
        str((os.environ if environ is None else environ).get("REGIME_V2_S4_WHITELIST_ONLY", "1"))
        .strip()
        .lower()
        not in _FALSY
    )
    if whitelist_only and get_conviction_tier(sym) == "SPECULATIVE":
        return False, "state4_speculative_symbol_rejected"

    boll_z_max = _env_float("REGIME_V2_S4_BOLL_Z_MAX", S4_BOLL_Z_MAX, environ)
    alpha_cap = _env_float("REGIME_V2_S4_ALPHA_CAP", S4_ALPHA_CAP, environ)
    min_tbr = _env_float("REGIME_V2_S4_MIN_TAKER_BUY_RATIO", S4_MIN_TAKER_BUY_RATIO, environ)
    min_obi = _env_float("REGIME_V2_S4_MIN_OBI_TOP5", S4_MIN_OBI_TOP5, environ)

    boll_z = resolve_boll_z(merged)
    if boll_z is None or boll_z > boll_z_max + _CMP_EPS:
        return False, "state4_boll_z_not_deep_enough"
    alpha = _finite(merged.get("alpha"))
    if alpha is None:
        low = _finite(merged.get("kline_low") or merged.get("low"))
        mid = _finite(merged.get("boll_mid"))
        px = _finite(merged.get("price_usd") or merged.get("price"))
        if low is not None and mid is not None and px is not None and mid > low + _CMP_EPS:
            alpha = (px - low) / (mid - low)
    if alpha is None or alpha > alpha_cap + _CMP_EPS or alpha < S4_ALPHA_MIN_WICK - _CMP_EPS:
        return False, "state4_dip_not_deep_enough"

    tbr = _finite(merged.get("taker_buy_ratio") or merged.get("spot_taker_buy_ratio"))
    if tbr is None or tbr < min_tbr - _CMP_EPS:
        return False, "state4_taker_buy_below_min"
    obi = _finite(merged.get("obi_top5") or merged.get("spot_obi_top5"))
    if obi is None or obi < min_obi - _CMP_EPS:
        return False, "state4_obi_top5_below_min"

    order_type = str(merged.get("order_type") or "").strip().upper()
    if order_type != "LIMIT_MAKER":
        return False, "state4_requires_limit_maker"
    lim = _finite(merged.get("limit_price"))
    px = _finite(merged.get("price_usd") or merged.get("price"))
    if lim is None or px is None or not (0.0 < lim < px):
        return False, "state4_limit_price_invalid"
    return True, None


def is_cme_or_us_cross_asset_open(now_ms: int) -> bool:
    """CME Globex / US cross-asset active hours: Sun 22:00 UTC .. Fri 21:00 UTC (excl. 21:00-22:00 break)."""
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    wd = dt.weekday()  # Mon=0 .. Sun=6
    hour = dt.hour
    if wd == 5:
        return False
    if wd == 6:
        return hour >= 22
    if wd == 4 and hour >= 21:
        return False
    if hour == 21:
        return False
    return True


def _parse_iso_ms(raw: object) -> int | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw) if math.isfinite(float(raw)) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.isdigit():
        return int(text)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            continue
    try:
        iso = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if iso.tzinfo is None:
            iso = iso.replace(tzinfo=timezone.utc)
        return int(iso.timestamp() * 1000)
    except ValueError:
        return None


def _age_invalid(ts_ms: int | None, now_ms: int, window_ms: int) -> bool:
    if ts_ms is None:
        return True
    age = now_ms - ts_ms
    return age < -120_000 or age > window_ms


def _epoch_unit_mismatch(raw: object) -> bool:
    """True when a millisecond field holds a unix-seconds magnitude. Do not rescale it."""
    number = _as_int(raw)
    if number is None and isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            number = int(text)
    if number is None:
        return False
    return 1_000_000_000 <= number < 100_000_000_000


def _source_clock_ms(raw: object) -> tuple[int | None, str | None]:
    if raw in (None, ""):
        return None, None
    if _epoch_unit_mismatch(raw):
        return None, "unit"
    if isinstance(raw, str) and not raw.strip().isdigit():
        return _parse_iso_ms(raw), None
    return _as_int(raw), None


def _clock_ms_or_none(raw: object) -> int | None:
    """Parsed millisecond stamp. A seconds-magnitude value is not rescaled."""
    ts, unit = _source_clock_ms(raw)
    if unit == "unit" or ts is None:
        return None
    return ts


def _cot_date_text(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def build_cross_margin_freshness_clocks(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy real source stamps onto the execution freshness clocks.

    A missing, blank, or unparseable source is omitted. Nothing here fills a
    clock from ``now``, from ``due_cftc_asof``, or from a fresher sibling
    stamp. ``deribit_gamma_ts_ms`` is ``deribit_options_gamma.ts_ms`` (the bar
    source time already stored as ``_deribit_gamma_ts_ms``). ``collected_at_ms``
    is not read. ``perp_counterparty_ts_ms`` is ``binance_perp_counterparty.ts_ms``.
    ``updated_at`` is not the indicator clock.
    """
    clocks: dict[str, Any] = {}
    indicator_ms = None
    for key in ("indicators_ts_ms", "_ind_ts_ms", "tech_updated_at"):
        indicator_ms = _clock_ms_or_none(row.get(key))
        if indicator_ms is not None:
            break
    if indicator_ms is not None:
        clocks["indicators_ts_ms"] = indicator_ms

    obi_ms = _clock_ms_or_none(row.get("obi_top5_source_ts_ms"))
    if obi_ms is not None:
        clocks["obi_top5_source_ts_ms"] = obi_ms

    perp_ms = _clock_ms_or_none(row.get("_perp_counterparty_ts_ms"))
    if perp_ms is None:
        perp_ms = _clock_ms_or_none(row.get("perp_counterparty_ts_ms"))
    if perp_ms is not None:
        clocks["perp_counterparty_ts_ms"] = perp_ms

    deribit_ms = _clock_ms_or_none(row.get("_deribit_gamma_ts_ms"))
    if deribit_ms is None:
        deribit_ms = _clock_ms_or_none(row.get("deribit_gamma_source_ts_ms"))
    if deribit_ms is not None:
        clocks["deribit_gamma_ts_ms"] = deribit_ms

    volume_ms = _clock_ms_or_none(row.get("volume_z_source_close_ms"))
    if volume_ms is not None:
        clocks["volume_z_source_close_ms"] = volume_ms

    news_ms = _clock_ms_or_none(row.get("news_fetched_at_ms"))
    if news_ms is None:
        news_ms = _clock_ms_or_none(row.get("news_fetched_at"))
    if news_ms is not None:
        clocks["news_fetched_at_ms"] = news_ms

    market_open = row.get("lseg_market_open")
    if isinstance(market_open, bool):
        clocks["lseg_market_open"] = market_open
    lseg_ms = _clock_ms_or_none(row.get("_lseg_cross_asset_ts_ms"))
    if lseg_ms is None:
        lseg_ms = _clock_ms_or_none(row.get("lseg_cross_asset_ts_ms"))
    if lseg_ms is not None:
        clocks["lseg_cross_asset_ts_ms"] = lseg_ms

    cot = _cot_date_text(row.get("_cot_max_date"))
    if cot is None:
        cot = _cot_date_text(row.get("cot_asof_date"))
    if cot is not None:
        clocks["cot_asof_date"] = cot
    return clocks


def merge_cross_margin_clocks(
    row: Mapping[str, Any],
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Source stamps first. A non-empty explicit clock overrides that key."""
    clocks = build_cross_margin_freshness_clocks(row)
    if isinstance(existing, Mapping):
        for key, value in existing.items():
            if value not in (None, ""):
                clocks[str(key)] = value
    return clocks


_EMIT_CLOCK_KEYS = (
    "indicators_ts_ms",
    "obi_top5_source_ts_ms",
    "perp_counterparty_ts_ms",
    "deribit_gamma_ts_ms",
    "volume_z_source_close_ms",
    "news_fetched_at_ms",
    "cot_asof_date",
)


def cross_margin_emit_clock_gap(clocks: Mapping[str, Any]) -> str | None:
    """Name the first execution clock that is absent. A present stale stamp is kept.

    ``snapshot_observed_at_ms`` is written from the live account read, not here.
    ``lseg_cross_asset_ts_ms`` is required only when the row did not say the
    cash session is closed. No timestamp is synthesized.
    """
    for key in _EMIT_CLOCK_KEYS:
        if clocks.get(key) in (None, ""):
            return key
    if clocks.get("lseg_market_open") is not False and clocks.get("lseg_cross_asset_ts_ms") in (
        None,
        "",
    ):
        return "lseg_cross_asset_ts_ms"
    return None


def drop_cross_margin_signals_missing_clocks(
    signals: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Do not publish a cross-margin intent whose real clocks are incomplete."""
    kept: list[dict[str, Any]] = []
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        clocks = sig.get("clocks") if isinstance(sig.get("clocks"), Mapping) else {}
        gap = cross_margin_emit_clock_gap(clocks)
        if gap is None:
            kept.append(sig)
    return kept


def stamp_cross_margin_signal_clocks(
    signals: Sequence[dict[str, Any]],
    hot_rows: Sequence[Mapping[str, Any]],
    *,
    news_fetched_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Write execution clocks onto signals about to be published.

    Hot rows can carry ``volume_z_source_close_ms`` that was attached after
    the strategy copied ``market_row``. ``news_fetched_ms`` is the whitelist
    ``fetched_at`` already read for this tick. A missing value is not filled.
    """
    by_symbol: dict[str, Mapping[str, Any]] = {}
    for row in hot_rows:
        if not isinstance(row, Mapping):
            continue
        for key in ("binance_symbol", "symbol", "token_address"):
            symbol = str(row.get(key) or "").strip().upper()
            if symbol:
                by_symbol.setdefault(symbol, row)
    news_ms = _clock_ms_or_none(news_fetched_ms)
    carried = (
        "volume_z_source_close_ms",
        "_perp_counterparty_ts_ms",
        "perp_counterparty_ts_ms",
        "_deribit_gamma_ts_ms",
        "deribit_gamma_source_ts_ms",
        "_lseg_cross_asset_ts_ms",
        "lseg_cross_asset_ts_ms",
        "_cot_max_date",
        "cot_asof_date",
        "lseg_market_open",
        "news_fetched_at_ms",
        "news_fetched_at",
        "tech_updated_at",
        "indicators_ts_ms",
        "_ind_ts_ms",
        "obi_top5_source_ts_ms",
    )
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        market = sig.get("market_row")
        if not isinstance(market, dict):
            market = {}
            sig["market_row"] = market
        symbol = str(
            sig.get("binance_symbol") or sig.get("symbol") or market.get("binance_symbol") or ""
        ).strip().upper()
        hot = by_symbol.get(symbol) or {}
        view: dict[str, Any] = dict(hot)
        for key, value in market.items():
            if value not in (None, ""):
                view[key] = value
        if news_ms is not None and _clock_ms_or_none(view.get("news_fetched_at_ms")) is None:
            if _clock_ms_or_none(view.get("news_fetched_at")) is None:
                view["news_fetched_at_ms"] = news_ms
        for key in carried:
            if key in view and key not in market and view[key] not in (None, ""):
                market[key] = view[key]
        prior = sig.get("clocks") if isinstance(sig.get("clocks"), Mapping) else None
        sig["clocks"] = merge_cross_margin_clocks(view, prior)
    return list(signals)


def query_latest_whitelist_news_fetched_ms(db_path: str | Path | None) -> int | None:
    if db_path is None or not str(db_path).strip():
        return None
    target = Path(str(db_path)).expanduser()
    if not target.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{target.resolve().as_posix()}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            """
            SELECT MAX(fetched_at) AS fetched_at
            FROM news_events
            WHERE lower(source_tier) = 'whitelist'
            """
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None or not row["fetched_at"]:
        return None
    return _parse_iso_ms(str(row["fetched_at"]))


def entry_kline_5m_clock_reason(
    row: Mapping[str, Any],
    *,
    now_ms: int,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Closed 5-minute bar clock. Missing, future, stale, or seconds-valued stamps fail."""
    raw = row.get("entry_kline_5m_close_ms")
    if raw in (None, ""):
        return "entry_5m_kline_clock_absent"
    if _epoch_unit_mismatch(raw):
        return "entry_5m_kline_clock_unit_mismatch"
    close_ms, unit = _source_clock_ms(raw)
    if unit == "unit":
        return "entry_5m_kline_clock_unit_mismatch"
    if close_ms is None:
        return "entry_5m_kline_clock_absent"
    window_ms = _env_int(
        ENTRY_KLINE_5M_MAX_AGE_ENV, DEFAULT_ENTRY_KLINE_5M_MAX_AGE_SEC, environ
    ) * 1000
    if _age_invalid(close_ms, now_ms, window_ms):
        return "entry_5m_kline_stale"
    return None


def check_row_and_tick_freshness(
    row: Mapping[str, Any],
    *,
    now_ms: int,
    news_snapshot: Any = None,
    require_all_clocks: bool = False,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Check 9-datasource freshness clocks on ``row`` and ``news_snapshot``.

    - Never reads file ``mtime``.
    - Uses ``due_cftc_asof`` for COT and schema/parseable ``next_unlock_utc`` for unlocks.
    - When ``require_all_clocks=True`` (``REGIME_V2_STALE_DATA_POLICY=fail_closed``),
      missing clocks fail closed; otherwise present clocks that are stale fail closed.
    """
    env = os.environ if environ is None else environ
    if v2_contract_active(env):
        require_all_clocks = True
    reasons: list[str] = []
    ind_window_ms = int(binance_indicators_fresh_sec()) * 1000
    kline_5m_window_ms = (
        _env_int(ENTRY_KLINE_5M_MAX_AGE_ENV, DEFAULT_ENTRY_KLINE_5M_MAX_AGE_SEC, env) * 1000
    )
    news_window_ms = int(NEWS_WINDOW_MIN) * 60_000

    # 1. Binance indicators. ``updated_at`` is a generic row stamp, not this clock.
    ind_raw = row.get("indicators_ts_ms")
    if ind_raw in (None, ""):
        ind_raw = row.get("_ind_ts_ms")
    if ind_raw in (None, ""):
        ind_raw = row.get("tech_updated_at")
    ind_ts, ind_unit = _source_clock_ms(ind_raw)
    if ind_unit == "unit":
        reasons.append("binance_indicators_clock_unit_mismatch")
    elif ind_ts is not None:
        if _age_invalid(ind_ts, now_ms, ind_window_ms):
            reasons.append("binance_indicators_stale")
    elif require_all_clocks:
        reasons.append("binance_indicators_clock_absent")

    turn_ts = _parse_iso_ms(row.get("quote_turnover_24h_asof"))
    if turn_ts is not None:
        if _age_invalid(turn_ts, now_ms, ind_window_ms):
            reasons.append("quote_turnover_stale")
    elif require_all_clocks:
        reasons.append("quote_turnover_clock_absent")

    # 2. 5m entry kline close_time_ms. A seconds value is not rescaled into milliseconds.
    k5_raw = row.get("entry_kline_5m_close_ms")
    k5_ts, k5_unit = _source_clock_ms(k5_raw)
    if k5_unit == "unit":
        reasons.append("entry_5m_kline_clock_unit_mismatch")
    elif k5_ts is not None:
        if _age_invalid(k5_ts, now_ms, kline_5m_window_ms):
            reasons.append("entry_5m_kline_stale")
    elif require_all_clocks:
        reasons.append("entry_5m_kline_clock_absent")

    # 3. 1h volume_z source_close_time_ms (inclusive 3600000ms boundary)
    vz_close = _as_int(row.get("volume_z_source_close_ms"))
    if vz_close is not None:
        if vz_close > now_ms + 120_000 or not source_close_is_fresh(vz_close, now_ms):
            reasons.append("volume_z_source_close_stale")
    elif require_all_clocks:
        reasons.append("volume_z_source_close_absent")

    # 4. Spot Top-5 OBI reader clock (obi_top5_source_ts_ms, never ts_ms)
    obi_ts = _as_int(row.get("obi_top5_source_ts_ms"))
    if obi_ts is not None:
        if _age_invalid(obi_ts, now_ms, ind_window_ms):
            reasons.append("obi_top5_source_stale")
    elif require_all_clocks:
        reasons.append("obi_top5_source_clock_absent")

    # 5. Public derivatives clocks: perp counterparty, cross-exchange baseline, Deribit gamma
    for key, label in (
        ("_perp_counterparty_ts_ms", "perp_counterparty"),
        ("_cross_exchange_ts_ms", "cross_exchange_spot_baseline"),
        ("_deribit_gamma_ts_ms", "deribit_options_gamma"),
    ):
        ts_val = _as_int(row.get(key))
        if ts_val is not None:
            if _age_invalid(ts_val, now_ms, ind_window_ms):
                reasons.append(f"{label}_stale")
        elif require_all_clocks:
            reasons.append(f"{label}_clock_absent")

    if require_all_clocks and row.get("_unlock_schedule_verified") is False:
        reasons.append("token_unlock_schedule_unverified")

    # 6. Whitelist news fetched_at clock (90m)
    news_fetched_ms = _parse_iso_ms(row.get("news_fetched_at_ms") or row.get("news_fetched_at"))
    if news_fetched_ms is None and news_snapshot is not None:
        avail = getattr(news_snapshot, "availability", None)
        avail_val = getattr(avail, "value", str(avail or "")).lower()
        if avail_val and avail_val != "ok":
            if require_all_clocks:
                reasons.append(f"news_events_{avail_val}")
        else:
            news_fetched_ms = query_latest_whitelist_news_fetched_ms(
                getattr(news_snapshot, "db_path", None)
            )
    if news_fetched_ms is not None:
        if _age_invalid(news_fetched_ms, now_ms, news_window_ms):
            reasons.append("news_fetched_at_stale")
    elif require_all_clocks:
        reasons.append("news_fetched_at_absent")

    # 7. LSEG 5m cross-asset bars clock (90m when market open)
    market_open_raw = row.get("lseg_market_open")
    market_open = (
        bool(market_open_raw)
        if isinstance(market_open_raw, bool)
        else is_cme_or_us_cross_asset_open(now_ms)
    )
    lseg_ts = _as_int(row.get("_lseg_cross_asset_ts_ms") or row.get("lseg_cross_asset_ts_ms"))
    if lseg_ts is not None:
        if market_open and _age_invalid(lseg_ts, now_ms, int(MAX_CROSS_ASSET_AGE_MS)):
            reasons.append("lseg_cross_asset_bars_stale")
    elif require_all_clocks and market_open:
        reasons.append("lseg_cross_asset_bars_absent")

    # 8. CFTC COT publication calendar (exact match to due_cftc_asof, no seconds TTL)
    cot_date_str = str(row.get("_cot_max_date") or row.get("cot_asof_date") or "").strip()[:10]
    expected_due = due_cftc_asof(
        datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    ).isoformat()
    if cot_date_str:
        if cot_date_str != expected_due:
            reasons.append("cot_publication_not_current")
    elif require_all_clocks:
        reasons.append("cot_publication_absent")

    return reasons


@dataclass(frozen=True)
class LiveEquityView:
    ok: bool
    error: str | None
    source: str
    equity_usdt: float
    usdt_free: float
    usdt_locked: float
    held_assets_usdt: float
    fetched_at_ms: int | None
    uid: str | None


def resolve_regime_v2_equity(
    *,
    now_ms: int,
    hot_rows: Sequence[Mapping[str, Any]] = (),
    open_positions: Sequence[Mapping[str, Any]] = (),
    environ: Mapping[str, str] | None = None,
) -> LiveEquityView:
    """Resolve real-time spot account equity & free USDT (fail-closed when configured)."""
    env = os.environ if environ is None else environ
    snap_path_raw = str(env.get("BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH", "")).strip()
    max_age_sec = _env_int(SNAPSHOT_MAX_AGE_ENV, DEFAULT_ACCOUNT_SNAPSHOT_MAX_AGE_SEC, env)
    enforce_live = str(env.get(ENFORCE_LIVE_EQUITY_ENV, "")).strip().lower() in _TRUTHY or (
        stale_data_fail_closed_active(env)
        and str(env.get(EQUITY_SOURCE_ENV, "")).strip() == RULED_EQUITY_SOURCE
    )

    px_by_base: dict[str, float] = {"USDT": 1.0, "FDUSD": 1.0, "USDC": 1.0}
    for row in hot_rows:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("binance_symbol") or row.get("symbol") or "").strip().upper()
        px = _finite(row.get("price_usd") or row.get("close") or row.get("price"))
        if sym.endswith("USDT") and px is not None and px > 0:
            px_by_base[sym[:-4]] = px

    cost_by_base: dict[str, float] = {}
    for pos in open_positions:
        if not isinstance(pos, Mapping):
            continue
        sym = str(pos.get("symbol") or "").strip().upper()
        cost = _finite(pos.get("entry_cost_usdt") or pos.get("notional_usdt"))
        if sym.endswith("USDT") and cost is not None and cost > 0:
            cost_by_base[sym[:-4]] = cost_by_base.get(sym[:-4], 0.0) + cost

    if snap_path_raw:
        from execution import binance_account_snapshot as snap_mod

        sign_val = (snap_mod._env_secret(snap_mod.API_SECRET_ENV) if environ is None else str(env.get(snap_mod.API_SECRET_ENV, "")).strip())
        if not sign_val:
            return LiveEquityView(
                False, "account_snapshot_secret_missing", "signed_snapshot",
                0.0, 0.0, 0.0, 0.0, None, None,
            )
        pinned_usdt = str(env.get("BINANCE_SPOT_REST_USDT_FREE", "")).strip() or None
        if pinned_usdt is None:
            try:
                peek_doc = json.loads(Path(snap_path_raw).expanduser().read_text(encoding="utf-8"))
                for b_item in (peek_doc.get("payload") or {}).get("balances") or []:
                    if isinstance(b_item, Mapping) and str(b_item.get("asset") or "").upper() == "USDT":
                        pinned_usdt = str(b_item.get("free") or "")
                        break
            except (OSError, ValueError, TypeError, AttributeError):
                pinned_usdt = None
        expected: dict[str, Any] = {
            "account_type": str(env.get("BINANCE_SPOT_REST_ACCOUNT_TYPE", "SPOT")).strip() or "SPOT",
            "usdt_free": pinned_usdt,
        }
        rest_uid = str(env.get("BINANCE_SPOT_REST_UID", "")).strip()
        if rest_uid:
            expected["uid"] = rest_uid
        view, err = snap_mod.load_snapshot_file(
            Path(snap_path_raw).expanduser(),
            secret=sign_val,
            now_ms=now_ms,
            max_age_ms=max_age_sec * 1000,
            expected=expected,
        )
        if err or view is None:
            return LiveEquityView(
                False, f"account_snapshot_{err or 'invalid'}", "signed_snapshot",
                0.0, 0.0, 0.0, 0.0, None, None,
            )
        uid_str = str(view.get("uid") or "").strip()
        if v2_contract_active(env) and not uid_str:
            return LiveEquityView(
                False, "account_identity_missing", "signed_snapshot",
                0.0, 0.0, 0.0, 0.0, None, None,
            )
        if uid_str == MCP_FORBIDDEN_UID:
            return LiveEquityView(
                False, "mcp_account_forbidden", "signed_snapshot",
                0.0, 0.0, 0.0, 0.0, None, uid_str,
            )
        raw_doc = json.loads(Path(snap_path_raw).expanduser().read_text(encoding="utf-8"))
        fetched_raw = raw_doc.get("fetched_at_ms")
        if v2_contract_active(env) and _epoch_unit_mismatch(fetched_raw):
            return LiveEquityView(
                False, "account_snapshot_clock_unit_mismatch", "signed_snapshot",
                0.0, 0.0, 0.0, 0.0, None, uid_str,
            )
        fetched_ms = _as_int(fetched_raw)
        if v2_contract_active(env) and fetched_ms is None:
            return LiveEquityView(
                False, "account_snapshot_clock_absent", "signed_snapshot",
                0.0, 0.0, 0.0, 0.0, None, uid_str,
            )

        usdt_free = 0.0
        usdt_locked = 0.0
        held_usdt = 0.0
        balances = view.get("balances")
        if not isinstance(balances, list):
            return LiveEquityView(
                False, "account_snapshot_balances_missing", "signed_snapshot",
                0.0, 0.0, 0.0, 0.0, fetched_ms, uid_str,
            )
        for item in balances:
            if not isinstance(item, Mapping):
                continue
            asset = str(item.get("asset") or "").strip().upper()
            if item.get("free") not in (None, "") and _finite(item.get("free")) is None:
                return LiveEquityView(
                    False, "account_balance_not_finite", "signed_snapshot",
                    0.0, 0.0, 0.0, 0.0, fetched_ms, uid_str,
                )
            if item.get("locked") not in (None, "") and _finite(item.get("locked")) is None:
                return LiveEquityView(
                    False, "account_balance_not_finite", "signed_snapshot",
                    0.0, 0.0, 0.0, 0.0, fetched_ms, uid_str,
                )
            free_v = _finite(item.get("free")) or 0.0
            lock_v = _finite(item.get("locked")) or 0.0
            tot_v = free_v + lock_v
            if tot_v <= 0:
                continue
            if asset == "USDT":
                usdt_free = free_v
                usdt_locked = lock_v
            elif asset in px_by_base:
                held_usdt += tot_v * px_by_base[asset]
            elif v2_contract_active(env):
                return LiveEquityView(
                    False, "account_quote_unpriced", "signed_snapshot",
                    0.0, 0.0, 0.0, 0.0, fetched_ms, uid_str,
                )
            elif asset in cost_by_base:
                held_usdt += cost_by_base[asset]
        equity = usdt_free + usdt_locked + held_usdt
        if equity <= 0:
            return LiveEquityView(
                False, "account_equity_not_positive", "signed_snapshot",
                0.0, usdt_free, usdt_locked, held_usdt, fetched_ms, uid_str,
            )
        return LiveEquityView(
            True, None, "signed_snapshot", equity, usdt_free, usdt_locked,
            held_usdt, fetched_ms, uid_str or None,
        )

    output_mode = str(env.get("REGIME_V2_OUTPUT_MODE", "")).strip().lower()
    if v2_contract_active(env):
        if output_mode == "dry_run":
            dry_eq = _finite(env.get(DRY_RUN_EQUITY_ENV))
            if dry_eq is None or dry_eq <= 0:
                return LiveEquityView(
                    False, "dry_run_equity_invalid", "dry_run_override",
                    0.0, 0.0, 0.0, 0.0, None, "dry_run",
                )
            committed = sum(cost_by_base.values())
            default_free = max(0.0, dry_eq - committed)
            dry_free = _finite(env.get(DRY_RUN_FREE_USDT_ENV))
            resolved_free = dry_free if dry_free is not None and dry_free >= 0 else default_free
            return LiveEquityView(
                True, None, "dry_run_override", dry_eq, resolved_free, 0.0,
                committed, None, "dry_run",
            )
        return LiveEquityView(
            False, "account_snapshot_stale_or_absent", "unconfigured",
            0.0, 0.0, 0.0, 0.0, None, None,
        )

    dry_eq = _finite(env.get(DRY_RUN_EQUITY_ENV))
    if dry_eq is not None and dry_eq > 0 and str(env.get(ENFORCE_LIVE_EQUITY_ENV, "")).strip() != "1":
        committed = sum(cost_by_base.values())
        default_free = max(0.0, dry_eq - committed)
        dry_free = _finite(env.get(DRY_RUN_FREE_USDT_ENV))
        resolved_free = dry_free if dry_free is not None and dry_free >= 0 else default_free
        return LiveEquityView(
            True, None, "dry_run_override", dry_eq, resolved_free, 0.0,
            committed, now_ms, "dry_run",
        )

    if enforce_live:
        return LiveEquityView(
            False, "account_snapshot_stale_or_absent", "unconfigured",
            0.0, 0.0, 0.0, 0.0, None, None,
        )

    legacy_book = _finite(env.get("BINANCE_SPOT_BOOK_USDT"))
    if legacy_book is not None and legacy_book > 0:
        committed = sum(cost_by_base.values())
        return LiveEquityView(
            True, None, "legacy_book_env", legacy_book, max(0.0, legacy_book - committed),
            0.0, committed, now_ms, None,
        )

    return LiveEquityView(
        True, None, "unit_fraction_only", 0.0, 0.0, 0.0, 0.0, None, None,
    )


def _active_open_positions_from_ledgers(
    environ: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read open positions across configured execution ledgers; fail closed on unreadable/unprotected."""
    env = os.environ if environ is None else environ
    paths: list[Path] = []
    single = str(env.get("DECISION_EXECUTION_LEDGER", "")).strip()
    if single:
        paths.append(Path(single).expanduser())
    else:
        for k in ("DECISION_EXECUTION_LEDGER_FADE", "DECISION_EXECUTION_LEDGER_CLASSIC"):
            raw = str(env.get(k, "")).strip()
            if raw:
                p = Path(raw).expanduser()
                if p.is_file() or stale_data_fail_closed_active(env):
                    paths.append(p)
    open_rows: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    active_statuses = {
        "open",
        "oco_working",
        "exit_submitted",
        "resting_entry",
        "entry_submitted",
        "oco_unknown",
        "oco_failed",
        "maker_cancel_unknown",
    }
    unprotected_statuses = {"oco_unknown", "oco_failed", "maker_cancel_unknown"}

    for path in paths:
        if not path.is_file():
            if stale_data_fail_closed_active(env) and not single:
                return [], f"execution_ledger_missing:{path.name}"
            continue
        book = read_protection_book(path)
        if book.error:
            return [], f"execution_ledger_error:{book.error}"
        for pos in book.positions:
            if not isinstance(pos, Mapping):
                continue
            status = str(pos.get("status") or "").strip().lower()
            res_qty = _finite(pos.get("residual_qty")) or 0.0
            if status in unprotected_statuses or (status == "closed_dust" and res_qty > 0):
                return [], f"unprotected_or_dust_inventory:{pos.get('symbol')}:{status}"
            if status in active_statuses:
                sym = str(pos.get("symbol") or "").strip().upper()
                key = (str(pos.get("purpose") or ""), sym)
                if key not in seen_keys:
                    seen_keys.add(key)
                    open_rows.append(dict(pos))
    return open_rows, None


def enforce_regime_v2_sizing_and_caps(
    signals: list[dict[str, Any]],
    *,
    hot_rows: Sequence[Mapping[str, Any]] = (),
    now_ms: int | None = None,
    open_positions_override: Sequence[Mapping[str, Any]] | None = None,
    equity_override: LiveEquityView | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enforce real-time 24% equity sizing, 20 USDT floor, concurrency, cluster, beta & total exposure caps."""
    env = os.environ if environ is None else environ
    clock = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if not v2_contract_active(env):
        return signals, {"applied": False}

    buys = [s for s in signals if str(s.get("action") or "").lower() == "buy"]
    if not buys:
        return signals, {"applied": True, "buys_in": 0, "buys_kept": 0, "dropped": {}}

    if open_positions_override is not None:
        open_positions = [dict(p) for p in open_positions_override]
        ledger_err = None
    else:
        open_positions, ledger_err = _active_open_positions_from_ledgers(env)
    if ledger_err:
        non_buys = [s for s in signals if str(s.get("action") or "").lower() != "buy"]
        return non_buys, {
            "applied": True,
            "halt_reason": ledger_err,
            "buys_in": len(buys),
            "buys_kept": 0,
            "dropped": {ledger_err: len(buys)},
        }

    eq_view = (
        equity_override
        if equity_override is not None
        else resolve_regime_v2_equity(
            now_ms=clock,
            hot_rows=hot_rows,
            open_positions=open_positions,
            environ=env,
        )
    )
    if not eq_view.ok:
        err_code = eq_view.error or "live_equity_unavailable"
        non_buys = [s for s in signals if str(s.get("action") or "").lower() != "buy"]
        return non_buys, {
            "applied": True,
            "halt_reason": err_code,
            "buys_in": len(buys),
            "buys_kept": 0,
            "dropped": {err_code: len(buys)},
            "reservation_release_interface": RELEASE_INTERFACE_BLOCK,
        }

    reserve_path = reservation_db_path(env)
    if reserve_path is None:
        non_buys = [s for s in signals if str(s.get("action") or "").lower() != "buy"]
        return non_buys, {
            "applied": True,
            "halt_reason": "reservation_store_unconfigured",
            "buys_in": len(buys),
            "buys_kept": 0,
            "dropped": {"reservation_store_unconfigured": len(buys)},
            "reservation_release_interface": RELEASE_INTERFACE_BLOCK,
        }
    held_reservations = active_reservations(reserve_path)

    max_conc = _env_int("REGIME_V2_MAX_CONCURRENT_POSITIONS", DEFAULT_MAX_CONCURRENT_POSITIONS, env)
    s4_max_conc = _env_int(
        "REGIME_V2_STATE4_MAX_CONCURRENT_POSITIONS", DEFAULT_STATE4_MAX_CONCURRENT_POSITIONS, env
    )
    max_cluster = _env_int("REGIME_V2_MAX_PER_CLUSTER", DEFAULT_MAX_PER_CLUSTER, env)
    max_single_pct = _env_float(
        "REGIME_V2_MAX_SINGLE_EXPOSURE_PCT", DEFAULT_MAX_SINGLE_EXPOSURE_PCT, env
    )
    max_total_pct = _env_float(
        "REGIME_V2_MAX_TOTAL_EXPOSURE_PCT", DEFAULT_MAX_TOTAL_EXPOSURE_PCT, env
    )
    s4_max_total_pct = _env_float(
        "REGIME_V2_STATE4_MAX_TOTAL_EXPOSURE_PCT", DEFAULT_STATE4_MAX_TOTAL_EXPOSURE_PCT, env
    )
    max_beta = _env_float("REGIME_V2_MAX_PORTFOLIO_BETA", DEFAULT_MAX_PORTFOLIO_BETA, env)
    s4_max_beta = _env_float(
        "REGIME_V2_STATE4_MAX_PORTFOLIO_BETA", DEFAULT_STATE4_MAX_PORTFOLIO_BETA, env
    )

    px_by_sym: dict[str, float] = {}
    for r in hot_rows:
        if isinstance(r, Mapping):
            s = str(r.get("binance_symbol") or r.get("symbol") or "").strip().upper()
            px = _finite(r.get("price_usd") or r.get("close") or r.get("price"))
            if s and px is not None and px > 0:
                px_by_sym[s] = px

    open_symbols: set[str] = set()
    cluster_counts: Counter[str] = Counter()
    open_exposure_pct = 0.0
    open_beta_sum = 0.0
    for pos in open_positions:
        sym = str(pos.get("symbol") or "").strip().upper()
        if sym:
            open_symbols.add(sym)
            cluster = symbol_sector_cluster(sym)
            if cluster != "idiosyncratic":
                ckey = cluster_exposure_key(sym) or cluster
                cluster_counts[ckey] += 1
        pos_pct = _finite(pos.get("size_pct"))
        if pos_pct is None or pos_pct <= 0:
            cost = _finite(pos.get("entry_cost_usdt") or pos.get("notional_usdt"))
            qty = _finite(pos.get("qty") or pos.get("executed_qty"))
            if qty is not None and qty > 0 and sym in px_by_sym and eq_view.equity_usdt > 0:
                pos_pct = (qty * px_by_sym[sym]) / eq_view.equity_usdt
            elif cost is not None and cost > 0 and eq_view.equity_usdt > 0:
                pos_pct = cost / eq_view.equity_usdt
            else:
                pos_pct = DEFAULT_BASE_POSITION_PCT
        open_exposure_pct += pos_pct
        open_beta_sum += pos_pct * symbol_beta_vs_btc(sym)
    position_cluster_counts = Counter(cluster_counts)
    position_exposure_pct = open_exposure_pct
    caller_live = eq_view.source == "signed_snapshot"
    for held in held_reservations:
        held_sym = str(held.get("symbol") or "").strip().upper()
        held_mode = str(held.get("mode") or "live")
        same_book = held_mode == ("live" if caller_live else "dry_run")
        if held_sym:
            open_symbols.add(held_sym)
        if held_sym and same_book and held_sym not in {
            str(pos.get("symbol") or "").strip().upper() for pos in open_positions
        }:
            held_cluster = str(held.get("cluster_key") or "")
            if held_cluster:
                cluster_counts[held_cluster] += 1
            held_pct = _finite(held.get("size_pct"))
            if held_pct is not None and held_pct > 0:
                open_exposure_pct += held_pct
                open_beta_sum += held_pct * symbol_beta_vs_btc(held_sym)
            held["_counts_as_open"] = 1
        else:
            held["_counts_as_open"] = 0

    dropped: Counter[str] = Counter()
    accepted_ids: set[int] = set()
    cur_count = len(open_positions) + sum(
        int(item.get("_counts_as_open") or 0) for item in held_reservations
    )
    cur_exposure_pct = open_exposure_pct
    cur_beta = open_beta_sum
    cur_new_notional = 0.0
    s4_new_in_tick = 0

    for sig in signals:
        if str(sig.get("action") or "").lower() != "buy":
            continue
        sym = str(
            sig.get("binance_symbol") or sig.get("symbol") or sig.get("token_address") or ""
        ).strip().upper()
        regime = str(sig.get("market_regime") or "")
        is_s4 = regime == "crowded_long_defensive"

        own_key = signal_reservation_key(sig)
        replay = any(str(item.get("signal_key") or "") == own_key for item in held_reservations)
        if replay:
            sig["reservation_state"] = "reserved"
            sig["reservation_idempotent"] = True
            sig["live_order_permitted"] = eq_view.source == "signed_snapshot"
            if eq_view.source != "signed_snapshot":
                sig["mode"] = "dry_run"
            accepted_ids.add(id(sig))
            continue

        if sym and sym in open_symbols:
            dropped["same_symbol_already_open"] += 1
            continue

        eff_max_conc = s4_max_conc if is_s4 else max_conc
        if cur_count >= eff_max_conc:
            dropped["max_concurrent_positions_reached"] += 1
            continue
        if is_s4 and s4_new_in_tick >= 1:
            dropped["state4_per_tick_cap_reached"] += 1
            continue

        cluster = symbol_sector_cluster(sym)
        ckey = (cluster_exposure_key(sym) or cluster) if cluster != "idiosyncratic" else ""
        if ckey and cluster_counts[ckey] >= max_cluster:
            dropped["cluster_concurrency_cap_reached"] += 1
            continue

        sz = _finite(sig.get("size_pct"))
        if sz is None or sz <= 0:
            dropped["size_pct_invalid"] += 1
            continue

        notional_usdt: float | None = None
        if eq_view.equity_usdt > 0:
            notional_usdt = eq_view.equity_usdt * sz
            if notional_usdt < MIN_EXCHANGE_ORDER_USDT - _CMP_EPS:
                lifted_pct = MIN_EXCHANGE_ORDER_USDT / eq_view.equity_usdt
                if lifted_pct > max_single_pct + _CMP_EPS:
                    dropped["notional_below_20_usdt_exceeds_single_cap"] += 1
                    continue
                sig["size_pct_before_min_notional"] = round(sz, 6)
                sz = round(lifted_pct, 6)
                sig["size_pct"] = sz
                sig["min_notional_lift"] = True
                notional_usdt = eq_view.equity_usdt * sz

        if sz > max_single_pct + _CMP_EPS:
            dropped["single_position_exposure_cap_exceeded"] += 1
            continue

        eff_total_cap = s4_max_total_pct if is_s4 else max_total_pct
        if cur_exposure_pct + sz > eff_total_cap + _CMP_EPS:
            dropped["total_portfolio_exposure_cap_exceeded"] += 1
            continue

        if notional_usdt is not None and eq_view.source != "unit_fraction_only":
            max_usable_free = eq_view.usdt_free * FREE_USDT_BUFFER_RATIO
            if cur_new_notional + notional_usdt > max_usable_free + _CMP_EPS:
                dropped["insufficient_free_usdt_with_buffer"] += 1
                continue

        eff_beta_cap = s4_max_beta if is_s4 else max_beta
        cand_beta = sz * symbol_beta_vs_btc(sym)
        if cur_beta + cand_beta > eff_beta_cap + _CMP_EPS:
            dropped["portfolio_beta_cap_exceeded"] += 1
            continue

        if eq_view.equity_usdt > 0 and notional_usdt is not None:
            sig["live_equity_usdt"] = round(eq_view.equity_usdt, 4)
            sig["planned_notional_usdt"] = round(notional_usdt, 4)
            sig["equity_source"] = eq_view.source
            if eq_view.fetched_at_ms is not None:
                sig["equity_snapshot_fetched_at_ms"] = eq_view.fetched_at_ms
        sig["live_order_permitted"] = eq_view.source == "signed_snapshot"
        if eq_view.source != "signed_snapshot":
            sig["mode"] = "dry_run"

        if reserve_path is None:
            dropped["reservation_store_unconfigured"] += 1
            continue
        reserved = try_reserve(
            reserve_path,
            sig,
            now_ms=clock,
            open_symbols=[str(pos.get("symbol") or "") for pos in open_positions],
            open_count=len(open_positions),
            cluster_counts=position_cluster_counts,
            open_exposure_pct=position_exposure_pct,
            max_concurrent=max_conc,
            state4_max_concurrent=s4_max_conc,
            max_per_cluster=max_cluster,
            max_total_pct=max_total_pct,
            state4_max_total_pct=s4_max_total_pct,
            size_pct=sz,
            cluster_key=ckey,
            is_state4=is_s4,
        )
        if not reserved.ok:
            dropped[reserved.reason or "reservation_rejected"] += 1
            continue
        sig["reservation_state"] = "reserved"
        sig["reservation_key"] = reserved.signal_key
        sig["reservation_idempotent"] = reserved.idempotent
        if not reserved.idempotent:
            held_reservations.append(
                {
                    "signal_key": reserved.signal_key,
                    "symbol": sym,
                    "cluster_key": ckey,
                    "size_pct": sz,
                    "regime": regime,
                }
            )
        if reserved.idempotent:
            accepted_ids.add(id(sig))
            continue

        accepted_ids.add(id(sig))
        if sym:
            open_symbols.add(sym)
        if ckey:
            cluster_counts[ckey] += 1
        cur_count += 1
        cur_exposure_pct += sz
        cur_beta += cand_beta
        if notional_usdt is not None:
            cur_new_notional += notional_usdt
        if is_s4:
            s4_new_in_tick += 1

    kept = [
        s for s in signals
        if str(s.get("action") or "").lower() != "buy" or id(s) in accepted_ids
    ]
    return kept, {
        "applied": True,
        "equity_source": eq_view.source,
        "live_equity_usdt": round(eq_view.equity_usdt, 4),
        "usdt_free": round(eq_view.usdt_free, 4),
        "open_positions_count": len(open_positions),
        "reservation_release_interface": RELEASE_INTERFACE_BLOCK,
        "open_exposure_pct": round(open_exposure_pct, 6),
        "final_exposure_pct": round(cur_exposure_pct, 6),
        "final_portfolio_beta": round(cur_beta, 6),
        "buys_in": len(buys),
        "buys_kept": len(accepted_ids),
        "dropped": dict(sorted(dropped.items())),
    }
