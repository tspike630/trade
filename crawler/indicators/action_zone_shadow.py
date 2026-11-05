"""Shadow entry-zone columns on ``binance_indicators``.

Computed from closed Binance spot klines only. A 5m candle that has not
reached its exchange ``close_time`` is still printing a wick; it is not a
pin, not a ``no_new_low`` check, and not a breakout.

Two families land on the same row:

* Action-completion (``action_complete``, ``breakout_fired``, and the
  washout flags) is ablation-only. Decision does not stack these as extra
  hard gates on the 3-tier entry.
* The α ruler is the recipe decision will read. Canonical stop is
  ``stop_price_zone = L - 0.1 * S``. ``natural_stop_price`` (pin low minus
  10 bps) is a secondary variant and is not the live stop.

Nothing here changes ``volume_strength``, BOLL parameters, the paper
``tech_boll_fade`` thresholds, TP/SL in the strategy, or the kill switch.
``action_zone_weight`` stays 0 and ``action_zone_state`` stays ``shadow``.
"""

from __future__ import annotations

import os
import re
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from logging_setup import get_logger

from indicators.technical_lib import boll_band_series

log = get_logger("indicators.action_zone_shadow")

# Pin triggers. These do not replace the live change_5m <= -2 gate.
PIN_CHANGE_5M_MAX = -2.0
PIN_CHANGE_5M_Z_MAX = -2.0
Z_PRIOR_RETURNS = 20
Z_SIGMA_EPS = 1e-12
RANGE_EPS = 1e-12
CMP_EPS = 1e-9

# Spot washout. Inputs are PR #164 columns when those fields exist on the
# kline; otherwise the three flags stay NULL.
TRADES_SPIKE_PRIOR_BARS = 12
TRADES_CLIMAX_MIN = 2.0
TAKER_BUY_ABSORB_MIN = 0.55
AVG_TRADE_PRIOR_BARS = 12
AVG_TRADE_SPIKE_MIN = 2.0

# Canonical recipe. SL = L - 0.1*S. Do not fit this to a worked example
# that printed 0.937: that number is L - 0.05*S and is not the formula.
STOP_ZONE_S_FRAC = 0.1
# Secondary, non-canonical. pin_low * (1 - 10/10000). Tick-and-fee haircut
# kept so review can ablate it against stop_price_zone. Not the live stop.
STOP_BUFFER_BPS = 10

TRADE_SIZE_PRIOR_BARS = 288  # 24h of 5m bars, current bar excluded
TIER1_SIZE_MIN = 1.3
TIER2_SIZE_MIN = 0.85
TIER1_ALPHA_LO = 0.08
TIER1_ALPHA_HI = 0.40
TIER2_ALPHA_LO = 0.12
TIER2_ALPHA_HI = 0.28
TIER2_LIMIT_ALPHA = 0.20
TIER3_HUNTER_S_FRAC = 0.50
TIER1_RR_MIN = 1.5
TIER2_RR_MIN = 2.5

# Futures out-clearing. Collection owns crowding_futures.db. This pipeline
# only reads; a missing file or an unfamiliar column stays NULL, not 0.
LS_PRIOR_SAMPLES = 3
LS_FLUSH_DROP_PCT = 5.0
FUTURES_TABLE_LS = "ls_ratio"
FUTURES_TABLE_OI = "oi_hist"

ACTION_ZONE_STATE = "shadow"
ACTION_ZONE_WEIGHT = 0.0

REASON_BAREFOOT = "barefoot"
REASON_CHASED = "chased"
REASON_BELOW_ZONE = "below_zone"
REASON_RR = "rr_below_bar"
REASON_RETAIL = "retail_crowded"

