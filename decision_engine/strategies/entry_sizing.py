"""Buy size_pct: ordinary floor, or thick-pool full size.

Paper execution may already treat notional as full size. This module is the
decision-layer answer: a buy is ``size_pct ≈ 1`` only when every thick-pool
condition holds. Every other buy stays at the existing floor (default 0.05,
never raised to 0.25).

Thick-pool (all required):

1. measured liquidity >= 150_000 USD (missing liquidity does not qualify)
2. pool age >= 7 days (from ``pair_created_at`` / ``pairCreatedAt`` ms, or an
   explicit ``pool_age_days`` / ``pool_age_min`` already on the row)
3. R3 = 0. R3 rejects when ``volatility_24h > 2.5`` AND (``change_1h < 0`` OR
   ``rsi_14 < 30`` OR ``rsi_14 >= 70``). Same predicate as
   ``crawler/indicators/shadow_stop_prone.eval_r3``. Missing vol / change / RSI
   does not by itself set R3.
4. ``jev_strength >= 0.15`` and the value is present

Admission floors are unchanged and are not reimplemented here: live liquidity
``DECISION_MIN_LIQ_USD`` (50k), strategy strength thresholds, and backtest
coverage >= 10. This function only chooses size. It does not drop a buy.

Not applied (documented so they are not silent blocks):

- Open ``supply_gaps`` window. An outage row in
  ``exports/reports/supply_gaps.csv`` does not skip or resize a buy.
- Stricter liquidity tier. Set ``DECISION_THICK_STRICT_MIN_LIQ_USD=500000``
  to require 500k before full size. Unset leaves the 150k rule.

``scripts/weekly_side_evidence.py`` ``step2_blocked`` (#71) is not read or
cleared here. Sizing does not open Step2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine import config as de_config
from decision_engine.core.signal_bus import coerce_pair_created_at_ms

log = get_logger("decision_engine.entry_sizing")

_MS_PER_DAY = 86_400_000.0
_MIN_PER_DAY = 1_440.0


@dataclass(frozen=True)
class ThickPoolThresholds:
    min_liq_usd: float = 150_000.0
    min_pool_age_days: float = 7.0
    r3_vol_thr: float = 2.5
    r3_rsi_lo: float = 30.0
    r3_rsi_hi: float = 70.0
    min_jev_strength: float = 0.15
    full_size_pct: float = 1.0
    ordinary_size_pct: float = 0.05
    strict_min_liq_usd: float | None = None


@dataclass(frozen=True)
class EntrySize:
    size_pct: float
    tier: str
    r3: int
    blockers: tuple[str, ...]


def thresholds_from_config() -> ThickPoolThresholds:
    return ThickPoolThresholds(
        min_liq_usd=float(de_config.THICK_MIN_LIQ_USD),
        min_pool_age_days=float(de_config.THICK_MIN_POOL_AGE_DAYS),
        r3_vol_thr=float(de_config.THICK_R3_VOL_THR),
        r3_rsi_lo=float(de_config.THICK_R3_RSI_LO),
        r3_rsi_hi=float(de_config.THICK_R3_RSI_HI),
        min_jev_strength=float(de_config.THICK_MIN_JEV_STRENGTH),
        full_size_pct=float(de_config.THICK_POOL_SIZE_PCT),
        ordinary_size_pct=float(de_config.ORDINARY_SIZE_PCT),
        strict_min_liq_usd=de_config.THICK_STRICT_MIN_LIQ_USD,
    )


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


def _lookup(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """First present key, parsed. Absent keys are skipped; a present invalid value is missing."""
    for key in keys:
        if key in row:
            return _finite(row.get(key))
    return None


def r3_reject(
    volatility_24h: float | None,
    change_1h: float | None,
    rsi_14: float | None,
    *,
    vol_thr: float = 2.5,
    rsi_lo: float = 30.0,
    rsi_hi: float = 70.0,
) -> bool:
    """True when R3 fires (entry_r3=1). False is R3=0.

    Matches ``eval_r3``: vol must be present and strictly above the threshold,
    and at least one of a negative 1h change or an RSI extreme must be present.
    """
    if volatility_24h is None or not (volatility_24h > vol_thr):
        return False
    weak_chg = change_1h is not None and change_1h < 0
    rsi_extreme = rsi_14 is not None and (rsi_14 < rsi_lo or rsi_14 >= rsi_hi)
    return bool(weak_chg or rsi_extreme)


def pool_age_days(row: dict[str, Any], now: datetime) -> float | None:
    """Days since pool creation. None when the row has no usable age."""
    if "pool_age_days" in row:
        return _finite(row.get("pool_age_days"))
    if "pool_age_min" in row:
        minutes = _finite(row.get("pool_age_min"))
        if minutes is None:
            return None
        return minutes / _MIN_PER_DAY
    if "pair_created_at" in row:
        created = row.get("pair_created_at")
    elif "pairCreatedAt" in row:
        created = row.get("pairCreatedAt")
    else:
        return None
    created_ms = coerce_pair_created_at_ms(created)
    if created_ms is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_ms = now.timestamp() * 1000.0
    return (now_ms - created_ms) / _MS_PER_DAY


def ordinary_size_pct(floor_pct: float, cap: float) -> float:
    """Keep a smaller explicit floor. Never raise an ordinary buy above ``cap``."""
    ceiling = _finite(cap)
    if ceiling is None or ceiling <= 0:
        ceiling = 0.05
    floor = _finite(floor_pct)
    if floor is None or floor <= 0:
        return ceiling
    return min(floor, ceiling)


def resolve_entry_size(
    row: dict[str, Any],
    *,
    floor_pct: float,
    now: datetime | None = None,
    thresholds: ThickPoolThresholds | None = None,
) -> EntrySize:
    """Return thick-pool full size, or the ordinary floor (capped)."""
    th = thresholds if thresholds is not None else thresholds_from_config()
    floor = ordinary_size_pct(floor_pct, th.ordinary_size_pct)
    when = now or datetime.now(timezone.utc)
    blockers: list[str] = []

    liq = _lookup(row, ("liquidity_usd", "liq_usd"))
    if liq is None:
        blockers.append("liq_missing")
    elif liq < th.min_liq_usd:
        blockers.append("liq_below_thick")
    elif th.strict_min_liq_usd is not None and liq < th.strict_min_liq_usd:
        blockers.append("liq_below_strict")

    age = pool_age_days(row, when)
    if age is None:
        blockers.append("pool_age_missing")
    elif age < th.min_pool_age_days:
        blockers.append("pool_age_below")

    vol = _lookup(row, ("volatility_24h", "vol_24h"))
    change_1h = _lookup(row, ("change_1h", "chg_1h"))
    rsi = _lookup(row, ("rsi_14", "rsi"))
    rejected = r3_reject(
        vol,
        change_1h,
        rsi,
        vol_thr=th.r3_vol_thr,
        rsi_lo=th.r3_rsi_lo,
        rsi_hi=th.r3_rsi_hi,
    )
    if rejected:
        blockers.append("r3")

    jev = _lookup(row, ("jev_strength",))
    if jev is None:
        blockers.append("jev_missing")
    elif jev < th.min_jev_strength:
        blockers.append("jev_below")

    if blockers:
        return EntrySize(size_pct=floor, tier="ordinary", r3=int(rejected), blockers=tuple(blockers))

    full = _finite(th.full_size_pct)
    if full is None or full <= 0:
        full = 1.0
    return EntrySize(size_pct=full, tier="thick_pool", r3=0, blockers=())


def buy_size_fields(
    row: dict[str, Any],
    *,
    floor_pct: float,
    now: datetime | None = None,
    thresholds: ThickPoolThresholds | None = None,
) -> dict[str, Any]:
    """Fields merged onto a buy signal. Does not admit or reject the buy."""
    decision = resolve_entry_size(row, floor_pct=floor_pct, now=now, thresholds=thresholds)
    if decision.tier == "thick_pool":
        log.info(
            "thick_pool size_pct=%s token=%s liq=%s jev=%s r3=0",
            decision.size_pct,
            str(row.get("token_address") or "")[:16],
            _lookup(row, ("liquidity_usd", "liq_usd")),
            _lookup(row, ("jev_strength",)),
        )
    return {
        "size_pct": decision.size_pct,
        "size_tier": decision.tier,
        "entry_r3": decision.r3,
        "size_blockers": ",".join(decision.blockers),
    }
