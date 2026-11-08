"""N9 liq ladder sizing旁证叠滤 → race_filt_liq_ladder.

selfbuilt full pass THEN:
  liq >= 200k → emit (full shadow size note)
  50k <= liq < 200k → emit with size_ladder_note=half (does NOT rewrite size_pct gate)
  liq < 50k → skip
Missing liq → skip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields
from decision_engine.strategies.selfbuilt_momentum_a import evaluate_candidate_a

log = get_logger("decision_engine.filt_liq_ladder")

RULE_VERSION = "filt_liq_ladder_n9_2026-10-30"
STRATEGY_ID = "filt_liq_ladder_v1"

TIER_FULL = 200_000.0
TIER_HALF = 50_000.0


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


def evaluate_liq_ladder(
    row: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, str | None, dict[str, Any]]:
    ok, why, extras = evaluate_candidate_a(row, now=now)
    extras = dict(extras)
    extras["rule_version"] = RULE_VERSION
    liq = _finite(row.get("liquidity_usd"))
    extras["liquidity_usd"] = liq
    extras["liq_ladder_tier"] = None
    extras["shadow_size_note"] = None
    if not ok:
        return False, why, extras
    if liq is None:
        return False, "liq_missing", extras
    if liq < TIER_HALF:
        return False, "liq_ladder_skip_thin", extras
    if liq >= TIER_FULL:
        extras["liq_ladder_tier"] = "full_ge_200k"
        extras["shadow_size_note"] = "full"
    else:
        extras["liq_ladder_tier"] = "half_50_200k"
        extras["shadow_size_note"] = "should_halve"
    return True, None, extras


class FiltLiqLadderStrategy(BaseStrategy):
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
            ok, why, extras = evaluate_liq_ladder(row, now=now)
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
                    f"filt_liq_ladder {RULE_VERSION} "
                    f"tier={extras['liq_ladder_tier']} shadow={extras['shadow_size_note']} "
                    f"liq={extras['liquidity_usd']:.0f}"
                ),
                rule_version=RULE_VERSION,
                liq_ladder_tier=extras["liq_ladder_tier"],
                shadow_size_note=extras["shadow_size_note"],
                liquidity_usd=extras["liquidity_usd"],
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