_LS_VALUE_CANDIDATES = (
    "global_long_short_account_ratio",
    "long_short_ratio",
    "longshortratio",
    "ratio",
    "ls_ratio",
)
_OI_VALUE_CANDIDATES = (
    "sum_open_interest",
    "open_interest",
    "openinterest",
    "oi",
)
_TIME_CANDIDATES = (
    "timestamp",
    "time_ms",
    "open_time_ms",
    "time",
    "ts",
    "recorded_at",
)
_SYMBOL_CANDIDATES = ("symbol", "pair", "binance_symbol")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# (column, sqlite type). ALTER-only. avg_trade_size is also added by the
# open crowding PR; ensure_schema skips a name that is already present and
# the INSERT dedupes so the two migrations can land in either order.
ACTION_ZONE_COLUMN_SPEC: tuple[tuple[str, str], ...] = (
    ("sell_climax_low", "REAL"),
    ("pin_low", "REAL"),
    ("hammer_close", "REAL"),
    ("lower_shadow_rebound_pct", "REAL"),
    ("no_new_low", "INTEGER"),
    ("action_complete", "INTEGER"),
    ("rebound_from_low_pct", "REAL"),
    ("setup_invalidated", "INTEGER"),
    ("breakout_entry_price", "REAL"),
    ("breakout_fired", "INTEGER"),
    ("natural_entry_low", "REAL"),
    ("natural_entry_high", "REAL"),
    ("natural_stop_price", "REAL"),
    ("stop_buffer_bps", "INTEGER"),
    ("trades_climax", "INTEGER"),
    ("taker_buy_absorb", "INTEGER"),
    ("avg_trade_size_spike", "INTEGER"),
    ("avg_trade_size", "REAL"),
    ("ls_flush", "INTEGER"),
    ("oi_flush", "INTEGER"),
    ("crowding_cleared", "INTEGER"),
    ("crowding_crowded_long", "INTEGER"),
    ("action_zone_ready", "INTEGER"),
    ("action_zone_state", "TEXT"),
    ("action_zone_weight", "REAL"),
    ("space_l", "REAL"),
    ("space_m", "REAL"),
    ("space_s", "REAL"),
    ("alpha_zone", "REAL"),
    ("rr_to_mid", "REAL"),
    ("rr_raw", "REAL"),
    ("trade_size_ratio", "REAL"),
    ("zone_tier", "INTEGER"),
    ("entry_zone_low", "REAL"),
    ("entry_zone_high", "REAL"),
    ("hunter_limit_price", "REAL"),
    ("entry_signal", "TEXT"),
    ("entry_skip_reason", "TEXT"),
    ("limit_buy_price", "REAL"),
    ("stop_price_zone", "REAL"),
    ("tp_price", "REAL"),
)

ALPHA_COLUMNS = (
    "space_l",
    "space_m",
    "space_s",
    "alpha_zone",
    "rr_to_mid",
    "rr_raw",
    "trade_size_ratio",
    "zone_tier",
    "entry_zone_low",
    "entry_zone_high",
    "hunter_limit_price",
    "entry_signal",
    "entry_skip_reason",
    "limit_buy_price",
    "stop_price_zone",
    "tp_price",
)

ACTION_ZONE_COLUMN_NAMES = tuple(name for name, _typ in ACTION_ZONE_COLUMN_SPEC)


