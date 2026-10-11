"""Independent indicators processing layer (旁路).

Computes technical + funding (JEV) + sentiment (X) metrics into token_indicators.
Never mutates tokens/pairs/pair_history or the main crawl pipeline.
Funding reads jev_signals only (no HTTP).
Sentiment reads x_watch / social alerts only (no HTTP).
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.3.2-spike"
