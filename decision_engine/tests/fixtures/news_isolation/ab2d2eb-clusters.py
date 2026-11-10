"""P2 Portfolio Risk-Parity Sizing & Cross-Asset Beta Cluster Exposure Controls.

Provides two institutional portfolio construction layers for the live Binance
spot arms (``live_bnb_boll_fade`` and ``live_bnb_boll_classic``):

1. **Inverse-Stop-Width / Volatility-Targeted Risk-Parity Sizer**:
   Scales ``size_pct`` inversely with the trade's relative stop distance
   ``(entry_price - stop_price) / entry_price`` and 24h realized volatility
   ``volatility_24h`` so that a wide-stop, high-volatility altcoin does not
   consume 5x–8x the dollar stop-loss risk of a tight-stop major.
   - Diagnostic fields (``stop_distance_pct``, ``risk_parity_scalar``,
     ``sector_cluster``) are always stamped onto live signals.
   - Active size scaling is enabled via ``LIVE_BNB_INVERSE_VOL_SIZING=1``.

2. **Cross-Asset Sector / Beta Cluster Exposure Cap**:
   Maps Binance spot USDT symbols into correlated beta clusters (``major_l1``,
   ``ai_compute``, ``meme_high_beta``, ``defi_infra``, ``l2_scaling``,
   ``gaming_metaverse``, ``legacy_pow_privacy``) and prevents:
   - Selecting multiple signals from the same correlated cluster in a single
     tick inside ``cap_live_bnb_buys`` (``reason="cluster_tick_cap_exceeded"``).
   - Opening a new position when the live book already holds the maximum
     allowed open positions in that same cluster inside ``apply_live_buy_guards``
     (``reason="cluster_open_exposure_cap:<cluster>"``).
   - Enabled via ``LIVE_BNB_CLUSTER_CAP_ENABLED=1`` (with configurable
     ``LIVE_BNB_MAX_PER_CLUSTER_PER_TICK`` default 1 and
     ``LIVE_BNB_MAX_OPEN_PER_CLUSTER`` default 1).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Sequence

from decision_engine.core.bnb_spot_map import classify_listed_symbol

ENV_INVERSE_VOL_SIZING = "LIVE_BNB_INVERSE_VOL_SIZING"
ENV_TARGET_STOP_PCT = "LIVE_BNB_TARGET_STOP_PCT"
ENV_TARGET_VOL_24H = "LIVE_BNB_TARGET_VOL_24H"

ENV_CLUSTER_CAP_ENABLED = "LIVE_BNB_CLUSTER_CAP_ENABLED"
ENV_MAX_PER_CLUSTER_TICK = "LIVE_BNB_MAX_PER_CLUSTER_PER_TICK"
ENV_MAX_OPEN_PER_CLUSTER = "LIVE_BNB_MAX_OPEN_PER_CLUSTER"

DEFAULT_TARGET_STOP_PCT = 0.015  # 1.5% stop distance = 1.0x risk-parity anchor
DEFAULT_TARGET_VOL_24H = 1.5
MIN_STOP_DIST_FLOOR = 0.002  # 20 bps minimum denominator
MIN_RISK_PARITY_SCALAR = 0.25
MAX_RISK_PARITY_SCALAR = 1.25

_TRUE = frozenset({"1", "true", "yes", "on"})
_OPEN_STATUSES = frozenset({"open", "oco_working", "exit_submitted"})

SECTOR_CLUSTERS: dict[str, frozenset[str]] = {
    "major_l1": frozenset(
        {
            "BTC", "ETH", "BNB", "SOL", "ADA", "AVAX", "DOT", "TRX", "TON",
            "NEAR", "APT", "SUI", "SEI", "ATOM", "ICP", "ALGO", "HBAR",
            "ETC", "LTC", "BCH", "XRP", "XLM",
        }
    ),
    "ai_compute": frozenset(
        {
            "FET", "RENDER", "RNDR", "TAO", "WLD", "ARKM", "IO", "AKT",
            "THETA", "GRT", "GLM", "NMR", "PHB", "AI", "NFP", "AGIX", "OCEAN",
        }
    ),
    "meme_high_beta": frozenset(
        {
            "DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "BOME", "MEME",
            "PEOPLE", "NEIRO", "TURBO", "1000SATS", "ORDI", "DOGS", "CATI",
            "PNUT", "ACT", "MOG", "POPCAT", "BRETT",
        }
    ),
    "defi_infra": frozenset(
        {
            "UNI", "AAVE", "MKR", "LDO", "PENDLE", "ENA", "ONDO", "LINK",
            "PYTH", "JUP", "RAY", "JTO", "CRV", "COMP", "SNX", "DYDX",
            "GMX", "CAKE", "1INCH", "RUNE", "INJ",
        }
    ),
    "l2_scaling": frozenset(
        {
            "ARB", "OP", "POL", "MATIC", "STRK", "ZK", "IMX", "MANTA",
            "METIS", "STX", "TIA", "DYM", "ALT", "SAGA",
        }
    ),
    "gaming_metaverse": frozenset(
        {
            "AXS", "SAND", "MANA", "GALA", "BEAMX", "PIXEL", "PORTAL",
            "YGG", "APE", "ENJ", "RONIN", "NOT", "HMSTR",
        }
    ),
    "legacy_pow_privacy": frozenset(
        {
            "DASH", "ZEC", "XMR", "ZEN", "DCR", "RVN", "KAS", "BSV",
        }
    ),
}

_BASE_TO_CLUSTER: dict[str, str] = {
    base: cluster
    for cluster, bases in SECTOR_CLUSTERS.items()
    for base in bases
}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def inverse_vol_sizing_enabled() -> bool:
    return os.environ.get(ENV_INVERSE_VOL_SIZING, "").strip().lower() in _TRUE


def cluster_cap_enabled() -> bool:
    return os.environ.get(ENV_CLUSTER_CAP_ENABLED, "").strip().lower() in _TRUE


def target_stop_pct() -> float:
    val = _finite(os.environ.get(ENV_TARGET_STOP_PCT))
    if val is None or val <= 0.0 or val >= 0.5:
        return DEFAULT_TARGET_STOP_PCT
    return val


def target_vol_24h() -> float:
    val = _finite(os.environ.get(ENV_TARGET_VOL_24H))
    if val is None or val <= 0.0:
        return DEFAULT_TARGET_VOL_24H
    return val


def max_per_cluster_per_tick() -> int:
    raw = os.environ.get(ENV_MAX_PER_CLUSTER_TICK, "").strip()
    if not raw:
        return 1
    try:
        val = int(raw)
    except ValueError:
        return 1
    return max(1, val)


def max_open_per_cluster() -> int:
    raw = os.environ.get(ENV_MAX_OPEN_PER_CLUSTER, "").strip()
    if not raw:
        return 1
    try:
        val = int(raw)
    except ValueError:
        return 1
    return max(1, val)


def base_asset_of(symbol: str | None) -> str | None:
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    spot = classify_listed_symbol(symbol.strip())
    if spot is not None and spot.endswith("USDT") and len(spot) > 4:
        return spot[:-4]
    text = symbol.strip().upper()
    if text.endswith("USDT") and len(text) > 4:
        return text[:-4]
    return text or None


def symbol_sector_cluster(symbol: str | None) -> str:
    """Return canonical sector cluster label, or ``'idiosyncratic'``."""
    base = base_asset_of(symbol)
    if base is None:
        return "idiosyncratic"
    return _BASE_TO_CLUSTER.get(base, "idiosyncratic")


def cluster_exposure_key(symbol: str | None) -> str | None:
    """Return deduplication key for correlated clusters, or ``idiosyncratic:<BASE>``."""
    base = base_asset_of(symbol)
    if base is None:
        return None
    cluster = _BASE_TO_CLUSTER.get(base)
    if cluster is not None:
        return cluster
    return f"idiosyncratic:{base}"


@dataclass(frozen=True)
class VolatilitySizeResult:
    size_pct: float
    effective_multiplier: float
    risk_parity_scalar: float
    stop_distance_pct: float | None
    applied: bool
    position_size_note: str | None


def compute_inverse_vol_size(
    planned_size_pct: float,
    *,
    tier_multiplier: float,
    entry_price: float | None,
    stop_price: float | None,
    volatility_24h: float | None = None,
    apply_scaling: bool | None = None,
) -> VolatilitySizeResult:
    """Compute inverse-stop-width and 24h-volatility risk-parity scalar.

    When ``apply_scaling`` is True (or ``LIVE_BNB_INVERSE_VOL_SIZING=1``),
    ``size_pct`` is multiplied by ``risk_parity_scalar``. When False, the
    scalar and ``stop_distance_pct`` are returned for diagnostic stamping
    without altering ``size_pct``.
    """
    enabled = inverse_vol_sizing_enabled() if apply_scaling is None else bool(apply_scaling)
    base_size = float(planned_size_pct) * float(tier_multiplier)
    entry = _finite(entry_price)
    stop = _finite(stop_price)
    vol = _finite(volatility_24h)

    stop_dist_pct: float | None = None
    stop_scalar = 1.0
    if entry is not None and stop is not None and entry > 0.0 and 0.0 < stop < entry:
        stop_dist_pct = (entry - stop) / entry
        tgt_stop = target_stop_pct()
        stop_scalar = tgt_stop / max(stop_dist_pct, MIN_STOP_DIST_FLOOR)

    raw_scalar = stop_scalar
    if vol is not None and vol > 0.0:
        tgt_vol = target_vol_24h()
        if vol > tgt_vol:
            vol_scalar = math.sqrt(tgt_vol / vol)
            raw_scalar = min(stop_scalar, vol_scalar)

    scalar = min(MAX_RISK_PARITY_SCALAR, max(MIN_RISK_PARITY_SCALAR, raw_scalar))

    if not enabled:
        return VolatilitySizeResult(
            size_pct=base_size,
            effective_multiplier=float(tier_multiplier),
            risk_parity_scalar=round(scalar, 6),
            stop_distance_pct=round(stop_dist_pct, 6) if stop_dist_pct is not None else None,
            applied=False,
            position_size_note=None,
        )

    eff_mult = float(tier_multiplier) * scalar
    scaled_size = min(1.0, float(planned_size_pct) * eff_mult)
    return VolatilitySizeResult(
        size_pct=scaled_size,
        effective_multiplier=round(eff_mult, 6),
        risk_parity_scalar=round(scalar, 6),
        stop_distance_pct=round(stop_dist_pct, 6) if stop_dist_pct is not None else None,
        applied=True,
        position_size_note="inverse_stop_width_risk_parity",
    )


def cluster_open_exposure_block(
    positions: Sequence[Any],
    *,
    purpose: str,
    symbol: str | None,
    max_open: int | None = None,
    enabled: bool | None = None,
) -> str | None:
    """Return block reason if the open book already holds ``max_open`` symbols in the same beta cluster."""
    is_on = cluster_cap_enabled() if enabled is None else bool(enabled)
    if not is_on:
        return None
    cluster = symbol_sector_cluster(symbol)
    if cluster == "idiosyncratic":
        return None
    cap = max_open_per_cluster() if max_open is None else max(1, int(max_open))
    buy_base = base_asset_of(symbol)
    open_in_cluster = 0
    for row in positions:
        if not isinstance(row, dict):
            continue
        row_purpose = str(row.get("purpose") or "").strip()
        if row_purpose and row_purpose != purpose:
            continue
        status = str(row.get("status") or "").strip()
        if status not in _OPEN_STATUSES:
            continue
        held_sym = str(row.get("symbol") or "").strip()
        held_base = base_asset_of(held_sym)
        if held_base is None or held_base == buy_base:
            continue
        if symbol_sector_cluster(held_sym) == cluster:
            open_in_cluster += 1
    if open_in_cluster >= cap:
        return f"cluster_open_exposure_cap:{cluster}"
    return None
