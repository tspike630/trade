"""W2 lifecycle Momentum-stage proxy → race_lifecycle_momentum.

Momentum stage proxy (no invented stage column):
  Δ24h >= 8%  ∧  Δ7d >= 12%  ∧  vol >= 1.3 × 7d-avg
Missing any required field → *_missing skip.
True change_7d / volume_7d_avg are not on hot path → honest skip when absent
(do not fabricate stage or invent 7d from 24h×7).
Buy-only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields

log = get_logger("decision_engine.lifecycle_momentum")

RULE_VERSION = "lifecycle_momentum_w2_2026-10-30"
STRATEGY_ID = "lifecycle_momentum_v1"

DELTA_24H_MIN = 8.0  # percent units as stored
DELTA_7D_MIN = 12.0
VOL_VS_7D_MIN = 1.3


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


def evaluate_lifecycle_momentum(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
    delta_24h_min: float = DELTA_24H_MIN,
    delta_7d_min: float = DELTA_7D_MIN,
    vol_vs_7d_min: float = VOL_VS_7D_MIN,
) -> tuple[bool, str | None, dict[str, Any]]:
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    c24 = _finite(row.get("change_24h"))
    c7 = _finite(row.get("change_7d"))
    # Prefer explicit 7d avg volume; do not invent from h24/7.
    vol = _finite(row.get("volume_1h"))
    if vol is None:
        vol = _finite(row.get("volume_h24"))
    vol_7d_avg = _finite(row.get("volume_7d_avg"))
    if vol_7d_avg is None:
        vol_7d_avg = _finite(row.get("vol_7d_avg"))

    extras: dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "change_24h": c24,
        "change_7d": c7,
        "volume": vol,
        "volume_7d_avg": vol_7d_avg,
        "stage_col": None,  # never invent
    }

    if c24 is None:
        return False, "change_24h_missing", extras
    if c7 is None:
        return False, "change_7d_missing", extras
    if vol is None:
        return False, "volume_missing", extras
    if vol_7d_avg is None:
        return False, "volume_7d_avg_missing", extras
    if vol_7d_avg <= 0:
        return False, "volume_7d_avg_non_positive", extras

    ratio = vol / vol_7d_avg
    extras["vol_vs_7d"] = round(ratio, 4)

    if c24 < delta_24h_min:
        return False, "change_24h_below", extras
    if c7 < delta_7d_min:
        return False, "change_7d_below", extras
    if ratio < vol_vs_7d_min:
        return False, "vol_vs_7d_below", extras
    return True, None, extras


class LifecycleMomentumStrategy(BaseStrategy):
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
        now = tick_now(data)
        for row in hot:
            ok, why, extras = evaluate_lifecycle_momentum(row, now=now)
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
                    f"lifecycle_momentum {RULE_VERSION} "
                    f"d24={extras['change_24h']:.2f} d7={extras['change_7d']:.2f} "
                    f"vol_vs_7d={extras.get('vol_vs_7d')}"
                ),
                rule_version=RULE_VERSION,
                change_24h=extras["change_24h"],
                change_7d=extras["change_7d"],
                vol_vs_7d=extras.get("vol_vs_7d"),
                lifecycle_sidebook="w2_momentum_proxy",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
