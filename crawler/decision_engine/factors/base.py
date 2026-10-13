"""Buy / sell factor base classes (abu-inspired, tick-level).

- FactorBuyBase.fit_tick(row) -> dict | None   # row = one hot_tokens row
- FactorSellBase.fit_position(position, data) -> dict | None
Returned dicts are *candidates*; FactorStrategy turns them into standard signals.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class FactorBase(ABC):
    factor_name: str = "base"

    def __init__(self, **params: Any):
        # Keep every parameter (abu kwargs semantics): thresholds live in _init_self.
        self.params = dict(params)
        self._init_self(**params)

    def _init_self(self, **kwargs: Any) -> None:
        """Subclass hook: pull parameters out of kwargs and coerce types."""
        pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.factor_name} params={self.params}>"


class FactorBuyBase(FactorBase):
    """Buy factor: one token row in, buy candidate dict or None out."""

    def __init__(self, **params: Any):
        super().__init__(**params)

    @abstractmethod
    def fit_tick(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """row = one hot_tokens row.

        Returned dict must include:
          size_pct: float          # target position (e.g. 0.05)
          reason: str              # same format as the legacy strategy reason
        Extra fields (jev_strength, etc.) are merged into the signal.
        Return None on a miss.
        """


class FactorSellBase(FactorBase):
    """Sell factor: one open position + this tick's data, sell candidate or None."""

    def __init__(self, **params: Any):
        super().__init__(**params)

    @abstractmethod
    def fit_position(
        self, position: dict[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """position fields: chain_id, token_address, size_pct, opened_ts (epoch sec), optional peak_price.

        data may provide: now_ts (epoch sec), prices={token_address: current_price}.
        Return a sell candidate dict (size_pct / reason required) or None.
        """
