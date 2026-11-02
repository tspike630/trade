"""Invert tech_momentum → race_tech_fade.

Buy-only mean-reversion: change_5m <= -min_change_5m AND volume_strength >= min.
Missing change_5m / volume_strength → skip (never invent).
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.tech_fade")

RULE_VERSION = "tech_fade_v1_2026-11-01"
STRATEGY_ID = "tech_fade_v1"


class TechFadeStrategy(BaseStrategy):
    """Fade short-term dumps with volume confirmation (invert of TechMomentum)."""

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
            try:
                c5 = float(row["change_5m"]) if row.get("change_5m") is not None else None
                vs = (
                    float(row["volume_strength"])
                    if row.get("volume_strength") is not None
                    else None
                )
            except (TypeError, ValueError):
                self.rejected["parse_error"] = self.rejected.get("parse_error", 0) + 1
                continue
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
            if c5 > thr:
                self.rejected["change_5m_not_oversold"] = (
                    self.rejected.get("change_5m_not_oversold", 0) + 1
                )
                continue
            if vs < self.min_volume_strength:
                self.rejected["volume_strength_below"] = (
                    self.rejected.get("volume_strength_below", 0) + 1
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
                    f"tech_fade {RULE_VERSION} "
                    f"change_5m={c5:.3f}<={thr}"
                    f" volume_strength={vs:.3f}>={self.min_volume_strength}"
                ),
                rule_version=RULE_VERSION,
                change_5m=c5,
                volume_strength=vs,
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
