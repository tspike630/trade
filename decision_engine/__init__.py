"""Decision engine — Step 1 dry-run + Step 2 backtest framework.

Independent of indicators / crawl / live_trade.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Monorepo layout: this package sits at the repo root, while logging_setup
# and config live in crawler/. A copy nested under crawler/ already has
# those modules next to it and does not need the extra path entry.
_PKG_PARENT = Path(__file__).resolve().parents[1]
if not (_PKG_PARENT / "logging_setup.py").is_file():
    _crawler = _PKG_PARENT / "crawler"
    if (_crawler / "logging_setup.py").is_file():
        _crawler_s = str(_crawler)
        if _crawler_s not in sys.path:
            sys.path.insert(0, _crawler_s)

__all__ = ["__version__"]
__version__ = "0.2.0-step2"
