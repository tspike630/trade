"""Isolated Binance cross-margin execution track.

Nothing here is imported by ``execution``, ``execution.executors``, the spot
gateway, or the decision engine. Construct ``CrossMarginTrack`` only from an
explicit caller.

Runtime live execution stays locked unless ``CROSS_MARGIN_PARAMETERS_APPROVED=1``,
``CROSS_MARGIN_APPROVED_CONFIG_JSON`` points to a valid zero-default JSON file,
``CROSS_MARGIN_LIVE_ENABLED=1``, and no kill-switch flag/file is active.
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
    APPROVED_CONFIG_PATH_ENV,
    APPROVED_CONTRACT_LABEL,
    APPROVED_DRY_RUN_MODE,
    HARD_CLASSIC_CROSS_LEVERAGE_CEILING,
    LIVE_EXECUTION_MODE,
    SUPPORTED_REGIME_STRATEGIES,
    ApprovedRiskConfig,
    load_approved_risk_config,
    parse_approved_risk_config,
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
    "APPROVED_CONFIG_PATH_ENV",
    "APPROVED_CONTRACT_LABEL",
    "APPROVED_DRY_RUN_MODE",
    "ApprovedPositionPlan",
    "ApprovedRiskConfig",
    "BinanceCrossMarginHttpClient",
    "CrossMarginTrack",
    "HARD_CLASSIC_CROSS_LEVERAGE_CEILING",
    "LIVE_EXECUTION_MODE",
    "LiveNetworkDisabled",
    "MarginExchangeError",
    "MarginTransportTimeout",
    "OFFLINE_FIXTURE_STRATEGY_ID",
    "PURPOSE_CROSS_MARGIN_TRACK",
    "REQUIRED_UNAPPROVED_LABEL",
    "SUPPORTED_REGIME_STRATEGIES",
    "StrategyGateDecision",
    "TrackResult",
    "build_approved_position_plan",
    "check_data_freshness",
    "evaluate_approved_regime_intent",
    "fetch_live_cross_margin_snapshot",
    "load_approved_risk_config",
    "parse_approved_risk_config",
    "sign_margin_query",
]
