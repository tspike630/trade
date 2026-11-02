"""Same entry sleeves as portfolio_risk_v1, tighter exit meta → race_portfolio_tight_sl.

Entry identical to portfolio_risk_v1 (A 35% / B swing 25% / C selfbuilt 10% / cash 30%,
token mutex, top-1/sleeve, size_pct scaled for SIZE_MULT=7).

Exit (execution peer contract via buy() extras; paper_executor picks tp/sl/hold):
  Parent peer defaults: tp_pct=10, sl_pct=5, max_hold_min=60.
  This arm:        tp_pct=10, sl_pct=2.5 (half), max_hold_min=60,
                   trail_pct=10.0 (advisory; paper tick path currently uses
                   tp/sl/hold only — trail reserved for peer/c_exit if wired).

Alias only: sleeve_tight_sl_v1 → portfolio_tight_sl_v1 (no extra peer).
"""

from __future__ import annotations

from typing import Any

from decision_engine.strategies.portfolio_risk_sleeves import (
    PortfolioRiskSleevesStrategy,
)

RULE_VERSION = "portfolio_tight_sl_v1_2026-11-01"
STRATEGY_ID = "portfolio_tight_sl_v1"

# Defensive vs paper defaults (TP10 / SL5 / hold60).
TIGHT_TP_PCT = 10.0
TIGHT_SL_PCT = 2.5  # half of DEFAULT_SL_PCT=5
TIGHT_MAX_HOLD_MIN = 60.0
TIGHT_TRAIL_PCT = 10.0  # half of factor trail 20%; advisory on paper path


class PortfolioTightSlStrategy(PortfolioRiskSleevesStrategy):
    """Risk sleeves entry + tighter SL (and trail meta) on buys."""

    strategy_id = STRATEGY_ID
    RULE_VERSION = RULE_VERSION

    def _exit_meta(self) -> dict[str, Any]:
        return {
            "tp_pct": TIGHT_TP_PCT,
            "sl_pct": TIGHT_SL_PCT,
            "max_hold_min": TIGHT_MAX_HOLD_MIN,
            "trail_pct": TIGHT_TRAIL_PCT,
            "exit_profile": "tight_sl",
        }
