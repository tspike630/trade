"""Invert factor_tech_boll_and → race_tech_boll_fade.

True AND of inverted legs (禁 OR):
  tech_fade: change_5m <= -min_change_5m AND volume_strength >= min
  boll_fade: price < boll_lower (lower-band break; invert of upper breakout)
Missing any required field → skip.
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.tech_boll_fade")

RULE_VERSION = "tech_boll_fade_v1_2026-11-01"
STRATEGY_ID = "tech_boll_fade_v1"


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


class TechBollFadeStrategy(BaseStrategy):
    strategy_id = STRATEGY_ID

    def __init__(
        self,
        min_change_5m: float = 2.0,
        min_volume_strength: float = 0.5,
        position_pct: float = 0.05,
        **kwargs: Any,
    ):
        super().__init__(
            min_change_5m=min_change_5m,
            min_volume_strength=min_volume_strength,
            position_pct=position_pct,
            **kwargs,
        )
        self.min_change_5m = float(min_change_5m)
        self.min_volume_strength = float(min_volume_strength)
        self.position_pct = float(position_pct)
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        thr = -self.min_change_5m
        for row in hot:
            c5 = _finite(row.get("change_5m"))
            vs = _finite(row.get("volume_strength"))
            if c5 is None:
                self.rejected["change_5m_missing"] = (
                    self.rejected.get("change_5m_missing", 0) + 1
                )
                continue
            if vs is None:
                self.rejected["volume_strength_missing"] = (
                    self.rejected.get("volume_strength_missing", 0) + 1
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

            # AND both inverted legs
            if c5 > thr:
                self.rejected["tech_leg_fail"] = self.rejected.get("tech_leg_fail", 0) + 1
                continue
            if vs < self.min_volume_strength:
                self.rejected["tech_vs_fail"] = self.rejected.get("tech_vs_fail", 0) + 1
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
                    f"tech_boll_fade {RULE_VERSION} "
                    f"c5={c5:.3f}<={thr} vs={vs:.3f} px={price:.8g}<lo={lower:.8g}"
                ),
                rule_version=RULE_VERSION,
                change_5m=c5,
                volume_strength=vs,
                boll_lower=lower,
                price_usd=price,
                and_mode="tech_fade&boll_lower",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
