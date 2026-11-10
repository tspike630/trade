"""True AND: vol_climax_mcap_band ∧ selfbuilt_momentum_a → race_and_vol_climax_selfbuilt.

Missing any required field from either admission → *_missing / component reject skip.
No OR. Buy-only; exits with execution peer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields
from decision_engine.strategies.selfbuilt_momentum_a import evaluate_candidate_a
from decision_engine.strategies.vol_climax_mcap_band import evaluate_vol_climax_mcap_band

log = get_logger("decision_engine.and_vol_climax_selfbuilt")

RULE_VERSION = "and_vol_climax_selfbuilt_2026-11-01"
STRATEGY_ID = "and_vol_climax_selfbuilt_v1"


def evaluate_and_vol_climax_selfbuilt(
    row: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, str | None, dict[str, Any]]:
    ok_v, why_v, ex_v = evaluate_vol_climax_mcap_band(row, now=now)
    extras = dict(ex_v)
    extras["rule_version"] = RULE_VERSION
    extras["leg_vol_climax"] = bool(ok_v)
    extras["leg_vol_climax_why"] = why_v
    if not ok_v:
        return False, why_v, extras
    ok_a, why_a, ex_a = evaluate_candidate_a(row, now=now)
    extras.update(ex_a)
    extras["rule_version"] = RULE_VERSION
    extras["leg_vol_climax"] = True
    extras["leg_vol_climax_why"] = None
    extras["leg_selfbuilt_a"] = bool(ok_a)
    extras["leg_selfbuilt_a_why"] = why_a
    if not ok_a:
        return False, why_a, extras
    return True, None, extras


class AndVolClimaxSelfbuiltStrategy(BaseStrategy):
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
            ok, why, extras = evaluate_and_vol_climax_selfbuilt(row, now=now)
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
                    f"and_vol_climax_selfbuilt {RULE_VERSION} "
                    f"accel={extras.get('vol_accel')} mcap={extras.get('market_cap')} "
                    f"br={extras.get('buy_ratio')} ch1h={extras.get('change_1h')} "
                    f"rsi={extras.get('rsi_14')}"
                ),
                rule_version=RULE_VERSION,
                vol_accel=extras.get("vol_accel"),
                market_cap=extras.get("market_cap"),
                buy_ratio=extras.get("buy_ratio"),
                change_1h=extras.get("change_1h"),
                rsi_14=extras.get("rsi_14"),
                volatility_24h=extras.get("volatility_24h"),
                and_sidebook="vol_climax_selfbuilt",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
