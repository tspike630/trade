"""JEV strength threshold strategy (Step 1)."""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy

log = get_logger("decision_engine.jev_strategy")


class JevStrengthStrategy(BaseStrategy):
    strategy_id = "jev_strength_v1"

    def __init__(
        self,
        min_jev_strength: float = 0.3,
        position_pct: float = 0.05,
        **kwargs: Any,
    ):
        super().__init__(
            min_jev_strength=min_jev_strength,
            position_pct=position_pct,
            **kwargs,
        )
        self.min_jev_strength = float(min_jev_strength)
        self.position_pct = float(position_pct)

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        hot = data.get("hot_tokens") or []
        above = 0
        for row in hot:
            strength = row.get("jev_strength")
            if strength is None:
                continue
            try:
                s = float(strength)
            except (TypeError, ValueError):
                continue
            if s >= self.min_jev_strength:
                above += 1
                self.buy(
                    chain_id=str(row.get("chain_id") or ""),
                    token_address=str(row.get("token_address") or ""),
                    pair_address=row.get("primary_pair_address"),
                    size_pct=self.position_pct,
                    reason=(
                        f"jev_strength={s:.4f}>={self.min_jev_strength}"
                        f" signal={row.get('jev_signal')} score={row.get('jev_score')}"
                    ),
                    jev_strength=s,
                    jev_signal=row.get("jev_signal"),
                    jev_score=row.get("jev_score"),
                )
        signals = self.drain()
        log.info(
            "jev_strength_v1 scanned=%s above_threshold=%s signals=%s min=%.3f",
            len(hot),
            above,
            len(signals),
            self.min_jev_strength,
        )
        return signals
