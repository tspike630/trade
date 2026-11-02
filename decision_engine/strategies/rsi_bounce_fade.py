"""Invert rsi_bounce → race_rsi_bounce_fade.

Original: RSI oversold (<=30) AND change_5m > 0 (catch bounce).
True polarity flip (buy-only): RSI overbought (>=70) AND change_5m < 0
(fade weakening winners / overbought dump confirmation).
Missing rsi_14 / change_5m → skip.
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.rsi_bounce_fade")

RULE_VERSION = "rsi_bounce_fade_v1_2026-11-01"
STRATEGY_ID = "rsi_bounce_fade_v1"


class RsiBounceFadeStrategy(BaseStrategy):
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
        self.max_change_5m = float(max_change_5m)  # require c5 < this (default 0)
        self.position_pct = float(position_pct)
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        for row in hot:
            rsi = row.get("rsi_14")
            if rsi is None:
                self.rejected["rsi_14_missing"] = self.rejected.get("rsi_14_missing", 0) + 1
                continue
            try:
                r = float(rsi)
            except (TypeError, ValueError):
                self.rejected["rsi_14_parse"] = self.rejected.get("rsi_14_parse", 0) + 1
                continue
            if r < self.overbought:
                self.rejected["rsi_not_overbought"] = (
                    self.rejected.get("rsi_not_overbought", 0) + 1
                )
                continue
            try:
                c5 = float(row["change_5m"]) if row.get("change_5m") is not None else None
            except (TypeError, ValueError):
                c5 = None
            if c5 is None:
                self.rejected["change_5m_missing"] = (
                    self.rejected.get("change_5m_missing", 0) + 1
                )
                continue
            # Invert bounce: require non-positive / weakening move
            if c5 >= self.max_change_5m:
                self.rejected["change_5m_not_weak"] = (
                    self.rejected.get("change_5m_not_weak", 0) + 1
                )
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
                    f"rsi_bounce_fade {RULE_VERSION} "
                    f"rsi={r:.1f}>={self.overbought:.0f} c5={c5:+.3f}<{self.max_change_5m}"
                ),
                rule_version=RULE_VERSION,
                rsi_14=r,
                change_5m=c5,
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