def indicator_write_columns(base: Sequence[str]) -> tuple[str, ...]:
    """Base indicator columns plus these, with duplicates removed.

    PR #164 appends ``avg_trade_size`` onto the base list. Listing it here
    as well must not produce ``INSERT`` with the same name twice.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for name in (*base, *ACTION_ZONE_COLUMN_NAMES):
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return tuple(ordered)


def lock_shadow_row(row: dict[str, Any]) -> dict[str, Any]:
    """Force weight 0 and state shadow. This writer does not arm sizing."""
    row["action_zone_state"] = ACTION_ZONE_STATE
    row["action_zone_weight"] = ACTION_ZONE_WEIGHT
    row["stop_buffer_bps"] = STOP_BUFFER_BPS
    return row


def add_action_zone_columns(conn: sqlite3.Connection) -> None:
    """``ALTER TABLE binance_indicators ADD COLUMN`` for any name missing.

    Does not rebuild the table, does not touch ``binance_klines``, and does
    not touch ``token_indicators``. A name that already exists (including
    ``avg_trade_size`` from the crowding PR) is left alone.
    """
    try:
        rows = conn.execute("PRAGMA table_info(binance_indicators)").fetchall()
    except sqlite3.Error as exc:
        log.warning("binance_indicators column probe failed: %s", exc)
        return
    existing = {str(row[1]) for row in rows}
    if not existing:
        return
    for name, typ in ACTION_ZONE_COLUMN_SPEC:
        if name in existing:
            continue
        if not _IDENT.match(name) or not _IDENT.match(typ):
            raise ValueError(f"refusing to add column {name} {typ}")
        conn.execute(f"ALTER TABLE binance_indicators ADD COLUMN {name} {typ}")


def _is_finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def _blank_row() -> dict[str, Any]:
    row: dict[str, Any] = {name: None for name in ACTION_ZONE_COLUMN_NAMES}
    return lock_shadow_row(row)


def lower_shadow_rebound_pct(high: float, low: float, close: float) -> float | None:
    """``(close - low) / max(high - low, eps) * 100``.

    A zero-range bar uses ``eps`` so the division is defined. When the close
    sits on that low the result is 0. ``high < low`` is not a bar and is NULL.
    """
    if not _is_finite(high) or not _is_finite(low) or not _is_finite(close):
        return None
    if high < low:
        return None
    span = float(high) - float(low)
    denom = span if span > RANGE_EPS else RANGE_EPS
    return (float(close) - float(low)) / denom * 100.0


def bar_is_pin(
    *,
    pierced: bool,
    change_5m: float | None,
    change_5m_z: float | None,
) -> bool:
    """True when a closed bar is a sell-climax candidate.

    Any one leg is enough: it crossed below its own BOLL lower band, its
    5m return is <= -2 percentage points, or that return is <= -2 sample
    standard deviations of the prior 20 bar returns.
    """
    if pierced:
        return True
    if change_5m is not None and change_5m <= PIN_CHANGE_5M_MAX:
        return True
    if change_5m_z is not None and change_5m_z <= PIN_CHANGE_5M_Z_MAX:
        return True
    return False


def _return_pct(prev_close: float, close: float) -> float | None:
    if not _is_finite(prev_close) or not _is_finite(close):
        return None
    if prev_close <= 0 or close <= 0:
        return None
    return (close - prev_close) / prev_close * 100.0


def _z_at(returns: Sequence[float | None], index: int) -> float | None:
    """Current bar return / sample σ of the previous 20 returns.

    The current return is not inside σ. σ <= 1e-12 percentage points
    (a flat window) is NULL, not infinity.
    """
    if index < Z_PRIOR_RETURNS:
        return None
    current = returns[index]
    if current is None:
        return None
    prior = list(returns[index - Z_PRIOR_RETURNS : index])
    if len(prior) != Z_PRIOR_RETURNS or any(value is None for value in prior):
        return None
    try:
        sigma = statistics.stdev(float(value) for value in prior if value is not None)
    except statistics.StatisticsError:
        return None
    if sigma != sigma or sigma <= Z_SIGMA_EPS:
        return None
    return float(current) / sigma


def _pierced(
    index: int,
    ordered: Sequence[Any],
    lower: Sequence[float | None],
) -> bool:
    """The bar crossed the lower band on this close, not 'still underneath'.

    A later bar that opens and closes below the band is not a new pierce,
    so the climax stays the bar that actually went through.
    """
    if index >= len(lower):
        return False
    band = lower[index]
    if band is None:
        return False
    bar = ordered[index]
    if float(bar.low) >= float(band):
        return False
    if float(bar.open) >= float(band):
        return True
    if index == 0:
        return True
    prev_band = lower[index - 1]
    if prev_band is None:
        return True
    return float(ordered[index - 1].close) >= float(prev_band)


def _pin_index(
    ordered: Sequence[Any],
    lower: Sequence[float | None],
    returns: Sequence[float | None],
) -> int | None:
    """Most recent closed pin. Later bars do not rewrite an earlier pin's coordinates."""
    for index in range(len(ordered) - 1, -1, -1):
        if bar_is_pin(
            pierced=_pierced(index, ordered, lower),
            change_5m=returns[index],
            change_5m_z=_z_at(returns, index),
        ):
            return index
    return None


def _avg_size(bar: Any) -> float | None:
    """``quote_volume / trades`` for one bar. Empty or non-positive trades → None."""
    trades = getattr(bar, "trades", None)
    if isinstance(trades, bool) or not _is_finite(trades) or float(trades) <= 0:
        return None
    quote = getattr(bar, "quote_volume", None)
    if not _is_finite(quote) or float(quote) < 0:
        return None
    return float(quote) / float(trades)


