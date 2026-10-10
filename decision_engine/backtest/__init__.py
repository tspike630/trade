"""Decision engine Step 2 — historical backtest framework.

Read-only on crawl/indicator tables. Writes only to an independent ledger
(backtest_log / backtest.db). Does not touch dry-run, weekly check, or
paper_trade live loops.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.2.0-step2"
