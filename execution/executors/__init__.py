"""Execution adapters (paper, gated Binance spot, and gated USDT-M perp short)."""

from __future__ import annotations

from execution.executors.binance_mcp_broker import LiveMCPBroker
from execution.executors.binance_perp_short import (
    BinancePerpShortAdapter,
    PerpShortExecutionReceipt,
)
from execution.executors.binance_spot import BinanceSpotAdapter
from execution.executors.paper_executor import PaperExecutor

__all__ = [
    "BinancePerpShortAdapter",
    "BinanceSpotAdapter",
    "LiveMCPBroker",
    "PaperExecutor",
    "PerpShortExecutionReceipt",
]
