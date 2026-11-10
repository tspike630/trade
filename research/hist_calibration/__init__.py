"""Historical archive staging and out-of-sample calibration.

Download, analyze, CCXT upsert, and activation are separate commands.
The default path does not write a live database and does not construct
tradeable on-chain cuts.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.1.0"
