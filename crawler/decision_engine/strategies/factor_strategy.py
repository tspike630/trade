"""FactorStrategy: strategy = buy-factor chain × sell-factor chain.

Keeps BaseStrategy contract (on_tick -> list[signals]) so engine/backtest/
registry stay untouched. Buy chain: first factor that hits claims the token.
Sell chain: only runs when data provides open_positions (dry-run: none -> noop).
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.factors.base import FactorBuyBase, FactorSellBase
from decision_engine.factors.buy import BUY_FACTOR_REGISTRY
from decision_engine.factors.sell import SELL_FACTOR_REGISTRY
from decision_engine.strategies.base_strategy import BaseStrategy

log = get_logger("decision_engine.factor_strategy")


class FactorStrategy(BaseStrategy):
    """Strategy = buy-factor chain + sell-factor chain. strategy_id is set at registration."""

    def __init__(
        self,
        *,
        strategy_id: str,
        buy_factors: list[str],
        sell_factors: list[str] | None = None,
        buy_params: dict[str, dict[str, Any]] | None = None,
        sell_params: dict[str, dict[str, Any]] | None = None,
        position_pct: float = 0.05,
        **kwargs: Any,
    ):
        super().__init__(strategy_id=strategy_id, position_pct=position_pct, **kwargs)
        self.strategy_id = strategy_id
        # Legacy strategies take thresholds as constructor kwargs
        # (min_jev_strength, min_change_5m, ...). Forward them onto each buy
        # factor so get_strategy("factor_jev_strength_v1", min_jev_strength=0.15)
        # matches jev_strength_v1. Explicit buy_params win.
        forwarded = dict(kwargs)
        self._buy_factors: list[FactorBuyBase] = []
        for name in buy_factors:
            params = dict(forwarded)
            params.update((buy_params or {}).get(name, {}))
            params.setdefault("position_pct", position_pct)
            self._buy_factors.append(BUY_FACTOR_REGISTRY[name](**params))
        self._sell_factors: list[FactorSellBase] = []
        for name in (sell_factors or ["noop"]):
            params = dict((sell_params or {}).get(name, {}))
            self._sell_factors.append(SELL_FACTOR_REGISTRY[name](**params))

    @property
    def buy_factor_names(self) -> list[str]:
        return [f.factor_name for f in self._buy_factors]

    @property
    def sell_factor_names(self) -> list[str]:
        return [f.factor_name for f in self._sell_factors]

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        hot = data.get("hot_tokens") or []
        for row in hot:
            cand = self._first_hit(row)
            if cand is None:
                continue
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=float(cand["size_pct"]),
                reason=str(cand["reason"]),
                **{k: v for k, v in cand.items() if k not in ("size_pct", "reason")},
            )
        # Sell chain: current dry-run has no positions → no-op (behavior equivalent).
        positions = data.get("open_positions") or []
        for pos in positions:
            cand = self._first_sell(pos, data)
            if cand is None:
                continue
            self.sell(
                chain_id=str(pos.get("chain_id") or ""),
                token_address=str(pos.get("token_address") or ""),
                size_pct=float(cand["size_pct"]),
                reason=str(cand["reason"]),
            )
        signals = self.drain()
        log.info(
            "%s buy_factors=%s sell_factors=%s scanned=%s signals=%s",
            self.strategy_id,
            self.buy_factor_names,
            self.sell_factor_names,
            len(hot),
            len(signals),
        )
        return signals

    def _first_hit(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Buy chain: the first factor that hits owns the token."""
        for bf in self._buy_factors:
            try:
                cand = bf.fit_tick(row)
            except Exception:  # noqa: BLE001
                cand = None
            if cand is not None:
                return cand
        return None

    def _first_sell(
        self, position: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        for sf in self._sell_factors:
            try:
                cand = sf.fit_position(position, data)
            except Exception:  # noqa: BLE001
                cand = None
            if cand is not None:
                return cand
        return None
