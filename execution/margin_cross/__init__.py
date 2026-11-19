"""Isolated Binance cross-margin execution track.

Live network writes are disabled in this package. Nothing here is imported by
``execution``, ``execution.executors``, the spot gateway, or the decision
engine. Construct ``CrossMarginTrack`` only from an explicit caller.

The numeric risk limits are an operator contract. This package does not treat
any limit as approved, and it does not read the five-state spot sizing rules.
"""

from execution.margin_cross.errors import (
    LiveNetworkDisabled,
    MarginExchangeError,
    MarginTransportTimeout,
)
from execution.margin_cross.risk import (
    OFFLINE_FIXTURE_STRATEGY_ID,
    PURPOSE_CROSS_MARGIN_TRACK,
    REQUIRED_UNAPPROVED_LABEL,
)
from execution.margin_cross.track import CrossMarginTrack, TrackResult

__all__ = [
    "CrossMarginTrack",
    "LiveNetworkDisabled",
    "MarginExchangeError",
    "MarginTransportTimeout",
    "OFFLINE_FIXTURE_STRATEGY_ID",
    "PURPOSE_CROSS_MARGIN_TRACK",
    "REQUIRED_UNAPPROVED_LABEL",
    "TrackResult",
]
