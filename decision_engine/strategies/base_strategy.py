"""Base strategy: on_tick + buy/sell helpers that only emit candidate dicts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BaseStrategy(ABC):
    strategy_id: str = "base"

    def __init__(self, **params: Any):
        self.params = dict(params)
        self._pending: list[dict[str, Any]] = []

    def buy(
        self,
        *,
        chain_id: str,
        token_address: str,
        size_pct: float,
        reason: str,
        pair_address: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        sig = {
            "strategy_id": self.strategy_id,
            "action": "buy",
            "chain_id": chain_id,
            "token_address": token_address,
            "pair_address": pair_address,
            "size_pct": size_pct,
            "reason": reason,
            "ts": utc_now_iso(),
            **extra,
        }
        self._pending.append(sig)
        return sig

    def sell(
        self,
        *,
        chain_id: str,
        token_address: str,
        size_pct: float,
        reason: str,
        pair_address: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        sig = {
            "strategy_id": self.strategy_id,
            "action": "sell",
            "chain_id": chain_id,
            "token_address": token_address,
            "pair_address": pair_address,
            "size_pct": size_pct,
            "reason": reason,
            "ts": utc_now_iso(),
            **extra,
        }
        self._pending.append(sig)
        return sig

    def drain(self) -> list[dict[str, Any]]:
        out = list(self._pending)
        self._pending.clear()
        return out

    @abstractmethod
    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Inspect data['hot_tokens'] etc.; return candidate signals."""
