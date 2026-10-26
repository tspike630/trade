"""Self-built momentum candidate A (parallel to jev_strength_v1).

Rule locked by five-layer r2:
``exports/reports/selfbuilt_entry_signal_hypothesis_DRAFT_2026-10-23_r2.md``

Admission (all required):
  liq >= 50k USD
  pool_age >= 1 day
  volatility_24h present AND R3 == 0
  change_1h > 0
  rsi_14 in [40, 65)

R3=1 when volatility_24h > 2.5 AND (change_1h < 0 OR rsi_14 < 30 OR rsi_14 >= 70).
Missing vol → not admitted (stricter than thick-pool sizing's r3_reject alone).

Buy-only; exits stay with the execution layer (TP10 / SL5 / h60).
Does not read or rewrite JEV stance / strength gates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import (
    buy_size_fields,
    pool_age_days,
    r3_reject,
)

log = get_logger("decision_engine.selfbuilt_momentum_a")

RULE_VERSION = "selfbuilt_momentum_a_r2_2026-10-23"
STRATEGY_ID = "selfbuilt_momentum_a_v1"

MIN_LIQ_USD = 50_000.0
MIN_POOL_AGE_DAYS = 1.0
RSI_LO = 40.0  # inclusive
RSI_HI = 65.0  # exclusive
R3_VOL_THR = 2.5
R3_RSI_LO = 30.0
R3_RSI_HI = 70.0


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def evaluate_candidate_a(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Return (admitted, reject_reason|None, extras).

    Reject reasons are stable strings for tests / logs.
    """
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    liq = _finite(row.get("liquidity_usd"))
    age = pool_age_days(row, when)
    vol = _finite(row.get("volatility_24h"))
    change_1h = _finite(row.get("change_1h"))
    rsi = _finite(row.get("rsi_14"))

    extras: dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "liquidity_usd": liq,
        "pool_age_days": None if age is None else round(age, 4),
        "volatility_24h": vol,
        "change_1h": change_1h,
        "rsi_14": rsi,
        "entry_r3": None,
    }

    if liq is None:
        return False, "liq_missing", extras
    if liq < MIN_LIQ_USD:
        return False, "liq_below", extras

    if age is None:
        return False, "pool_age_missing", extras
    if age < MIN_POOL_AGE_DAYS:
        return False, "pool_age_below", extras

    if vol is None:
        return False, "vol_missing", extras

    if change_1h is None or rsi is None:
        return False, "momentum_incomplete", extras

    is_r3 = r3_reject(
        vol,
        change_1h,
        rsi,
        vol_thr=R3_VOL_THR,
        rsi_lo=R3_RSI_LO,
        rsi_hi=R3_RSI_HI,
    )
    extras["entry_r3"] = int(is_r3)
    if is_r3:
        return False, "r3", extras

    if not (change_1h > 0):
        return False, "change_1h_non_positive", extras
    if not (RSI_LO <= rsi < RSI_HI):
        return False, "rsi_out_of_band", extras

    return True, None, extras


class SelfbuiltMomentumAStrategy(BaseStrategy):
    """Parallel momentum-A candidate; strategy_id is attributable in decision_log."""

    strategy_id = STRATEGY_ID

    def __init__(self, position_pct: float = 0.05, **kwargs: Any):
        super().__init__(position_pct=position_pct, **kwargs)
        self.position_pct = float(position_pct)
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        now = datetime.now(timezone.utc)
        for row in hot:
            ok, why, extras = evaluate_candidate_a(row, now=now)
            if not ok:
                self.rejected[why or "unknown"] = self.rejected.get(why or "unknown", 0) + 1
                continue
            hits += 1
            sized = buy_size_fields(row, floor_pct=self.position_pct, now=now)
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=float(sized["size_pct"]),
                size_tier=sized["size_tier"],
                entry_r3=0,
                size_blockers=sized["size_blockers"],
                reason=(
                    f"selfbuilt_a {RULE_VERSION} "
                    f"liq={extras['liquidity_usd']:.0f} "
                    f"age_d={extras['pool_age_days']} "
                    f"ch1h={extras['change_1h']:.4f} "
                    f"rsi={extras['rsi_14']:.2f} "
                    f"vol24h={extras['volatility_24h']:.4f} R3=0"
                ),
                rule_version=RULE_VERSION,
                change_1h=extras["change_1h"],
                rsi_14=extras["rsi_14"],
                volatility_24h=extras["volatility_24h"],
                pool_age_days=extras["pool_age_days"],
                selfbuilt_sidebook="momentum_a",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id,
            len(hot),
            hits,
            len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
