"""Execution adapters (paper, and the gated Binance spot arm)."""

from __future__ import annotations

from execution.executors.binance_mcp_broker import LiveMCPBroker
from execution.executors.binance_spot import BinanceSpotAdapter
from execution.executors.paper_executor import PaperExecutor

__all__ = ["BinanceSpotAdapter", "LiveMCPBroker", "PaperExecutor"]
