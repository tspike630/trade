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


def _has_canonical_fet(book: dict[str, Any]) -> bool:
    rows = book.get("canonical_positions")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (
            str(row.get("purpose") or "") == PURPOSE_LIVE_BNB_BOLL_FADE
            and str(row.get("symbol") or "") == "FETUSDT"
            and str(row.get("status") or "") == "held_naked"
        ):
            return True
    return False


def dispatch_live_bnb(
    signal: dict[str, Any],
    book: dict[str, Any],
    transport: CatalogProbe | None = None,
) -> dict[str, Any]:
    """Gate one signal. An empty canonical book is not a permission to buy."""
    if str(signal.get("purpose") or "").strip() != PURPOSE_LIVE_BNB_BOLL_FADE:
        broker = LiveMCPBroker(transport=transport)
        return broker.evaluate(signal, book)
    if not _has_canonical_fet(book):
        broker = LiveMCPBroker(transport=transport)
        blocked = broker.evaluate(signal, book)
        blocked["reason"] = "canonical_fet_missing"
        blocked["allow_buy"] = False
        blocked["allow_sell"] = False
        for item in blocked.get("parent_queue") or []:
            if isinstance(item, dict):
                item["release"] = False
                item["invoke"] = False
        blocked["idem"] = dict(blocked.get("idem") or {})
        blocked["idem"]["idem_decision"] = "canonical_fet_missing"
        return blocked
    broker = LiveMCPBroker(transport=transport)
    return broker.evaluate(signal, book)


def parent_queue_view(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    return parent_calls(evaluation)


def live_bnb_paper_env_block(*, paper_enabled: bool, accept_live_bnb: bool) -> str | None:
    """Refuse the live arm when dotenv paper settings are still present."""
    if not accept_live_bnb:
        return None
    if paper_enabled:
        return (
            "live_bnb_boll_fade requires explicit --no-paper-enabled "
            "(EXECUTION_PAPER_ENABLED must not enable paper)"
        )
    dirty = [name for name in _PAPER_ENV if os.environ.get(name, "").strip()]
    if dirty:
        return (
            "live_bnb_boll_fade refuses paper env "
            + ",".join(dirty)
            + "; pass --no-paper-enabled with PAPER_DB_PATH= "
            "EXECUTION_PAPER_DB= EXECUTION_PAPER_ENABLED="
        )
    return None
