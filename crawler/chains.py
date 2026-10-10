"""P2 multi-chain enable filter."""

from __future__ import annotations

from config import ENABLED_CHAINS
from store import norm_chain

# Documented common DexScreener chainIds
DOCUMENTED_CHAINS = (
    "solana",
    "ethereum",
    "bsc",
    "base",
    "arbitrum",
    "polygon",
    "avalanche",
    "optimism",
)


def enabled_chain_set() -> set[str]:
    return {norm_chain(c) for c in ENABLED_CHAINS if c}


def chain_enabled(chain_id: str) -> bool:
    """Empty ENABLED_CHAINS list means allow all; else membership check."""
    enabled = enabled_chain_set()
    if not enabled:
        return True
    return norm_chain(chain_id) in enabled
