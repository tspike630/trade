"""Re-invert wiped race_rsi_bounce_fade → race_rsi_bounce_r90 (Yang −90% 再反做 2026-11-03).

Fade was: RSI overbought (>=70) AND change_5m < 0.
Reinvert (back to oversold bounce): RSI <= oversold (default 30) AND change_5m > 0.
AND gates only (禁 OR). Missing rsi_14 / change_5m → skip.
NEW strategy_id/purpose/module — do NOT reuse wiped rsi_bounce_fade / rsi_bounce books.
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.rsi_bounce_r90")

RULE_VERSION = "rsi_bounce_r90_v1_2026-11-03"
STRATEGY_ID = "rsi_bounce_r90_v1"


class RsiBounceR90Strategy(BaseStrategy):
    strategy_id = STRATEGY_ID

    def __init__(
        self,
        oversold: float = 30.0,
        min_change_5m: float = 0.0,
        position_pct: float = 0.05,
        **kwargs: Any,
    ):
        super().__init__(
            oversold=oversold,
            min_change_5m=min_change_5m,
            position_pct=position_pct,
            **kwargs,
        )
        self.oversold = float(oversold)
        self.min_change_5m = float(min_change_5m)  # require c5 > 0 and >= floor
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
            if r > self.oversold:
                self.rejected["rsi_not_oversold"] = (
                    self.rejected.get("rsi_not_oversold", 0) + 1
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
            # Bounce confirmation: positive change_5m (AND with oversold; 禁 OR)
            if c5 <= 0 or c5 < self.min_change_5m:
                self.rejected["change_5m_not_bounce"] = (
                    self.rejected.get("change_5m_not_bounce", 0) + 1
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
                    f"rsi_bounce_r90 {RULE_VERSION} "
                    f"rsi={r:.1f}<={self.oversold:.0f} c5={c5:+.3f}>0"
                ),
                rule_version=RULE_VERSION,
                rsi_14=r,
                change_5m=c5,
                parent_wipe_purpose="race_rsi_bounce_fade",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
