"""True AND: swing_established ∧ selfbuilt_momentum_a → race_and_swing_selfbuilt.

Missing any required field from either admission → *_missing / component reject skip.
No OR. Buy-only; exits with execution peer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields
from decision_engine.strategies.selfbuilt_momentum_a import evaluate_candidate_a
from decision_engine.strategies.swing_established import evaluate_swing_established

log = get_logger("decision_engine.and_swing_selfbuilt")

RULE_VERSION = "and_swing_selfbuilt_2026-11-01"
STRATEGY_ID = "and_swing_selfbuilt_v1"


def evaluate_and_swing_selfbuilt(
    row: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, str | None, dict[str, Any]]:
    ok_s, why_s, ex_s = evaluate_swing_established(row, now=now)
    extras = dict(ex_s)
    extras["rule_version"] = RULE_VERSION
    extras["leg_swing"] = bool(ok_s)
    extras["leg_swing_why"] = why_s
    if not ok_s:
        return False, why_s, extras
    ok_a, why_a, ex_a = evaluate_candidate_a(row, now=now)
    extras.update(ex_a)
    extras["rule_version"] = RULE_VERSION
    extras["leg_swing"] = True
    extras["leg_swing_why"] = None
    extras["leg_selfbuilt_a"] = bool(ok_a)
    extras["leg_selfbuilt_a_why"] = why_a
    if not ok_a:
        return False, why_a, extras
    return True, None, extras


class AndSwingSelfbuiltStrategy(BaseStrategy):
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
            ok, why, extras = evaluate_and_swing_selfbuilt(row, now=now)
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
                    f"and_swing_selfbuilt {RULE_VERSION} "
                    f"liq={extras.get('liquidity_usd')} age_d={extras.get('pool_age_days')} "
                    f"rsi={extras.get('rsi_14')} c24={extras.get('change_24h')} "
                    f"ch1h={extras.get('change_1h')}"
                ),
                rule_version=RULE_VERSION,
                liquidity_usd=extras.get("liquidity_usd"),
                pool_age_days=extras.get("pool_age_days"),
                rsi_14=extras.get("rsi_14"),
                change_24h=extras.get("change_24h"),
                change_1h=extras.get("change_1h"),
                volatility_24h=extras.get("volatility_24h"),
                and_sidebook="swing_selfbuilt",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
