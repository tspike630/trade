r"""Multi-Horizon 1h/4h Trend & Volatility Regime Filter + Cross-Sectional Relative Strength Ranker.

Addresses two core institutional statistical-arbitrage requirements for short-horizon
mean-reversion (``live_bnb_boll_fade``):

1. **Multi-Horizon Trend & Volatility Regime Filter**:
   - **1h / 4h Waterfall Cascade Gate**: Blocks 5m dip-buying when the higher timeframe
     is in a structural waterfall crash (``change_1h <= -LIVE_BNB_MAX_DROP_1H_PCT``
     default ``-8.0%``, or ``change_4h <= -LIVE_BNB_MAX_DROP_4H_PCT`` default ``-14.0%``).
   - **Moderate HTF Downtrend Downgrade**: When ``change_1h <= -LIVE_BNB_DOWNGRADE_DROP_1H_PCT``
     (default ``-5.0%``), downgrades ``large_trade`` ``1.0x`` market orders to defensive
     ``0.5x`` ``neutral`` rules (``horizon_regime="htf_downtrend_downgraded"``).
   - **Bollinger Bandwidth Compression Filter**: When ``(boll_upper - boll_lower) / boll_mid``
     is below ``LIVE_BNB_MIN_BOLL_BANDWIDTH_PCT`` (default ``0.006`` = 60 bps), skips the
     entry because mean-reversion room to the midline cannot overcome spread/fee friction.

2. **Cross-Sectional Market Breadth & Relative Strength Ranker**:
   - Computes universe-wide median returns (``median_change_5m``, ``median_change_1h``,
     ``median_change_4h``) and 1h advancing breadth (``breadth_1h_positive_ratio``).
   - **Systemic Crash Breadth Circuit Breaker**: When ``>= 5`` symbols are scanned,
     ``breadth_1h_positive_ratio < LIVE_BNB_MIN_MARKET_BREADTH_1H`` (default ``0.12``),
     and ``median_change_1h <= -3.5%``, blocks dip-buying into a systemic market-wide
     liquidation cascade (``xs_systemic_crash_breadth``).
   - **Extreme Idiosyncratic Laggard Filter**: Rejects names bleeding ``<= -6.0%`` worse
     than the universe 1h median (``xs_extreme_laggard_bleed``).
   - **Composite Multi-Factor Alpha Score (``composite_alpha_score``)**: Combines
     reward-to-risk odds, taker-buy absorption, block trade-size ratio, and 1h
     cross-sectional relative resilience into a ``[0, 100]`` score for top-buy ranking.
"""

from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

MULTI_HORIZON_REGIME_ENV = "LIVE_BNB_MULTI_HORIZON_REGIME"
MAX_DROP_1H_PCT_ENV = "LIVE_BNB_MAX_DROP_1H_PCT"
MAX_DROP_4H_PCT_ENV = "LIVE_BNB_MAX_DROP_4H_PCT"
DOWNGRADE_DROP_1H_PCT_ENV = "LIVE_BNB_DOWNGRADE_DROP_1H_PCT"
MIN_BOLL_BANDWIDTH_PCT_ENV = "LIVE_BNB_MIN_BOLL_BANDWIDTH_PCT"
MIN_MARKET_BREADTH_1H_ENV = "LIVE_BNB_MIN_MARKET_BREADTH_1H"
MAX_XS_LAGGARD_1H_PCT_ENV = "LIVE_BNB_MAX_XS_LAGGARD_1H_PCT"

DEFAULT_MAX_DROP_1H_PCT = 8.0
DEFAULT_MAX_DROP_4H_PCT = 14.0
DEFAULT_DOWNGRADE_DROP_1H_PCT = 5.0
DEFAULT_MIN_BOLL_BANDWIDTH_PCT = 0.006
DEFAULT_MIN_MARKET_BREADTH_1H = 0.12
DEFAULT_SYSTEMIC_MEDIAN_1H_PCT = -3.5
DEFAULT_MAX_XS_LAGGARD_1H_PCT = 6.0

_TRUE = frozenset({"1", "true", "yes", "on"})