def trade_size_ratio_at(ordered: Sequence[Any], index: int) -> tuple[float | None, float | None]:
    """``(avg_trade_size of this bar, size / mean of the prior 288 bars)``.

    The current bar is not in the mean. The window is 24h of 5m bars.
    Any bar in that window with no usable trade count makes the ratio NULL
    rather than a shorter average. The ratio is a change in print size.
    It does not identify a whale and it does not say one print ended the drop.
    """
    current = _avg_size(ordered[index])
    if current is None:
        return None, None
    start = index - TRADE_SIZE_PRIOR_BARS
    if start < 0:
        return current, None
    samples: list[float] = []
    for bar in ordered[start:index]:
        value = _avg_size(bar)
        if value is None:
            return current, None
        samples.append(value)
    if len(samples) != TRADE_SIZE_PRIOR_BARS:
        return current, None
    mean = sum(samples) / float(len(samples))
    if mean <= 0:
        return current, None
    return current, current / mean


def _zone_tier(ratio: float) -> int:
    """1 large prints absorbing, 2 mixed, 3 retail crowded.

    Boundaries: ``>= 1.3`` tier 1, ``>= 0.85`` and ``< 1.3`` tier 2, else 3.
    """
    if ratio >= TIER1_SIZE_MIN:
        return 1
    if ratio >= TIER2_SIZE_MIN:
        return 2
    return 3


def _inside(price: float, low: float, high: float) -> bool:
    return price >= low - CMP_EPS and price <= high + CMP_EPS


def _rr_meets(rr: float | None, bar: float) -> bool:
    return rr is not None and rr + CMP_EPS >= bar


def _entry_decision(
    *,
    tier: int,
    price: float,
    floor: float,
    span: float,
    alpha: float,
    rr: float | None,
) -> tuple[str, str | None, float | None, float | None, float | None, float | None]:
    """Return signal, skip reason, zone low, zone high, limit price, hunter price.

    Market requires the close inside that tier's zone AND buffered
    ``rr_to_mid`` at or above the tier bar (1.5 / 2.5). Tier 2 above the
    zone is a limit at ``L + 0.20 S``. Tier 3 is skip ``retail_crowded``;
    the hunter price ``L - 0.50 S`` is stored beside it and is not a market buy.
    """
    if tier == 1:
        zone_lo = floor + TIER1_ALPHA_LO * span
        zone_hi = floor + TIER1_ALPHA_HI * span
        if _inside(price, zone_lo, zone_hi) and _rr_meets(rr, TIER1_RR_MIN):
            return "market", None, zone_lo, zone_hi, None, None
        if price > zone_hi + CMP_EPS:
            return "skip", REASON_CHASED, zone_lo, zone_hi, None, None
        if price < zone_lo - CMP_EPS:
            return "skip", REASON_BAREFOOT, zone_lo, zone_hi, None, None
        return "skip", REASON_RR, zone_lo, zone_hi, None, None
    if tier == 2:
        zone_lo = floor + TIER2_ALPHA_LO * span
        zone_hi = floor + TIER2_ALPHA_HI * span
        if _inside(price, zone_lo, zone_hi) and _rr_meets(rr, TIER2_RR_MIN):
            return "market", None, zone_lo, zone_hi, None, None
        if price > zone_hi + CMP_EPS:
            limit_px = floor + TIER2_LIMIT_ALPHA * span
            return "limit", None, zone_lo, zone_hi, limit_px, None
        if price < zone_lo - CMP_EPS:
            reason = REASON_BAREFOOT if alpha < TIER1_ALPHA_LO else REASON_BELOW_ZONE
            return "skip", reason, zone_lo, zone_hi, None, None
        return "skip", REASON_RR, zone_lo, zone_hi, None, None
    if tier == 3:
        hunter = floor - TIER3_HUNTER_S_FRAC * span
        return "skip", REASON_RETAIL, None, None, None, hunter
    raise AssertionError(f"unknown zone_tier {tier}")


