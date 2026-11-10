"""Validation-only execution.executors package init. Not a deploy file.

Production execution/executors/__init__.py imports PaperExecutor and
LiveMCPBroker. PaperExecutor imports the paper ledger. The acceptance tests
need resolve_entry_size and buy_exposure_block, and binance_spot must be
imported before binance_spot_protect because those two modules import each
other. This init follows that order and does not import the paper ledger,
the MCP broker, or an order client.
"""

from execution.executors.binance_spot_gate import buy_exposure_block
from execution.executors.binance_spot import BinanceSpotAdapter

__all__ = ["BinanceSpotAdapter", "buy_exposure_block"]
