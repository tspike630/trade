"""Isolated Binance cross-margin execution track.

Nothing here is imported by ``execution``, ``execution.executors``, the spot
gateway, or the decision engine. Construct ``CrossMarginTrack`` only from an
explicit caller.

Runtime live execution stays locked unless ``CROSS_MARGIN_PARAMETERS_APPROVED=1``,
``CROSS_MARGIN_LIVE_ENABLED=1``, and ``LIVE_TRADING_DISABLED`` is absent.
"""

from execution.margin_cross.errors import (
    LiveNetworkDisabled,
    MarginExchangeError,
    MarginTransportTimeout,
)
from execution.margin_cross.http_client import (
    BinanceCrossMarginHttpClient,
    fetch_live_cross_margin_snapshot,
    sign_margin_query,
)
from execution.margin_cross.policy import (
    APPROVED_CONTRACT_LABEL,
    APPROVED_CROSS_MARGIN_STRATEGIES,
    APPROVED_DRY_RUN_MODE,
    EMERGENCY_MARGIN_LEVEL,
    LIVE_EXECUTION_MODE,
    MAX_ACCOUNT_LEVERAGE,
    MAX_EFFECTIVE_LEVERAGE,
    MIN_ENTRY_MARGIN_LEVEL,
    WARN_MARGIN_LEVEL,
)
from execution.margin_cross.risk import (
    OFFLINE_FIXTURE_STRATEGY_ID,
    PURPOSE_CROSS_MARGIN_TRACK,
    REQUIRED_UNAPPROVED_LABEL,
)
from execution.margin_cross.strategy_gate import (
    ApprovedPositionPlan,
    StrategyGateDecision,
    build_approved_position_plan,
    check_data_freshness,
    evaluate_approved_regime_intent,
)
from execution.margin_cross.track import CrossMarginTrack, TrackResult

__all__ = [
    "APPROVED_CONTRACT_LABEL",
    "APPROVED_CROSS_MARGIN_STRATEGIES",
    "APPROVED_DRY_RUN_MODE",
    "ApprovedPositionPlan",
    "BinanceCrossMarginHttpClient",
    "CrossMarginTrack",
    "EMERGENCY_MARGIN_LEVEL",
    "LIVE_EXECUTION_MODE",
    "LiveNetworkDisabled",
    "MAX_ACCOUNT_LEVERAGE",
    "MAX_EFFECTIVE_LEVERAGE",
    "MIN_ENTRY_MARGIN_LEVEL",
    "MarginExchangeError",
    "MarginTransportTimeout",
    "OFFLINE_FIXTURE_STRATEGY_ID",
    "PURPOSE_CROSS_MARGIN_TRACK",
    "REQUIRED_UNAPPROVED_LABEL",
    "StrategyGateDecision",
    "TrackResult",
    "WARN_MARGIN_LEVEL",
    "build_approved_position_plan",
    "check_data_freshness",
    "evaluate_approved_regime_intent",
    "fetch_live_cross_margin_snapshot",
    "sign_margin_query",
]