def alpha_recipe(
    *,
    low: float,
    mid: float | None,
    close: float,
    trade_size_ratio: float | None,
) -> dict[str, Any]:
    """α coordinates for one closed pin bar.

    ``L`` is that bar's low, ``M`` is BOLL mid on that same bar, ``P`` is
    that bar's close, ``S = M - L``. ``S <= 0`` nulls the whole α group.
    ``rr_to_mid = (M - P) / (P - (L - 0.1 S))``. ``rr_raw = (M - P) / (P - L)``
    when ``P > L``, else NULL. ``rr_raw`` is for ablation; the tier bar
    reads the buffered ratio.
    """
    nulls = {name: None for name in ALPHA_COLUMNS}
    if mid is None or not _is_finite(low) or not _is_finite(close) or not _is_finite(mid):
        return nulls
    floor = float(low)
    mean = float(mid)
    price = float(close)
    span = mean - floor
    if span <= 0:
        return nulls
    # Canonical stop. Worked-example 0.937 is L - 0.05*S and is not used.
    stop = floor - STOP_ZONE_S_FRAC * span
    alpha = (price - floor) / span
    rr_raw = None
    if price > floor:
        rr_raw = (mean - price) / (price - floor)
    risk = price - stop
    rr = (mean - price) / risk if risk > 0 else None
    out = dict(nulls)
    out["space_l"] = floor
    out["space_m"] = mean
    out["space_s"] = span
    out["alpha_zone"] = alpha
    out["rr_to_mid"] = rr
    out["rr_raw"] = rr_raw
    out["stop_price_zone"] = stop
    out["tp_price"] = mean
    ratio = float(trade_size_ratio) if _is_finite(trade_size_ratio) else None
    if ratio is not None and ratio < 0:
        ratio = None
    out["trade_size_ratio"] = ratio
    if ratio is None:
        return out
    tier = _zone_tier(ratio)
    out["zone_tier"] = tier
    signal, reason, zone_lo, zone_hi, limit_px, hunter = _entry_decision(
        tier=tier,
        price=price,
        floor=floor,
        span=span,
        alpha=alpha,
        rr=rr,
    )
    out["entry_signal"] = signal
    out["entry_skip_reason"] = reason
    out["entry_zone_low"] = zone_lo
    out["entry_zone_high"] = zone_hi
    out["limit_buy_price"] = limit_px
    out["hunter_limit_price"] = hunter
    return out


def _trades_spike_at(ordered: Sequence[Any], index: int) -> float | None:
    start = index - TRADES_SPIKE_PRIOR_BARS
    if start < 0:
        return None
    trades = getattr(ordered[index], "trades", None)
    if isinstance(trades, bool) or not _is_finite(trades) or float(trades) < 0:
        return None
    samples: list[float] = []
    for bar in ordered[start:index]:
        count = getattr(bar, "trades", None)
        if isinstance(count, bool) or not _is_finite(count) or float(count) < 0:
            return None
        samples.append(float(count))
    if len(samples) != TRADES_SPIKE_PRIOR_BARS:
        return None
    mean = sum(samples) / float(len(samples))
    if mean <= 0:
        return None
    return float(trades) / mean


def _taker_ratio(bar: Any) -> float | None:
    taker = getattr(bar, "taker_buy_quote_volume", None)
    if not _is_finite(taker) or float(taker) < 0:
        return None
    quote = getattr(bar, "quote_volume", None)
    if not _is_finite(quote) or float(quote) <= 0:
        return None
    return float(taker) / float(quote)


def _avg_trade_size_spike(ordered: Sequence[Any], index: int) -> int | None:
    """1 when this bar's print size is >= 2× the median of the prior 12."""
    current = _avg_size(ordered[index])
    if current is None:
        return None
    start = index - AVG_TRADE_PRIOR_BARS
    if start < 0:
        return None
    samples: list[float] = []
    for bar in ordered[start:index]:
        value = _avg_size(bar)
        if value is None:
            return None
        samples.append(value)
    if len(samples) != AVG_TRADE_PRIOR_BARS:
        return None
    median = statistics.median(samples)
    if median <= 0:
        return None
    return 1 if current >= AVG_TRADE_SPIKE_MIN * median else 0


