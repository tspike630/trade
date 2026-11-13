"""Core names taken from the public-feed symbol tuples.

``scripts/fetch_public_derivatives_and_counterparty.py`` lists
``DEFAULT_SPOT_MICRO_SYMBOLS``. This module repeats that list so a box
run does not import the collector. It is not a new universe and it does
not add names.
"""

from __future__ import annotations

from typing import Any

# Same order as DEFAULT_SPOT_MICRO_SYMBOLS on main ab2d2eb.
SPOT_MICRO_CANON: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "SUIUSDT",
    "APTUSDT",
    "FETUSDT",
    "WLDUSDT",
    "PEPEUSDT",
    "ENAUSDT",
    "ONDOUSDT",
    "ARBUSDT",
    "OPUSDT",
    "TIAUSDT",
)

# Persistent names that also appear on the USD-M metrics/bookDepth feeds
# and on the spot micro list. 1000PEPE is the perp alias and is not used
# as a spot kline symbol here.
PERP_AND_SPOT: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "SUIUSDT",
)


def venue_markets(binance_symbol: str) -> dict[str, Any]:
    """Map one Binance USDT id onto public venues.

    Coinbase spot is USD, not USDT. The quote is labeled and is not treated
    as the same series as a USDT pair.
    """
    symbol = str(binance_symbol or "").strip().upper()
    if not symbol.endswith("USDT") or symbol == "USDT":
        raise ValueError(f"not a Binance USDT id: {binance_symbol!r}")
    base = symbol[: -len("USDT")]
    coinbase = f"{base}/USD"
    return {
        "binance_symbol": symbol,
        "base": base,
        "binance_spot_quote": "USDT",
        "okx": {"symbol": f"{base}/USDT", "quote": "USDT"},
        "bybit": {"symbol": f"{base}/USDT", "quote": "USDT"},
        "coinbase": {
            "symbol": coinbase,
            "quote": "USD",
            "comparable_to_usdt": False,
            "basis_label": "coinbase_usd_vs_binance_usdt",
        },
    }