def multi_horizon_regime_enabled() -> bool:
    raw = os.environ.get(MULTI_HORIZON_REGIME_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE


def _finite(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _env_float(name: str, default: float) -> float:
    val = _finite(os.environ.get(name))
    if val is None or val <= 0:
        return default
    return val


@dataclass(frozen=True)
class CrossSectionalContext:
    universe_count: int
    median_change_5m: float | None
    median_change_1h: float | None
    median_change_4h: float | None
    breadth_1h_positive_ratio: float | None
    btc_change_1h: float | None
    eth_change_1h: float | None
    is_systemic_crash: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_count": self.universe_count,
            "median_change_5m": None if self.median_change_5m is None else round(self.median_change_5m, 4),
            "median_change_1h": None if self.median_change_1h is None else round(self.median_change_1h, 4),
            "median_change_4h": None if self.median_change_4h is None else round(self.median_change_4h, 4),
            "breadth_1h_positive_ratio": (
                None if self.breadth_1h_positive_ratio is None else round(self.breadth_1h_positive_ratio, 4)
            ),
            "btc_change_1h": None if self.btc_change_1h is None else round(self.btc_change_1h, 4),
            "eth_change_1h": None if self.eth_change_1h is None else round(self.eth_change_1h, 4),
            "is_systemic_crash": self.is_systemic_crash,
        }


@dataclass(frozen=True)
class MultiHorizonVerdict:
    allow: bool
    reject_reason: str | None
    downgrade_to_neutral: bool
    horizon_regime: str
    boll_bandwidth_pct: float | None
    change_1h: float | None
    change_4h: float | None
    xs_rel_1h: float | None


def compute_boll_bandwidth_pct(row: dict[str, Any]) -> float | None:
    """Compute ``(boll_upper - boll_lower) / boll_mid`` when all three are positive."""
    upper = _finite(row.get("boll_upper"))
    mid = _finite(row.get("boll_mid"))
    lower = _finite(row.get("boll_lower", row.get("boll_low")))
    if upper is None or mid is None or lower is None:
        return None
    if mid <= 0 or upper < lower:
        return None
    return (upper - lower) / mid


def compute_cross_sectional_context(
    rows: Sequence[dict[str, Any]],
    *,
    min_breadth_1h: float | None = None,
    systemic_median_1h_pct: float = DEFAULT_SYSTEMIC_MEDIAN_1H_PCT,
) -> CrossSectionalContext:
    """Summarize cross-sectional return distribution and market breadth across ``rows``."""
    c5_list: list[float] = []
    c1h_list: list[float] = []
    c4h_list: list[float] = []
    btc_1h: float | None = None
    eth_1h: float | None = None

    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("binance_symbol") or row.get("token_address") or row.get("symbol") or "").strip().upper()
        c5 = _finite(row.get("change_5m"))
        c1h = _finite(row.get("change_1h"))
        c4h = _finite(row.get("change_4h"))
        if c5 is not None:
            c5_list.append(c5)
        if c1h is not None:
            c1h_list.append(c1h)
            if sym in {"BTCUSDT", "BTC"}:
                btc_1h = c1h
            elif sym in {"ETHUSDT", "ETH"}:
                eth_1h = c1h
        if c4h is not None:
            c4h_list.append(c4h)

    med_5m = statistics.median(c5_list) if c5_list else None
    med_1h = statistics.median(c1h_list) if c1h_list else None
    med_4h = statistics.median(c4h_list) if c4h_list else None

    breadth_1h: float | None = None
    if c1h_list:
        advancing = sum(1 for val in c1h_list if val >= 0.0)
        breadth_1h = advancing / len(c1h_list)

    breadth_floor = (
        float(min_breadth_1h)
        if min_breadth_1h is not None
        else _env_float(MIN_MARKET_BREADTH_1H_ENV, DEFAULT_MIN_MARKET_BREADTH_1H)
    )
    is_crash = (
        len(c1h_list) >= 5
        and breadth_1h is not None
        and breadth_1h < breadth_floor
        and med_1h is not None
        and med_1h <= systemic_median_1h_pct
    )
    return CrossSectionalContext(
        universe_count=max(len(c1h_list), len(c5_list)),
        median_change_5m=med_5m,
        median_change_1h=med_1h,
        median_change_4h=med_4h,
        breadth_1h_positive_ratio=breadth_1h,
        btc_change_1h=btc_1h,
        eth_change_1h=eth_1h,
        is_systemic_crash=is_crash,
    )


