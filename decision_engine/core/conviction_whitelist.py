"""Core Real-Demand & Organic Willingness-to-Pay Whitelist (12 Institutional/Consumer Pillars).

Screens symbols based on verified non-subsidized user fees, daily active users (DAU),
stablecoin payment settlement utility, and real-world financial stickiness:
- Tier S (Global Settlement & Exchange Pillars): BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, TRXUSDT
- Tier A (Monopoly Cash-Flow Apps & RWA Infrastructure): AAVEUSDT, UNIUSDT, LINKUSDT, LDOUSDT, ONDOUSDT, ENAUSDT, JUPUSDT (plus RAYUSDT alias)

Usage in 5-State Router:
- When ``ENFORCE_REAL_DEMAND_WHITELIST=1`` (or ``row["enforce_real_demand_whitelist"] == True``):
  - State 1 (1h Mean-Reversion Dip Buying) STRICTLY restricts buys to Whitelist symbols only
    (never catch falling knives on zero-revenue narrative/meme tokens).
  - State 2/3 (Squeeze / Bull Breakout) stamps ``conviction_tier`` ("S" / "A" / "SPECULATIVE")
    and caps non-whitelist speculative tokens to Stage 1 base size (blocking 85%-95% pyramid exposure).
"""

from __future__ import annotations

import os
from typing import Any

WHITELIST_ENV = "ENFORCE_REAL_DEMAND_WHITELIST"

TIER_S_PILLARS: frozenset[str] = frozenset(
    {
        "BTCUSDT",   # Global sovereign & ETF store-of-value reserve
        "ETHUSDT",   # #1 financial & RWA settlement layer ($2.0B/quarter ecosystem fees)
        "BNBUSDT",   # #1 CEX fee-discount utility + 3.2M DAU chain
        "SOLUSDT",   # High-throughput retail payment & trading rail (2.5M DAU)
        "TRXUSDT",   # #1 emerging-market daily USDT payroll & remittance rail (4.0M DAU)
    }
)

TIER_A_CASHFLOW_APPS: frozenset[str] = frozenset(
    {
        "AAVEUSDT",  # #1 decentralized credit & lending market
        "UNIUSDT",   # #1 decentralized spot FX/swap venue (~2/3 ETH DEX share)
        "LINKUSDT",  # Monopoly oracle & institutional cross-chain settlement middleware
        "LDOUSDT",   # #1 liquid staking infrastructure (~31% ETH staked backbone)
        "ONDOUSDT",  # #1 tokenized US Treasuries & equities (RWA) issuer
        "ENAUSDT",   # Top revenue-generating synthetic dollar & basis yield protocol
        "JUPUSDT",   # #1 Solana retail swap aggregator & perps fee router
        "RAYUSDT",   # Core Solana AMM liquidity & fee engine
    }
)

REAL_DEMAND_TOP12_WHITELIST: frozenset[str] = TIER_S_PILLARS | TIER_A_CASHFLOW_APPS


def normalize_spot_symbol(symbol: object) -> str:
    s = str(symbol or "").strip().upper().replace("/", "").replace("-", "")
    if s and not s.endswith("USDT") and not s.startswith("0X") and len(s) <= 8:
        return f"{s}USDT"
    return s


def get_conviction_tier(symbol: object) -> str:
    sym = normalize_spot_symbol(symbol)
    if sym in TIER_S_PILLARS:
        return "S"
    if sym in TIER_A_CASHFLOW_APPS:
        return "A"
    return "SPECULATIVE"


def is_whitelist_enforced(row: dict[str, Any] | None = None) -> bool:
    if isinstance(row, dict) and "enforce_real_demand_whitelist" in row:
        return bool(row["enforce_real_demand_whitelist"])
    val = os.environ.get(WHITELIST_ENV, "").strip().lower()
    return val in ("1", "true", "yes", "on")
