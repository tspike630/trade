"""Entry zone for purpose ``live_bnb_boll_fade`` only.

Replaces the paper legs (5m change <= -2, volume_strength >= 0.5, close below
``boll_lower``) on that purpose. Those three stay diagnostic extras. They do
not admit or block a live entry. ``race_tech_boll_fade`` does not call this
module.

Coordinates of the latest **closed** 5m bar:

- ``L`` low, ``M`` boll_mid at that same bar, ``P`` close
- ``S = M - L``
- ``alpha = (P - L) / S``

Only ``S > 0`` and a finite alpha are considered. ``trade_size_ratio`` is the
current bar's average quote notional per trade divided by the prior 24h
average (sum of quote volume / sum of trades). It is an observable size
ratio, not a dealer or retail identity.

#164 (open, not on main) stores ``avg_trade_size`` for the current bar and
does **not** store ``trade_size_ratio``. This module prefers a finite
``trade_size_ratio`` already on the row, then ``avg_trade_size`` over a 24h
baseline, then ``quote_volume / trades`` from the closed bars. A missing
ratio or a missing coordinate fails closed. It does not fall back to the
paper legs.

Tiers (``ratio >= 1.3`` large, ``0.85 <= ratio < 1.3`` neutral, else crowded):

1. large_trade: alpha in [0.08, 0.40] and odds ``(M-P)/(P-stop) >= 1.5``.
   Market intent at ``P``. Planned size is the existing sizer output
   (multiplier 1). Stop reference ``L - 0.1*S``. Take-profit is ``M``.
2. neutral: alpha in [0.12, 0.28] and odds >= 2.5 → market. If ``P`` is too
   high to qualify, a limit at ``L + 0.20*S`` only when that limit rests
   strictly below ``P`` and its own odds clear 2.5. Otherwise no chase.
   Size is half the planned single-name size. The risk shell has no
   inverse-stop-width sizer; this path does not add one and does not change
   other purposes. The live emitter may raise the emitted ``size_pct`` after
   this decision so the quote clears the 20 USDT exchange minimum. That lift
   does not change this multiplier or these bands.
3. retail_crowded: no market buy. This round does not rest the deep bid at
   ``L - 0.5*S``. That price is recorded for audit only.

Orderflow Toxicity Overlay (``taker_buy_ratio = taker_buy_quote_volume / quote_volume``):

- When ``taker_buy_ratio`` is present (or derived from bar volumes):
  - ``taker_buy_ratio < MIN_TAKER_BUY_RATIO`` (0.30): one-sided aggressive
    market sell dump (> 70% taker sell volume) → rejected with
    ``reject_reason="orderflow_toxic_sell_dump"`` (``orderflow_regime="toxic_sell_dump"``).
  - ``0.30 <= taker_buy_ratio < LARGE_MIN_TAKER_BUY_RATIO`` (0.42) on a
    ``large_trade`` bar: large trade size is still dominated by aggressive
    selling rather than active buyer absorption → downgraded from ``1.0x``
    ``large_trade`` market to ``0.5x`` ``neutral`` rules
    (``orderflow_regime="absorption_weak_downgraded"``).
  - ``taker_buy_ratio >= 0.42``: active buyer absorption confirmed
    (``orderflow_regime="absorption_strong"``).

No fee or tick haircut is applied to 0.08 / 0.40 / 0.12 / 0.28 / 1.5 / 2.5.
Comparisons use a 1e-9 absolute tolerance so a boundary built as
``L + alpha*S`` is not rejected by binary noise. The tolerance is not a
wider band.

Stop ``>=`` the entry price makes odds undefined (``stop_not_below_price``).
Odds are a ratio, so a tight band can still clear them while the stop sits
a few basis points under the entry. A BUY is not emitted unless the stop is
at least ``MIN_ENTRY_STOP_GAP`` under the intended entry (signal price for
MARKET, limit for LIMIT): ``stop_too_close_to_entry``. ``P`` below the tier
floor or at/above ``M`` does not get a market buy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from decision_engine.core.multi_horizon_and_xs_regime import (
    compute_composite_alpha_score,
    evaluate_multi_horizon_regime,
)
from decision_engine.core.perp_microstructure_and_beta_hedge import (
    evaluate_perp_microstructure,
)
from decision_engine.core.stop_gap import stop_distance_reason

ALPHA_FLOOR = 0.08
ALPHA_CAP = 0.40
NEUTRAL_ALPHA_MIN = 0.12
NEUTRAL_ALPHA_MAX = 0.28
NEUTRAL_LIMIT_ALPHA = 0.20
LARGE_MIN_RATIO = 1.3
NEUTRAL_MIN_RATIO = 0.85
LARGE_MIN_ODDS = 1.5
NEUTRAL_MIN_ODDS = 2.5
MIN_TAKER_BUY_RATIO = 0.30
LARGE_MIN_TAKER_BUY_RATIO = 0.42
STOP_SPAN_FRAC = 0.1
DEEP_SPAN_FRAC = 0.5
_CMP_EPS = 1e-9
_DAY_MS = 24 * 60 * 60 * 1000
_TRUE = frozenset({"1", "true", "yes", "on"})

Tier = Literal["large_trade", "neutral", "retail_crowded"]
OrderType = Literal["MARKET", "LIMIT", "LIMIT_MAKER"]

RULE_VERSION = "live_bnb_entry_zone_v2_2026-11-06"


def require_taker_buy_ratio_enabled() -> bool:
    """When ``LIVE_BNB_REQUIRE_TAKER_BUY_RATIO=1``, missing taker_buy_ratio fails closed."""
    return os.environ.get("LIVE_BNB_REQUIRE_TAKER_BUY_RATIO", "").strip().lower() in _TRUE


def post_only_limit_enabled() -> bool:
    """When ``LIVE_BNB_POST_ONLY_LIMIT=1``, neutral limit orders emit as ``LIMIT_MAKER``."""
    return os.environ.get("LIVE_BNB_POST_ONLY_LIMIT", "").strip().lower() in _TRUE


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in row:
            continue
        number = _finite(row.get(key))
        if number is not None:
            return number
    return None


def _gte(value: float, bound: float) -> bool:
    return value >= bound - _CMP_EPS


def _lte(value: float, bound: float) -> bool:
    return value <= bound + _CMP_EPS


def _gt(value: float, bound: float) -> bool:
    return value > bound + _CMP_EPS


@dataclass(frozen=True)
class ZoneBar:
    """One closed 5m bar. ``trades`` is None when the column is absent."""

    open_ms: int
    close_ms: int
    low: float
    close: float
    quote_volume: float | None
    trades: float | None
    volume: float | None = None
    taker_buy_base_volume: float | None = None
    taker_buy_quote_volume: float | None = None


@dataclass(frozen=True)
class EntryDecision:
    emit: bool
    reject_reason: str | None
    entry_reason: str | None
    tier: str | None
    order_type: str | None
    limit_price: float | None
    stop_price: float | None
    take_profit_price: float | None
    entry_zone_min: float | None
    entry_zone_max: float | None
    alpha: float | None
    span: float | None
    low: float | None
    mid: float | None
    price: float | None
    trade_size_ratio: float | None
    trade_size_ratio_source: str | None
    trade_size_baseline_bars: int | None
    position_size_multiplier: float | None
    position_size_note: str | None
    deep_zone_price: float | None
    legacy_change_5m_pass: bool | None
    legacy_volume_strength_pass: bool | None
    legacy_boll_lower_break: bool | None
    taker_buy_ratio: float | None = None
    taker_buy_ratio_source: str | None = None
    orderflow_regime: str | None = None
    funding_rate: float | None = None
    perp_basis_bps: float | None = None
    perp_regime: str | None = None
    boll_bandwidth_pct: float | None = None
    xs_rel_1h: float | None = None
    horizon_regime: str | None = None
    composite_alpha_score: float | None = None


def _empty(
    *,
    reason: str,
    alpha: float | None = None,
    span: float | None = None,
    low: float | None = None,
    mid: float | None = None,
    price: float | None = None,
    ratio: float | None = None,
    ratio_source: str | None = None,
    baseline_bars: int | None = None,
    deep: float | None = None,
    legacy: tuple[bool | None, bool | None, bool | None] = (None, None, None),
    tier: str | None = None,
    zone: tuple[float | None, float | None] = (None, None),
    stop_price: float | None = None,
    take_profit_price: float | None = None,
    taker_buy_ratio: float | None = None,
    taker_buy_ratio_source: str | None = None,
    orderflow_regime: str | None = None,
    funding_rate: float | None = None,
    perp_basis_bps: float | None = None,
    perp_regime: str | None = None,
    boll_bandwidth_pct: float | None = None,
    xs_rel_1h: float | None = None,
    horizon_regime: str | None = None,
    composite_alpha_score: float | None = None,
) -> EntryDecision:
    return EntryDecision(
        emit=False,
        reject_reason=reason,
        entry_reason=None,
        tier=tier,
        order_type=None,
        limit_price=None,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        entry_zone_min=zone[0],
        entry_zone_max=zone[1],
        alpha=alpha,
        span=span,
        low=low,
        mid=mid,
        price=price,
        trade_size_ratio=ratio,
        trade_size_ratio_source=ratio_source,
        trade_size_baseline_bars=baseline_bars,
        position_size_multiplier=None,
        position_size_note=None,
        deep_zone_price=deep,
        legacy_change_5m_pass=legacy[0],
        legacy_volume_strength_pass=legacy[1],
        legacy_boll_lower_break=legacy[2],
        taker_buy_ratio=taker_buy_ratio,
        taker_buy_ratio_source=taker_buy_ratio_source,
        orderflow_regime=orderflow_regime,
        funding_rate=funding_rate,
        perp_basis_bps=perp_basis_bps,
        perp_regime=perp_regime,
        boll_bandwidth_pct=boll_bandwidth_pct,
        xs_rel_1h=xs_rel_1h,
        horizon_regime=horizon_regime,
        composite_alpha_score=composite_alpha_score,
    )


def reward_risk(mid: float, entry: float, stop: float) -> float | None:
    """``(M - entry) / (entry - stop)``. Undefined when the stop is not below entry."""
    risk = entry - stop
    reward = mid - entry
    if risk <= _CMP_EPS or reward <= _CMP_EPS:
        return None
    return reward / risk


def taker_buy_ratio_from_bar(bar: ZoneBar) -> tuple[float | None, str | None]:
    """Compute active taker-buy volume share in ``[0, 1]`` from a ``ZoneBar``."""
    if (
        bar.taker_buy_quote_volume is not None
        and bar.quote_volume is not None
        and bar.quote_volume > 0
    ):
        if bar.taker_buy_quote_volume < 0:
            return None, None
        ratio = bar.taker_buy_quote_volume / bar.quote_volume
        if ratio <= 1.0 + 1e-6:
            return min(1.0, max(0.0, ratio)), "kline_taker_buy_quote"
        return None, None
    if (
        bar.taker_buy_base_volume is not None
        and bar.volume is not None
        and bar.volume > 0
    ):
        if bar.taker_buy_base_volume < 0:
            return None, None
        ratio = bar.taker_buy_base_volume / bar.volume
        if ratio <= 1.0 + 1e-6:
            return min(1.0, max(0.0, ratio)), "kline_taker_buy_base"
        return None, None
    return None, None


def resolve_taker_buy_ratio(
    row: dict[str, Any],
) -> tuple[float | None, str | None, str | None]:
    """``(taker_buy_ratio, source, error)``.

    Direct ``taker_buy_ratio`` wins when present. Falls back to current-bar
    ``taker_buy_quote_volume / quote_volume``, then ``taker_buy_base_volume / volume``,
    then the last element of ``entry_zone_bars``. Out-of-range values (< 0 or > 1)
    return ``error="taker_buy_ratio_invalid"``.
    """
    if "taker_buy_ratio" in row and row.get("taker_buy_ratio") is not None:
        direct = _finite(row.get("taker_buy_ratio"))
        if direct is None or direct < 0.0 or direct > 1.0 + 1e-6:
            return None, None, "taker_buy_ratio_invalid"
        src = str(row.get("taker_buy_ratio_source") or "taker_buy_ratio")
        return min(1.0, max(0.0, direct)), src, None

    tb_quote = _first(row, ("taker_buy_quote_volume", "bar_taker_buy_quote_volume"))
    bar_quote = _first(row, ("bar_quote_volume", "quote_volume"))
    if tb_quote is not None:
        if tb_quote < 0.0:
            return None, None, "taker_buy_ratio_invalid"
        if bar_quote is not None and bar_quote > 0.0:
            ratio = tb_quote / bar_quote
            if ratio > 1.0 + 1e-6:
                return None, None, "taker_buy_ratio_invalid"
            return min(1.0, max(0.0, ratio)), "kline_taker_buy_quote", None

    tb_base = _first(row, ("taker_buy_base_volume", "bar_taker_buy_base_volume"))
    bar_vol = _first(row, ("bar_volume", "volume"))
    if tb_base is not None:
        if tb_base < 0.0:
            return None, None, "taker_buy_ratio_invalid"
        if bar_vol is not None and bar_vol > 0.0:
            ratio = tb_base / bar_vol
            if ratio > 1.0 + 1e-6:
                return None, None, "taker_buy_ratio_invalid"
            return min(1.0, max(0.0, ratio)), "kline_taker_buy_base", None

    bars = row.get("entry_zone_bars")
    if isinstance(bars, Sequence) and not isinstance(bars, (str, bytes)):
        parsed = [item for item in bars if isinstance(item, ZoneBar)]
        if parsed:
            tbr, src = taker_buy_ratio_from_bar(parsed[-1])
            if tbr is not None:
                return tbr, src, None
    return None, None, None


def _sum_quote_trades(bars: Sequence[ZoneBar]) -> tuple[float, float] | None:
    quote = 0.0
    trades = 0.0
    used = False
    for bar in bars:
        if bar.quote_volume is None or bar.trades is None:
            return None
        if bar.quote_volume < 0 or bar.trades < 0:
            return None
        quote += bar.quote_volume
        trades += bar.trades
        used = True
    if not used:
        return None
    return quote, trades


def trade_size_ratio_from_bars(
    bars: Sequence[ZoneBar],
) -> tuple[float | None, int]:
    """Current bar avg / prior-24h avg. Current bar is the last element.

    The baseline is every earlier bar whose close is still inside the prior
    24h (``close_ms >= current_close_ms - 24h``). The current bar is not in
    the denominator. A bar that closed exactly 24h before the current close
    is included. Returns ``(None, n)`` when the ratio cannot be formed.
    ``n`` is the baseline bar count, or 0 when there is no current bar.
    """
    if not bars:
        return None, 0
    current = bars[-1]
    if (
        current.quote_volume is None
        or current.trades is None
        or current.quote_volume < 0
        or current.trades <= 0
    ):
        return None, 0
    cutoff = current.close_ms - _DAY_MS
    baseline = [
        bar
        for bar in bars[:-1]
        if bar.open_ms < current.open_ms and bar.close_ms >= cutoff
    ]
    summed = _sum_quote_trades(baseline)
    if summed is None:
        return None, len(baseline)
    quote, trades = summed
    if trades <= 0 or quote < 0:
        return None, len(baseline)
    baseline_avg = quote / trades
    if baseline_avg <= 0:
        return None, len(baseline)
    current_avg = current.quote_volume / current.trades
    if current_avg < 0:
        return None, len(baseline)
    return current_avg / baseline_avg, len(baseline)


def resolve_trade_size_ratio(
    row: dict[str, Any],
) -> tuple[float | None, str | None, str | None]:
    """``(ratio, source, error)``.

    A present but negative ``trade_size_ratio`` is an error, not a cue to
    recompute. A missing column falls through to ``avg_trade_size`` and then
    to bar quote/trades. ``error`` is set only when a supplied value is illegal.
    """
    if "trade_size_ratio" in row and row.get("trade_size_ratio") is not None:
        direct = _finite(row.get("trade_size_ratio"))
        if direct is None or direct < 0:
            return None, None, "trade_size_ratio_invalid"
        return direct, "trade_size_ratio", None

    avg = _first(row, ("avg_trade_size",))
    avg_24h = _first(row, ("avg_trade_size_24h",))
    if avg is not None and avg < 0:
        return None, None, "trade_size_ratio_invalid"
    if avg is not None and avg_24h is not None:
        if avg_24h <= 0:
            return None, None, "trade_size_ratio_invalid"
        return avg / avg_24h, "avg_trade_size", None

    baseline_quote = _first(row, ("baseline_quote_volume",))
    baseline_trades = _first(row, ("baseline_trades",))
    baseline_avg: float | None = None
    if baseline_quote is not None and baseline_trades is not None and baseline_trades > 0:
        if baseline_quote < 0:
            return None, None, "trade_size_ratio_invalid"
        baseline_avg = baseline_quote / baseline_trades
    if avg is not None and baseline_avg is not None and baseline_avg > 0:
        return avg / baseline_avg, "avg_trade_size_over_kline_baseline", None

    bar_quote = _first(row, ("bar_quote_volume", "quote_volume"))
    bar_trades = _first(row, ("bar_trades", "trades"))
    if (
        bar_quote is not None
        and bar_trades is not None
        and bar_trades > 0
        and bar_quote >= 0
        and baseline_avg is not None
        and baseline_avg > 0
    ):
        return (bar_quote / bar_trades) / baseline_avg, "kline_quote_per_trade", None

    bars = row.get("entry_zone_bars")
    if isinstance(bars, Sequence) and not isinstance(bars, (str, bytes)):
        parsed: list[ZoneBar] = []
        for item in bars:
            if isinstance(item, ZoneBar):
                parsed.append(item)
        if parsed:
            ratio, _count = trade_size_ratio_from_bars(parsed)
            if ratio is not None:
                return ratio, "kline_quote_per_trade", None
    return None, None, None


def _legacy(
    row: dict[str, Any],
    price: float | None,
    *,
    min_change_5m: float,
    min_volume_strength: float,
) -> tuple[bool | None, bool | None, bool | None]:
    change = _finite(row.get("change_5m"))
    strength = _finite(row.get("volume_strength"))
    lower = _finite(row.get("boll_lower"))
    if lower is None:
        lower = _finite(row.get("boll_low"))
    change_pass = None if change is None else change <= -float(min_change_5m)
    strength_pass = None if strength is None else strength >= float(min_volume_strength)
    break_pass = None
    if price is not None and lower is not None:
        break_pass = price < lower
    return change_pass, strength_pass, break_pass


def _tier(ratio: float) -> Tier:
    if _gte(ratio, LARGE_MIN_RATIO):
        return "large_trade"
    if _gte(ratio, NEUTRAL_MIN_RATIO):
        return "neutral"
    return "retail_crowded"


def _level(low: float, span: float, alpha: float) -> float:
    return low + alpha * span


def _stop(low: float, span: float) -> float:
    return low - STOP_SPAN_FRAC * span


def _deep(low: float, span: float) -> float:
    return low - DEEP_SPAN_FRAC * span


def _emit(
    *,
    tier: Tier,
    entry_reason: str,
    order_type: OrderType,
    limit_price: float | None,
    stop_price: float,
    take_profit: float,
    zone_min: float,
    zone_max: float,
    alpha: float,
    span: float,
    low: float,
    mid: float,
    price: float,
    ratio: float,
    ratio_source: str | None,
    baseline_bars: int | None,
    multiplier: float,
    size_note: str,
    deep: float,
    legacy: tuple[bool | None, bool | None, bool | None],
    taker_buy_ratio: float | None = None,
    taker_buy_ratio_source: str | None = None,
    orderflow_regime: str | None = None,
    funding_rate: float | None = None,
    perp_basis_bps: float | None = None,
    perp_regime: str | None = None,
    boll_bandwidth_pct: float | None = None,
    xs_rel_1h: float | None = None,
    horizon_regime: str | None = None,
    composite_alpha_score: float | None = None,
) -> EntryDecision:
    return EntryDecision(
        emit=True,
        reject_reason=None,
        entry_reason=entry_reason,
        tier=tier,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        take_profit_price=take_profit,
        entry_zone_min=zone_min,
        entry_zone_max=zone_max,
        alpha=alpha,
        span=span,
        low=low,
        mid=mid,
        price=price,
        trade_size_ratio=ratio,
        trade_size_ratio_source=ratio_source,
        trade_size_baseline_bars=baseline_bars,
        position_size_multiplier=multiplier,
        position_size_note=size_note,
        deep_zone_price=deep,
        legacy_change_5m_pass=legacy[0],
        legacy_volume_strength_pass=legacy[1],
        legacy_boll_lower_break=legacy[2],
        taker_buy_ratio=taker_buy_ratio,
        taker_buy_ratio_source=taker_buy_ratio_source,
        orderflow_regime=orderflow_regime,
        funding_rate=funding_rate,
        perp_basis_bps=perp_basis_bps,
        perp_regime=perp_regime,
        boll_bandwidth_pct=boll_bandwidth_pct,
        xs_rel_1h=xs_rel_1h,
        horizon_regime=horizon_regime,
        composite_alpha_score=composite_alpha_score,
    )


def _limit_or_reject(
    *,
    price: float,
    limit_price: float,
    stop_price: float,
    mid: float,
    base: dict[str, Any],
    row: dict[str, Any] | None = None,
) -> EntryDecision:
    """Resting bid at ``L+0.20*S``. A limit at or above ``P`` would take, so it is not sent."""

    def _refused(reason: str) -> EntryDecision:
        return _empty(
            reason=reason,
            tier="neutral",
            ratio=base["ratio"],
            zone=(base["zone_min"], base["zone_max"]),
            stop_price=stop_price,
            take_profit_price=base["take_profit"],
            alpha=base["alpha"],
            span=base["span"],
            low=base["low"],
            mid=base["mid"],
            price=base["price"],
            deep=base["deep"],
            legacy=base["legacy"],
            ratio_source=base["ratio_source"],
            baseline_bars=base["baseline_bars"],
            taker_buy_ratio=base.get("taker_buy_ratio"),
            taker_buy_ratio_source=base.get("taker_buy_ratio_source"),
            orderflow_regime=base.get("orderflow_regime"),
            funding_rate=base.get("funding_rate"),
            perp_basis_bps=base.get("perp_basis_bps"),
            perp_regime=base.get("perp_regime"),
            boll_bandwidth_pct=base.get("boll_bandwidth_pct"),
            xs_rel_1h=base.get("xs_rel_1h"),
            horizon_regime=base.get("horizon_regime"),
        )

    if not _gt(price, limit_price) or limit_price <= 0 or stop_price >= limit_price:
        return _refused("neutral_limit_not_executable")
    odds = reward_risk(mid, limit_price, stop_price)
    if odds is None or not _gte(odds, NEUTRAL_MIN_ODDS):
        return _refused("neutral_limit_not_executable")
    if stop_distance_reason(limit_price, stop_price):
        return _refused("stop_too_close_to_entry")
    chosen_type: OrderType = "LIMIT_MAKER" if post_only_limit_enabled() else "LIMIT"
    alpha_score = compute_composite_alpha_score(
        row or {},
        odds=odds,
        trade_size_ratio=base.get("ratio"),
        taker_buy_ratio=base.get("taker_buy_ratio"),
    )
    return _emit(
        tier="neutral",
        entry_reason="neutral_limit",
        order_type=chosen_type,
        limit_price=limit_price,
        stop_price=stop_price,
        multiplier=0.5,
        size_note="half_planned_size_no_inverse_stop_width_sizer",
        composite_alpha_score=alpha_score,
        **base,
    )


def evaluate_entry_zone(
    row: dict[str, Any],
    *,
    min_change_5m: float = 2.0,
    min_volume_strength: float = 0.5,
) -> EntryDecision:
    """One closed bar → emit a buy intent or a fail-closed reason."""
    symbol = str(row.get("binance_symbol") or "").strip()
    if not symbol:
        return _empty(reason="binance_symbol_missing")
    coord_error = row.get("entry_zone_coord_error")
    if isinstance(coord_error, str) and coord_error.strip():
        return _empty(reason=coord_error.strip())

    low = _first(row, ("kline_low", "low", "bar_low"))
    mid = _first(row, ("boll_mid",))
    price = _first(row, ("price_usd", "close", "price"))
    legacy = _legacy(
        row,
        price,
        min_change_5m=min_change_5m,
        min_volume_strength=min_volume_strength,
    )
    if low is None:
        return _empty(reason="kline_low_missing", price=price, mid=mid, legacy=legacy)
    if mid is None:
        return _empty(reason="boll_mid_missing", low=low, price=price, legacy=legacy)
    if price is None:
        return _empty(reason="price_missing", low=low, mid=mid, legacy=legacy)
    if low <= 0 or mid <= 0 or price <= 0:
        return _empty(
            reason="price_not_positive",
            low=low,
            mid=mid,
            price=price,
            legacy=legacy,
        )

    span = mid - low
    deep = _deep(low, span) if span > 0 else None
    if span <= _CMP_EPS:
        return _empty(
            reason="span_not_positive",
            span=span,
            low=low,
            mid=mid,
            price=price,
            deep=None,
            legacy=legacy,
        )
    alpha = (price - low) / span
    if alpha != alpha:
        return _empty(
            reason="alpha_invalid",
            span=span,
            low=low,
            mid=mid,
            price=price,
            deep=deep,
            legacy=legacy,
        )

    ratio, ratio_source, ratio_error = resolve_trade_size_ratio(row)
    tbr, tbr_source, tbr_error = resolve_taker_buy_ratio(row)
    baseline_raw = row.get("trade_size_baseline_bars")
    baseline_bars = int(baseline_raw) if isinstance(baseline_raw, int) else None
    common = {
        "alpha": alpha,
        "span": span,
        "low": low,
        "mid": mid,
        "price": price,
        "deep": deep,
        "legacy": legacy,
        "ratio_source": ratio_source,
        "baseline_bars": baseline_bars,
        "taker_buy_ratio": tbr,
        "taker_buy_ratio_source": tbr_source,
    }
    if ratio_error:
        return _empty(reason=ratio_error, ratio=None, **common)
    if ratio is None:
        return _empty(reason="trade_size_ratio_missing", ratio=None, **common)
    if tbr_error:
        return _empty(reason=tbr_error, ratio=ratio, **common)
    if tbr is None and require_taker_buy_ratio_enabled():
        return _empty(reason="taker_buy_ratio_missing", ratio=ratio, **common)

    tier = _tier(ratio)
    stop_price = _stop(low, span)
    take_profit = mid
    zone_floor = _level(low, span, ALPHA_FLOOR)
    zone_cap = _level(low, span, ALPHA_CAP)
    neutral_min = _level(low, span, NEUTRAL_ALPHA_MIN)
    neutral_max = _level(low, span, NEUTRAL_ALPHA_MAX)
    limit_price = _level(low, span, NEUTRAL_LIMIT_ALPHA)

    if tier == "retail_crowded":
        return _empty(
            reason="retail_crowded_skip",
            tier=tier,
            ratio=ratio,
            zone=(zone_floor, zone_cap),
            stop_price=stop_price,
            take_profit_price=take_profit,
            **common,
        )

    orderflow_regime: str | None = None
    if tbr is not None:
        if not _gte(tbr, MIN_TAKER_BUY_RATIO):
            return _empty(
                reason="orderflow_toxic_sell_dump",
                tier=tier,
                ratio=ratio,
                zone=(zone_floor, zone_cap) if tier == "large_trade" else (neutral_min, neutral_max),
                stop_price=stop_price,
                take_profit_price=take_profit,
                orderflow_regime="toxic_sell_dump",
                **common,
            )
        if tier == "large_trade" and not _gte(tbr, LARGE_MIN_TAKER_BUY_RATIO):
            # Large trade size with weak taker-buy share (< 42%) means aggressive
            # sellers still dominate; downgrade from 1.0x market to 0.5x neutral rules.
            tier = "neutral"
            orderflow_regime = "absorption_weak_downgraded"
        else:
            orderflow_regime = "absorption_strong"

    common["orderflow_regime"] = orderflow_regime
    perp_verdict = evaluate_perp_microstructure(row, taker_buy_ratio=tbr)
    common["funding_rate"] = perp_verdict.funding_rate
    common["perp_basis_bps"] = perp_verdict.perp_basis_bps
    common["perp_regime"] = perp_verdict.perp_regime
    if not perp_verdict.allow:
        return _empty(
            reason=perp_verdict.reject_reason or "perp_microstructure_rejected",
            tier=tier,
            ratio=ratio,
            zone=(zone_floor, zone_cap) if tier == "large_trade" else (neutral_min, neutral_max),
            stop_price=stop_price,
            take_profit_price=take_profit,
            **common,
        )

    mh_verdict = evaluate_multi_horizon_regime(row)
    common["boll_bandwidth_pct"] = mh_verdict.boll_bandwidth_pct
    common["xs_rel_1h"] = mh_verdict.xs_rel_1h
    common["horizon_regime"] = mh_verdict.horizon_regime
    if not mh_verdict.allow:
        return _empty(
            reason=mh_verdict.reject_reason or "multi_horizon_rejected",
            tier=tier,
            ratio=ratio,
            zone=(zone_floor, zone_cap) if tier == "large_trade" else (neutral_min, neutral_max),
            stop_price=stop_price,
            take_profit_price=take_profit,
            **common,
        )
    if mh_verdict.downgrade_to_neutral and tier == "large_trade":
        tier = "neutral"

    base = {
        "take_profit": take_profit,
        "alpha": alpha,
        "span": span,
        "low": low,
        "mid": mid,
        "price": price,
        "ratio": ratio,
        "ratio_source": ratio_source,
        "baseline_bars": baseline_bars,
        "deep": deep,
        "legacy": legacy,
        "taker_buy_ratio": tbr,
        "taker_buy_ratio_source": tbr_source,
        "orderflow_regime": orderflow_regime,
        "funding_rate": perp_verdict.funding_rate,
        "perp_basis_bps": perp_verdict.perp_basis_bps,
        "perp_regime": perp_verdict.perp_regime,
        "boll_bandwidth_pct": mh_verdict.boll_bandwidth_pct,
        "xs_rel_1h": mh_verdict.xs_rel_1h,
        "horizon_regime": mh_verdict.horizon_regime,
    }

    if tier == "large_trade":
        zone = (zone_floor, zone_cap)
        if stop_price >= price:
            return _empty(
                reason="stop_not_below_price",
                tier=tier,
                ratio=ratio,
                zone=zone,
                stop_price=stop_price,
                take_profit_price=take_profit,
                **common,
            )
        if _gte(price, mid):
            return _empty(
                reason="price_above_mid",
                tier=tier,
                ratio=ratio,
                zone=zone,
                stop_price=stop_price,
                take_profit_price=take_profit,
                **common,
            )
        if not _gte(alpha, ALPHA_FLOOR):
            return _empty(
                reason="price_below_floor",
                tier=tier,
                ratio=ratio,
                zone=zone,
                stop_price=stop_price,
                take_profit_price=take_profit,
                **common,
            )
        if not _lte(alpha, ALPHA_CAP):
            return _empty(
                reason="alpha_above_cap",
                tier=tier,
                ratio=ratio,
                zone=zone,
                stop_price=stop_price,
                take_profit_price=take_profit,
                **common,
            )
        odds = reward_risk(mid, price, stop_price)
        if odds is None or not _gte(odds, LARGE_MIN_ODDS):
            return _empty(
                reason="odds_below_min",
                tier=tier,
                ratio=ratio,
                zone=zone,
                stop_price=stop_price,
                take_profit_price=take_profit,
                **common,
            )
        if stop_distance_reason(price, stop_price):
            return _empty(
                reason="stop_too_close_to_entry",
                tier=tier,
                ratio=ratio,
                zone=zone,
                stop_price=stop_price,
                take_profit_price=take_profit,
                **common,
            )
        alpha_score = compute_composite_alpha_score(
            row,
            odds=odds,
            trade_size_ratio=ratio,
            taker_buy_ratio=tbr,
        )
        return _emit(
            tier="large_trade",
            entry_reason="large_trade_market",
            order_type="MARKET",
            limit_price=None,
            stop_price=stop_price,
            zone_min=zone_floor,
            zone_max=zone_cap,
            multiplier=1.0,
            size_note="planned_single_name_size",
            composite_alpha_score=alpha_score,
            **base,
        )

    if tier == "neutral":
        zone = (neutral_min, neutral_max)
        if stop_price >= price:
            return _empty(
                reason="stop_not_below_price",
                tier=tier,
                ratio=ratio,
                zone=zone,
                stop_price=stop_price,
                take_profit_price=take_profit,
                **common,
            )
        neutral_base = {
            **base,
            "zone_min": neutral_min,
            "zone_max": neutral_max,
        }
        in_band = _gte(price, neutral_min) and _lte(price, neutral_max)
        if in_band:
            odds = reward_risk(mid, price, stop_price)
            if odds is not None and _gte(odds, NEUTRAL_MIN_ODDS):
                if stop_distance_reason(price, stop_price):
                    return _empty(
                        reason="stop_too_close_to_entry",
                        tier=tier,
                        ratio=ratio,
                        zone=zone,
                        stop_price=stop_price,
                        take_profit_price=take_profit,
                        **common,
                    )
                alpha_score = compute_composite_alpha_score(
                    row,
                    odds=odds,
                    trade_size_ratio=ratio,
                    taker_buy_ratio=tbr,
                )
                return _emit(
                    tier="neutral",
                    entry_reason="neutral_market",
                    order_type="MARKET",
                    limit_price=None,
                    stop_price=stop_price,
                    multiplier=0.5,
                    size_note="half_planned_size_no_inverse_stop_width_sizer",
                    composite_alpha_score=alpha_score,
                    **neutral_base,
                )
            if _gt(price, limit_price):
                return _limit_or_reject(
                    price=price,
                    limit_price=limit_price,
                    stop_price=stop_price,
                    mid=mid,
                    base=neutral_base,
                    row=row,
                )
            return _empty(
                reason="odds_below_min",
                tier=tier,
                ratio=ratio,
                zone=zone,
                stop_price=stop_price,
                take_profit_price=take_profit,
                **common,
            )
        if _gt(price, neutral_max):
            return _limit_or_reject(
                price=price,
                limit_price=limit_price,
                stop_price=stop_price,
                mid=mid,
                base=neutral_base,
                row=row,
            )
        return _empty(
            reason="neutral_below_band",
            tier=tier,
            ratio=ratio,
            zone=zone,
            stop_price=stop_price,
            take_profit_price=take_profit,
            **common,
        )

    unexpected: never = tier
    return _empty(reason=f"unknown_tier:{unexpected}", ratio=ratio, **common)