def stamp_cross_sectional_metrics(
    rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], CrossSectionalContext]:
    """Return a copy of ``rows`` stamped with cross-sectional relative strength & breadth."""
    ctx = compute_cross_sectional_context(rows)
    c1h_sorted = sorted(
        val
        for r in rows
        if isinstance(r, dict)
        for val in (_finite(r.get("change_1h")),)
        if val is not None
    )
    n_1h = len(c1h_sorted)
    stamped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        c1h = _finite(item.get("change_1h"))
        c4h = _finite(item.get("change_4h"))
        if c1h is not None and ctx.median_change_1h is not None:
            item["xs_rel_1h"] = round(c1h - ctx.median_change_1h, 6)
        if c4h is not None and ctx.median_change_4h is not None:
            item["xs_rel_4h"] = round(c4h - ctx.median_change_4h, 6)
        if c1h is not None and n_1h > 1:
            less_eq = sum(1 for v in c1h_sorted if v <= c1h)
            item["xs_rank_pct_1h"] = round((less_eq - 1) / (n_1h - 1), 4)
        item["xs_systemic_crash"] = ctx.is_systemic_crash
        if ctx.breadth_1h_positive_ratio is not None:
            item["xs_breadth_1h"] = round(ctx.breadth_1h_positive_ratio, 4)
        stamped.append(item)
    return stamped, ctx


def evaluate_multi_horizon_regime(
    row: dict[str, Any],
    *,
    enforce: bool | None = None,
) -> MultiHorizonVerdict:
    """Evaluate 1h/4h waterfall gates, Bollinger bandwidth compression, and XS laggard bleed."""
    active = multi_horizon_regime_enabled() if enforce is None else bool(enforce)
    bw_pct = compute_boll_bandwidth_pct(row)
    c1h = _finite(row.get("change_1h"))
    c4h = _finite(row.get("change_4h"))
    xs_rel_1h = _finite(row.get("xs_rel_1h"))

    if not active:
        return MultiHorizonVerdict(
            allow=True,
            reject_reason=None,
            downgrade_to_neutral=False,
            horizon_regime="disabled",
            boll_bandwidth_pct=bw_pct,
            change_1h=c1h,
            change_4h=c4h,
            xs_rel_1h=xs_rel_1h,
        )

    if row.get("xs_systemic_crash") is True:
        return MultiHorizonVerdict(
            allow=False,
            reject_reason="xs_systemic_crash_breadth",
            downgrade_to_neutral=False,
            horizon_regime="systemic_crash_halt",
            boll_bandwidth_pct=bw_pct,
            change_1h=c1h,
            change_4h=c4h,
            xs_rel_1h=xs_rel_1h,
        )

    min_bw = _env_float(MIN_BOLL_BANDWIDTH_PCT_ENV, DEFAULT_MIN_BOLL_BANDWIDTH_PCT)
    if bw_pct is not None and bw_pct < min_bw:
        return MultiHorizonVerdict(
            allow=False,
            reject_reason="volatility_compression_skip",
            downgrade_to_neutral=False,
            horizon_regime="vol_compressed",
            boll_bandwidth_pct=bw_pct,
            change_1h=c1h,
            change_4h=c4h,
            xs_rel_1h=xs_rel_1h,
        )

    max_drop_1h = _env_float(MAX_DROP_1H_PCT_ENV, DEFAULT_MAX_DROP_1H_PCT)
    if c1h is not None and c1h <= -max_drop_1h:
        return MultiHorizonVerdict(
            allow=False,
            reject_reason="multi_horizon_1h_waterfall",
            downgrade_to_neutral=False,
            horizon_regime="waterfall_1h",
            boll_bandwidth_pct=bw_pct,
            change_1h=c1h,
            change_4h=c4h,
            xs_rel_1h=xs_rel_1h,
        )

    max_drop_4h = _env_float(MAX_DROP_4H_PCT_ENV, DEFAULT_MAX_DROP_4H_PCT)
    if c4h is not None and c4h <= -max_drop_4h:
        return MultiHorizonVerdict(
            allow=False,
            reject_reason="multi_horizon_4h_waterfall",
            downgrade_to_neutral=False,
            horizon_regime="waterfall_4h",
            boll_bandwidth_pct=bw_pct,
            change_1h=c1h,
            change_4h=c4h,
            xs_rel_1h=xs_rel_1h,
        )

    max_xs_laggard = _env_float(MAX_XS_LAGGARD_1H_PCT_ENV, DEFAULT_MAX_XS_LAGGARD_1H_PCT)
    if xs_rel_1h is not None and xs_rel_1h <= -max_xs_laggard:
        return MultiHorizonVerdict(
            allow=False,
            reject_reason="xs_extreme_laggard_bleed",
            downgrade_to_neutral=False,
            horizon_regime="xs_laggard_bleed",
            boll_bandwidth_pct=bw_pct,
            change_1h=c1h,
            change_4h=c4h,
            xs_rel_1h=xs_rel_1h,
        )

    downgrade_1h = _env_float(DOWNGRADE_DROP_1H_PCT_ENV, DEFAULT_DOWNGRADE_DROP_1H_PCT)
    if c1h is not None and c1h <= -downgrade_1h:
        return MultiHorizonVerdict(
            allow=True,
            reject_reason=None,
            downgrade_to_neutral=True,
            horizon_regime="htf_downtrend_downgraded",
            boll_bandwidth_pct=bw_pct,
            change_1h=c1h,
            change_4h=c4h,
            xs_rel_1h=xs_rel_1h,
        )

    regime = "htf_aligned" if (c1h is not None and c1h >= -2.0) else "htf_neutral"
    return MultiHorizonVerdict(
        allow=True,
        reject_reason=None,
        downgrade_to_neutral=False,
        horizon_regime=regime,
        boll_bandwidth_pct=bw_pct,
        change_1h=c1h,
        change_4h=c4h,
        xs_rel_1h=xs_rel_1h,
    )


