"""Production dispatch for ``live_bnb_boll_fade``. No HTTP and no paper fill.

The 147831 FET buy was a manual MCP order. It did not pass through
``execution.run``. The verified receipt below is that fill. Importing it
does not write an exit, a paper order, or the execution cursor.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from execution.executors.binance_mcp_broker import (
    HOLD_NAKED_NO_AUTO_SELL,
    LiveMCPBroker,
    parent_calls,
)
from execution.executors.binance_spot import PURPOSE_LIVE_BNB_BOLL_FADE
from execution.ledger import ExecutionLedger

VERIFIED_FET_FILL: dict[str, Any] = {
    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
    "symbol": "FETUSDT",
    "status": "held_naked",
    "decision_log_id": 147831,
    "order_id": "3735698106",
    "client_order_id": "bnbfade-147831",
    "quantity": "140.1",
    "executed_qty": "140.1",
    "avg_price": "0.1566",
    "cummulative_quote_qty": "21.93966",
    "commission": "0.00002310",
    "commission_asset": "BNB",
    "source": "manual_mcp",
}

_PAPER_ENV = ("PAPER_DB_PATH", "EXECUTION_PAPER_DB", "EXECUTION_PAPER_ENABLED")


class CatalogProbe:
    """Parent tool list. ``invoke`` is never used by this process."""

    def __init__(self, names: Iterable[str], *, reachable: bool = True) -> None:
        self._names = tuple(str(name) for name in names if str(name).strip())
        self._reachable = reachable
        self.invocations = 0

    def names(self) -> tuple[str, ...]:
        if not self._reachable:
            raise OSError("mcp_unreachable")
        return self._names

    def invoke(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.invocations += 1
        raise OSError("mcp_unreachable")


def import_verified_fet_fill(ledger: ExecutionLedger) -> dict[str, Any]:
    """Idempotent copy of the manual FET fill into this ledger only."""
    if not HOLD_NAKED_NO_AUTO_SELL:
        raise RuntimeError("naked hold flag is off")
    return ledger.import_verified_fill(dict(VERIFIED_FET_FILL))


def dispatch_live_bnb(
    signal: dict[str, Any],
    book: dict[str, Any],
    transport: CatalogProbe | None = None,
) -> dict[str, Any]:
    """Gate one signal. An empty book is not permission to buy.

    The manual FET row is not required. A ``held_naked`` row still blocks a
    new buy. This function does not write the worker's close of that fill.
    """
    broker = LiveMCPBroker(transport=transport)
    return broker.evaluate(signal, book)


def parent_queue_view(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    return parent_calls(evaluation)


def live_bnb_paper_env_block(
    *,
    paper_enabled: bool,
    accept_live_bnb: bool,
    purpose: str | None = None,
) -> str | None:
    """Refuse the live arm when dotenv paper settings are still present."""
    if not accept_live_bnb:
        return None
    label = str(purpose or "").strip() or PURPOSE_LIVE_BNB_BOLL_FADE
    if paper_enabled:
        return (
            f"{label} requires explicit --no-paper-enabled "
            "(EXECUTION_PAPER_ENABLED must not enable paper)"
        )
    dirty = [name for name in _PAPER_ENV if os.environ.get(name, "").strip()]
    if dirty:
        return (
            f"{label} refuses paper env "
            + ",".join(dirty)
            + "; pass --no-paper-enabled with PAPER_DB_PATH= "
            "EXECUTION_PAPER_DB= EXECUTION_PAPER_ENABLED="
        )
    return None
