"""Phase 1 panel registry.

Append a new PanelSpec to add a panel. Do not edit the existing panel modules
to register it. P4 manual trading controls are intentionally not registered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from dashboard.data import DataStore
from dashboard.panels.execution import render_execution
from dashboard.panels.market import render_market
from dashboard.panels.positions import render_positions
from dashboard.panels.risk import render_risk
from dashboard.panels.signals import render_signals


@dataclass(frozen=True)
class PanelSpec:
    id: str
    title: str
    render: Callable[[DataStore], None]


PANELS: tuple[PanelSpec, ...] = (
    PanelSpec("risk", "风险", render_risk),
    PanelSpec("positions", "持仓", render_positions),
    PanelSpec("signals", "信号流", render_signals),
    PanelSpec("execution", "执行记录", render_execution),
    PanelSpec("market", "链上行情", render_market),
)