def crowding_columns_present(bars: Sequence[Any]) -> bool:
    """True when klines carry PR #164's ``taker_buy_quote_volume`` field.

    Before that field exists, washout flags stay NULL. A test can set the
    attribute on a bar to turn the flags on without waiting for that PR.
    """
    for bar in bars:
        if "taker_buy_quote_volume" in getattr(bar, "__dict__", {}):
            return True
        fields = getattr(type(bar), "__dataclass_fields__", None) or {}
        if "taker_buy_quote_volume" in fields:
            return True
    return False


def _flag_ge(value: float | None, threshold: float) -> int | None:
    if value is None:
        return None
    return 1 if value >= threshold else 0


def _tri_and(legs: Sequence[int | None]) -> int | None:
    """SQL AND: a known 0 wins; otherwise any NULL stays NULL; else 1."""
    if any(leg == 0 for leg in legs):
        return 0
    if any(leg is None for leg in legs):
        return None
    return 1


def _not_flag(flag: int | None) -> int | None:
    if flag is None:
        return None
    if flag == 1:
        return 0
    if flag == 0:
        return 1
    return None


def _closed_bars(klines: Sequence[Any], now_ms: int | None) -> list[Any]:
    """Drop the forming candle.

    Closed means ``close_time_ms <= now``. The in-progress 5m bar's high
    and low are an unfinished wick and are ignored by every shadow field,
    including ``no_new_low`` and the α ruler.
    """
    moment = int(time.time() * 1000) if now_ms is None else int(now_ms)
    ordered = sorted(klines, key=lambda bar: (int(bar.close_time_ms), int(bar.open_time_ms)))
    return [bar for bar in ordered if int(bar.close_time_ms) <= moment]


def _confirmation(ordered: Sequence[Any], pin_index: int, row: dict[str, Any]) -> None:
    """Next closed bar only. No intrabar wick, no bar after a later pin."""
    pin = ordered[pin_index]
    pin_low = float(pin.low)
    row["sell_climax_low"] = pin_low
    row["pin_low"] = pin_low
    row["hammer_close"] = float(pin.close)
    row["lower_shadow_rebound_pct"] = lower_shadow_rebound_pct(
        float(pin.high), pin_low, float(pin.close)
    )
    row["natural_entry_low"] = pin_low
    # Non-canonical secondary stop. Live recipe uses stop_price_zone.
    if pin_low > 0:
        row["natural_stop_price"] = pin_low * (1.0 - STOP_BUFFER_BPS / 10_000.0)
    if pin_index + 1 >= len(ordered):
        return
    confirm = ordered[pin_index + 1]
    confirm_high = float(confirm.high)
    confirm_low = float(confirm.low)
    confirm_close = float(confirm.close)
    row["natural_entry_high"] = confirm_high
    row["breakout_entry_price"] = confirm_high
    no_new = 1 if confirm_low >= pin_low else 0
    row["no_new_low"] = no_new
    row["action_complete"] = 1 if no_new == 1 and confirm_close > pin_low else 0
    if pin_low > 0:
        row["rebound_from_low_pct"] = (confirm_close - pin_low) / pin_low * 100.0
    if no_new == 0:
        row["setup_invalidated"] = 1
        row["breakout_fired"] = 0
        return
    # Low held on the confirmation bar. The setup is live until a later
    # closed bar trades under the pin, or a breakout prints.
    row["setup_invalidated"] = 0
    fired: int | None = None if row["action_complete"] == 1 else 0
    for bar in ordered[pin_index + 2 :]:
        if float(bar.low) < pin_low:
            row["setup_invalidated"] = 1
            fired = 0
            break
        if (
            row["action_complete"] == 1
            and float(bar.high) > confirm_high
            and float(bar.low) >= pin_low
        ):
            fired = 1
            row["setup_invalidated"] = 0
            break
    row["breakout_fired"] = fired


def futures_db_candidates(root: Path | None = None) -> list[Path]:
    """Where collection may have left ``crowding_futures.db``. First file wins."""
    base = root or Path(__file__).resolve().parents[2]
    paths: list[Path] = []
    env = os.environ.get("CROWDING_FUTURES_DB", "").strip()
    if env:
        paths.append(Path(env))
    paths.append(base / "exports" / "crowding_futures.db")
    paths.append(base / "crawler" / "exports" / "crowding_futures.db")
    return paths


def _discover_futures_db() -> Path | None:
    for path in futures_db_candidates():
        if path.is_file():
            return path
    return None


