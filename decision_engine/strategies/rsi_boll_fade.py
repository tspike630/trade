"""Invert factor_rsi_boll_and → race_rsi_boll_fade.

True AND of inverted legs (禁 OR):
  rsi_fade: RSI >= overbought (70) AND change_5m < 0
  boll_fade: price < boll_lower
Missing any required field → skip.
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.rsi_boll_fade")

RULE_VERSION = "rsi_boll_fade_v1_2026-11-01"
STRATEGY_ID = "rsi_boll_fade_v1"


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


class RsiBollFadeStrategy(BaseStrategy):
    strategy_id = STRATEGY_ID

    def __init__(
        self,
        overbought: float = 70.0,
        max_change_5m: float = 0.0,
        position_pct: float = 0.05,
        **kwargs: Any,
    ):
        super().__init__(
            overbought=overbought,
            max_change_5m=max_change_5m,
            position_pct=position_pct,
            **kwargs,
        )
        self.overbought = float(overbought)
        self.max_change_5m = float(max_change_5m)
        self.position_pct = float(position_pct)
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        for row in hot:
            rsi = _finite(row.get("rsi_14"))
            if rsi is None:
                self.rejected["rsi_14_missing"] = self.rejected.get("rsi_14_missing", 0) + 1
                continue
            c5 = _finite(row.get("change_5m"))
            if c5 is None:
                self.rejected["change_5m_missing"] = (
                    self.rejected.get("change_5m_missing", 0) + 1
                )
                continue
            lo = row.get("boll_lower")
            if lo is None:
                lo = row.get("boll_low")
            px = row.get("price_usd")
            if px is None:
                px = row.get("price")
            if lo is None:
                self.rejected["boll_lower_missing"] = (
                    self.rejected.get("boll_lower_missing", 0) + 1
                )
                continue
            if px is None:
                self.rejected["price_missing"] = self.rejected.get("price_missing", 0) + 1
                continue
            try:
                lower = float(lo)
                price = float(px)
            except (TypeError, ValueError):
                self.rejected["boll_price_parse"] = (
                    self.rejected.get("boll_price_parse", 0) + 1
                )
                continue

            if rsi < self.overbought:
                self.rejected["rsi_leg_fail"] = self.rejected.get("rsi_leg_fail", 0) + 1
                continue
            if c5 >= self.max_change_5m:
                self.rejected["rsi_c5_leg_fail"] = (
                    self.rejected.get("rsi_c5_leg_fail", 0) + 1
                )
                continue
            if price >= lower:
                self.rejected["boll_leg_fail"] = self.rejected.get("boll_leg_fail", 0) + 1
                continue

            hits += 1
            sized = buy_size_fields(row, floor_pct=self.position_pct)
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=float(sized["size_pct"]),
                size_tier=sized["size_tier"],
                entry_r3=sized["entry_r3"],
                size_blockers=sized["size_blockers"],
                reason=(
                    f"rsi_boll_fade {RULE_VERSION} "
                    f"rsi={rsi:.1f}>={self.overbought:.0f} c5={c5:+.3f} "
                    f"px={price:.8g}<lo={lower:.8g}"
                ),
                rule_version=RULE_VERSION,
                rsi_14=rsi,
                change_5m=c5,
                boll_lower=lower,
                price_usd=price,
                and_mode="rsi_fade&boll_lower",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