def compute_composite_alpha_score(
    row: dict[str, Any],
    *,
    odds: float | None = None,
    trade_size_ratio: float | None = None,
    taker_buy_ratio: float | None = None,
) -> float:
    """Compute a ``[0.0, 100.0]`` multi-factor alpha score for cross-sectional ranking.

    Weights:
      - Reward-to-risk odds (up to 30 pts)
      - Taker-buy orderflow absorption (up to 25 pts)
      - Block trade-size ratio (up to 20 pts)
      - Higher-timeframe & cross-sectional relative resilience (up to 25 pts)
    """
    # 1. Odds component (0..30)
    odds_val = _finite(odds if odds is not None else row.get("reward_risk_odds"))
    if odds_val is None or odds_val <= 1.0:
        odds_pts = 10.0
    else:
        odds_pts = min(30.0, max(0.0, (odds_val - 1.0) / 3.0 * 30.0))

    # 2. Orderflow absorption component (0..25)
    tbr_val = _finite(taker_buy_ratio if taker_buy_ratio is not None else row.get("taker_buy_ratio"))
    if tbr_val is None:
        tbr_pts = 12.5
    else:
        tbr_pts = min(25.0, max(0.0, (tbr_val - 0.30) / 0.40 * 25.0))

    # 3. Trade size ratio component (0..20)
    tsr_val = _finite(trade_size_ratio if trade_size_ratio is not None else row.get("trade_size_ratio"))
    if tsr_val is None:
        tsr_pts = 10.0
    else:
        tsr_pts = min(20.0, max(0.0, (tsr_val - 0.85) / 1.50 * 20.0))

    # 4. Cross-sectional & multi-horizon resilience component (0..25)
    xs_rel = _finite(row.get("xs_rel_1h"))
    c1h = _finite(row.get("change_1h"))
    ref_rel = xs_rel if xs_rel is not None else c1h
    if ref_rel is None:
        htf_pts = 12.5
    else:
        # Map [-6%, +4%] to [0, 25]
        htf_pts = min(25.0, max(0.0, (ref_rel + 6.0) / 10.0 * 25.0))

    return round(odds_pts + tbr_pts + tsr_pts + htf_pts, 4)