def _as_ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number:
            return None
        if number > 1e12:
            return int(number)
        if number > 1e9:
            return int(number * 1000.0)
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.replace(".", "", 1).isdigit():
        return _as_ms(float(text) if "." in text else int(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000.0)


def _symbol_forms(symbol: str) -> list[str]:
    raw = symbol.strip().upper()
    compact = raw.replace("/", "").replace("-", "").replace("_", "").replace(":", "")
    forms = {raw, compact}
    if compact.endswith("USDT") and len(compact) > 4:
        base = compact[: -len("USDT")]
        forms.add(f"{base}/USDT")
        forms.add(f"{base}USDT")
    return sorted(forms)


def _pick(columns: Sequence[str], options: tuple[str, ...]) -> str | None:
    folded = {column.lower(): column for column in columns if _IDENT.match(column)}
    for option in options:
        found = folded.get(option)
        if found is not None:
            return found
    return None


def _read_points(
    conn: sqlite3.Connection,
    table: str,
    symbol: str,
    value_candidates: tuple[str, ...],
    as_of_ms: int,
) -> list[tuple[int, float]] | None:
    """Points at or before ``as_of_ms``. None means the table is not usable."""
    if not _IDENT.match(table):
        return None
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if found is None:
        return None
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    columns = [str(row[1]) for row in info]
    sym_col = _pick(columns, _SYMBOL_CANDIDATES)
    time_col = _pick(columns, _TIME_CANDIDATES)
    val_col = _pick(columns, value_candidates)
    if sym_col is None or time_col is None or val_col is None:
        return None
    forms = _symbol_forms(symbol)
    placeholders = ", ".join("?" for _ in forms)
    sql = (
        f"SELECT {time_col}, {val_col} FROM {table} "
        f"WHERE UPPER(TRIM({sym_col})) IN ({placeholders})"
    )
    fetched = conn.execute(sql, forms).fetchall()
    points: list[tuple[int, float]] = []
    for raw_time, raw_value in fetched:
        stamp = _as_ms(raw_time)
        if stamp is None or stamp > as_of_ms or not _is_finite(raw_value):
            continue
        points.append((stamp, float(raw_value)))
    points.sort()
    return points


def _window(
    points: Sequence[tuple[int, float]] | None,
) -> tuple[float, float, int] | None:
    """``(latest, mean of prior 3, oldest prior timestamp)``."""
    if points is None or len(points) < LS_PRIOR_SAMPLES + 1:
        return None
    chunk = list(points[-(LS_PRIOR_SAMPLES + 1) :])
    latest = chunk[-1][1]
    prior = chunk[:-1]
    mean = sum(value for _stamp, value in prior) / float(len(prior))
    return latest, mean, prior[0][0]


def _price_fell(bars: Sequence[Any], oldest_ms: int, latest_close: float) -> bool | None:
    ref: float | None = None
    for bar in bars:
        if int(bar.close_time_ms) <= oldest_ms:
            ref = float(bar.close)
    if ref is None or ref <= 0 or not _is_finite(latest_close):
        return None
    return float(latest_close) < ref


def _futures_nulls() -> dict[str, None]:
    return {
        "ls_flush": None,
        "oi_flush": None,
        "crowding_cleared": None,
        "crowding_crowded_long": None,
    }


def read_futures_crowding(
    symbol: str | None,
    closed: Sequence[Any],
    *,
    futures_db: Path | None = None,
) -> dict[str, int | None]:
    """Long/short and open-interest flush labels, or NULLs when unreachable.

    Window is the latest sample at or before the last closed spot bar, versus
    the mean of the 3 samples before it. ``ls_flush`` is 1 when that ratio
    dropped by more than 5%. ``oi_flush`` is 1 when open interest is below
    its prior mean and spot price fell versus the closed bar at or before
    the oldest of those 3 OI samples. ``crowding_crowded_long`` is both
    series rising while price falls. Collection owns the join; this function
    does not create the database.
    """
    nulls = _futures_nulls()
    if not symbol or not closed:
        return nulls
    path = futures_db if futures_db is not None else _discover_futures_db()
    if path is None or not path.is_file():
        return nulls
    as_of = int(closed[-1].close_time_ms)
    latest_close = float(closed[-1].close)
    try:
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.warning("crowding_futures open failed: %s", exc)
        return nulls
    try:
        ls_points = _read_points(conn, FUTURES_TABLE_LS, symbol, _LS_VALUE_CANDIDATES, as_of)
        oi_points = _read_points(conn, FUTURES_TABLE_OI, symbol, _OI_VALUE_CANDIDATES, as_of)
    except sqlite3.Error as exc:
        log.warning("crowding_futures read failed: %s", exc)
        return nulls
    finally:
        conn.close()
    ls = _window(ls_points)
    oi = _window(oi_points)
    ls_flush: int | None
    if ls is None or ls[1] <= 0:
        ls_flush = None
    else:
        drop_pct = (ls[1] - ls[0]) / ls[1] * 100.0
        ls_flush = 1 if drop_pct > LS_FLUSH_DROP_PCT else 0
    oi_flush: int | None
    crowded: int | None
    if oi is None or oi[1] <= 0:
        oi_flush = None
        crowded = None
    else:
        fell = _price_fell(closed, oi[2], latest_close)
        if fell is None:
            oi_flush = None
            crowded = None
        else:
            oi_flush = 1 if oi[0] < oi[1] and fell else 0
            if ls is None or ls[1] <= 0:
                crowded = None
            else:
                crowded = 1 if ls[0] > ls[1] and oi[0] > oi[1] and fell else 0
    cleared = _tri_and((ls_flush, oi_flush))
    return {
        "ls_flush": ls_flush,
        "oi_flush": oi_flush,
        "crowding_cleared": cleared,
        "crowding_crowded_long": crowded,
    }


def action_zone_row_from_klines(
    klines: Sequence[Any],
    *,
    symbol: str | None = None,
    now_ms: int | None = None,
    futures_db: Path | None = None,
    crowding_columns: bool | None = None,
) -> dict[str, Any]:
    """One indicator row of shadow columns. Missing inputs stay NULL.

    ``action_complete`` and ``breakout_fired`` are informational. The α
    fields describe the latest closed pin only, using that bar's low, close,
    and BOLL mid. Bars after the pin do not move ``space_m`` or ``alpha_zone``.
    """
    row = _blank_row()
    closed = _closed_bars(klines, now_ms)
    if not closed:
        return row
    closes = [float(bar.close) for bar in closed]
    _lower, mid, _upper = boll_band_series(closes)
    returns: list[float | None] = [None]
    for prev, bar in zip(closed, closed[1:]):
        returns.append(_return_pct(float(prev.close), float(bar.close)))
    pin_index = _pin_index(closed, _lower, returns)
    enabled = (
        crowding_columns_present(closed) if crowding_columns is None else bool(crowding_columns)
    )
    if pin_index is not None:
        _confirmation(closed, pin_index, row)
        pin_avg, ratio = trade_size_ratio_at(closed, pin_index)
        row["avg_trade_size"] = pin_avg
        recipe = alpha_recipe(
            low=float(closed[pin_index].low),
            mid=mid[pin_index] if pin_index < len(mid) else None,
            close=float(closed[pin_index].close),
            trade_size_ratio=ratio,
        )
        row.update(recipe)
        if enabled:
            spike = _trades_spike_at(closed, pin_index)
            taker = _taker_ratio(closed[pin_index])
            row["trades_climax"] = _flag_ge(spike, TRADES_CLIMAX_MIN)
            row["taker_buy_absorb"] = _flag_ge(taker, TAKER_BUY_ABSORB_MIN)
            row["avg_trade_size_spike"] = _avg_trade_size_spike(closed, pin_index)
        row.update(
            read_futures_crowding(symbol, closed, futures_db=futures_db)
        )
        # Composite is about this pin. It is not an entry gate.
        # Futures data missing stays NULL, so a missing clear does not
        # become a hard 0 by itself unless some other leg is already 0.
        row["action_zone_ready"] = _tri_and(
            (
                row["action_complete"],
                _not_flag(row["setup_invalidated"]),
                row["trades_climax"],
                row["taker_buy_absorb"],
                row["crowding_cleared"],
            )
        )
    else:
        row.update(read_futures_crowding(symbol, closed, futures_db=futures_db))
    return lock_shadow_row(row)
