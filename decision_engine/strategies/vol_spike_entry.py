"""R4 volume_1h spike entry → race_vol_spike_entry.

Independent entry (not selfbuilt):
  volume_1h >= 3 × (volume_h24/24)   # proxy for 24×1h median when series absent
  change_1h in (0, +40%]
  pool_age >= 1d
  liq_usd >= 50k
Missing volume/age/liq/change → skip (never invent).
Buy-only; exits with execution peer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields, pool_age_days

log = get_logger("decision_engine.vol_spike_entry")

RULE_VERSION = "vol_spike_entry_r4_2026-10-30"
STRATEGY_ID = "vol_spike_entry_v1"

MIN_LIQ_USD = 50_000.0
MIN_POOL_AGE_DAYS = 1.0
SPIKE_MULT = 3.0
CHANGE_1H_LO = 0.0  # exclusive
CHANGE_1H_HI = 40.0  # inclusive (% units as stored)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def evaluate_vol_spike(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
    min_liq_usd: float = MIN_LIQ_USD,
    min_pool_age_days: float = MIN_POOL_AGE_DAYS,
    spike_mult: float = SPIKE_MULT,
) -> tuple[bool, str | None, dict[str, Any]]:
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    liq = _finite(row.get("liquidity_usd"))
    age = pool_age_days(row, when)
    v1 = _finite(row.get("volume_1h"))
    v24 = _finite(row.get("volume_h24"))
    c1 = _finite(row.get("change_1h"))

    baseline = None if v24 is None else (v24 / 24.0)
    extras: dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "liquidity_usd": liq,
        "pool_age_days": None if age is None else round(age, 4),
        "volume_1h": v1,
        "volume_h24": v24,
        "volume_1h_baseline": None if baseline is None else round(baseline, 6),
        "change_1h": c1,
        "spike_proxy": "volume_h24_div_24",
    }

    if liq is None:
        return False, "liq_missing", extras
    if liq < min_liq_usd:
        return False, "liq_below", extras
    if age is None:
        return False, "pool_age_missing", extras
    if age < min_pool_age_days:
        return False, "pool_age_below", extras
    if v1 is None:
        return False, "volume_1h_missing", extras
    if v24 is None or baseline is None:
        return False, "volume_h24_missing", extras
    if baseline <= 0:
        return False, "volume_baseline_non_positive", extras
    if v1 < spike_mult * baseline:
        return False, "volume_spike_below", extras
    if c1 is None:
        return False, "change_1h_missing", extras
    if not (CHANGE_1H_LO < c1 <= CHANGE_1H_HI):
        return False, "change_1h_out_of_band", extras
    return True, None, extras


class VolSpikeEntryStrategy(BaseStrategy):
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
            ok, why, extras = evaluate_vol_spike(row, now=now)
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
                entry_r3=sized["entry_r3"],
                size_blockers=sized["size_blockers"],
                reason=(
                    f"vol_spike {RULE_VERSION} "
                    f"v1h={extras['volume_1h']:.2f} base={extras['volume_1h_baseline']:.2f} "
                    f"ch1h={extras['change_1h']:.4f} liq={extras['liquidity_usd']:.0f}"
                ),
                rule_version=RULE_VERSION,
                volume_1h=extras["volume_1h"],
                volume_h24=extras["volume_h24"],
                change_1h=extras["change_1h"],
                vol_spike_sidebook="r4",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
