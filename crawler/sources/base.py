"""Base interface for multi-source supplements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceResult:
    """Normalized items for upsert into tokens/pairs."""

    source_name: str
    tokens: list[dict[str, Any]] = field(default_factory=list)
    # each token: {chainId, tokenAddress, ...profile fields}
    pairs: list[dict[str, Any]] = field(default_factory=list)
    # each pair: DexScreener-like pair object
    notes: str = ""


class TokenSource(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch(self) -> SourceResult:
        """Fetch supplemental data (may be empty / no-op)."""
