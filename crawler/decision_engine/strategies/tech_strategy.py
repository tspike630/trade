"""Technical threshold strategy (Step 2 scaffold) — change_5m + volume_strength."""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy

log = get_logger("decision_engine.tech_strategy")


class TechMomentumStrategy(BaseStrategy):
    """Buy when short-term change and volume strength both clear thresholds."""

    strategy_id = "tech_momentum_v1"

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

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        hot = data.get("hot_tokens") or []
        hits = 0
        for row in hot:
            try:
                c5 = float(row["change_5m"]) if row.get("change_5m") is not None else None
                vs = (
                    float(row["volume_strength"])
                    if row.get("volume_strength") is not None
                    else None
                )
            except (TypeError, ValueError):
                continue
            if c5 is None or vs is None:
                continue
            if c5 >= self.min_change_5m and vs >= self.min_volume_strength:
                hits += 1
                self.buy(
                    chain_id=str(row.get("chain_id") or ""),
                    token_address=str(row.get("token_address") or ""),
                    pair_address=row.get("primary_pair_address"),
                    size_pct=self.position_pct,
                    reason=(
                        f"change_5m={c5:.3f}>={self.min_change_5m}"
                        f" volume_strength={vs:.3f}>={self.min_volume_strength}"
                    ),
                    change_5m=c5,
                    volume_strength=vs,
                )
        signals = self.drain()
        log.info(
            "tech_momentum_v1 scanned=%s hits=%s signals=%s",
            len(hot),
            hits,
            len(signals),
        )
        return signals
