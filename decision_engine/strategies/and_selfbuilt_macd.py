"""P4 selfbuilt ∧ MACD true AND → race_and_selfbuilt_macd.

selfbuilt_momentum_a full admission AND macd > macd_signal and macd > 0
(single-bar cross approx; same honesty as factor_macd_cross).
Missing macd/signal → skip. No OR.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields
from decision_engine.strategies.selfbuilt_momentum_a import evaluate_candidate_a

log = get_logger("decision_engine.and_selfbuilt_macd")

RULE_VERSION = "and_selfbuilt_macd_p4_2026-10-30"
STRATEGY_ID = "and_selfbuilt_macd_v1"


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


def evaluate_and_selfbuilt_macd(
    row: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, str | None, dict[str, Any]]:
    ok, why, extras = evaluate_candidate_a(row, now=now)
    extras = dict(extras)
    extras["rule_version"] = RULE_VERSION
    macd = _finite(row.get("macd"))
    signal = _finite(row.get("macd_signal"))
    extras["macd"] = macd
    extras["macd_signal"] = signal
    if not ok:
        return False, why, extras
    if macd is None or signal is None:
        return False, "macd_missing", extras
    if not (macd > 0 and macd > signal):
        return False, "macd_not_bullish_cross", extras
    return True, None, extras


class AndSelfbuiltMacdStrategy(BaseStrategy):
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
            ok, why, extras = evaluate_and_selfbuilt_macd(row, now=now)
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
                    f"and_selfbuilt_macd {RULE_VERSION} "
                    f"macd={extras['macd']:.6g}>sig={extras['macd_signal']:.6g} "
                    f"rsi={extras.get('rsi_14')} ch1h={extras.get('change_1h')}"
                ),
                rule_version=RULE_VERSION,
                macd=extras["macd"],
                macd_signal=extras["macd_signal"],
                change_1h=extras.get("change_1h"),
                rsi_14=extras.get("rsi_14"),
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
