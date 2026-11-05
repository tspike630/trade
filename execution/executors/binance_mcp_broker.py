"""Parent gate for ``live_bnb_boll_fade``. No order is sent from here.

The official spot OCO is ``POST /api/v3/orderList/oco`` (operation
``orderListOco``). After Trade reauthorization the connected Binance MCP
catalog still has 72 tools and none of them is that operation. There is no
other channel in this process: a cloud subprocess cannot call MCP, and
``original_api_key`` is not treated as available.

Buys stay frozen and the existing naked position is not sold. This module
does not poll, does not start a thread, and does not open a socket.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from execution.executors.binance_spot import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    _client_order_id,
    _decimal_text,
    _to_decimal,
    floor_base_qty,
)

GateExpr = Literal[
    "original_api_key",
    "protection_tool",
    "resume_signals",
    "buy",
    "sell",
    "oco",
    "poll_naked",
]

CONFIRMED_SESSION_TOOL_COUNT = 72
DOCUMENTED_OCO_REST = "POST /api/v3/orderList/oco"
DOCUMENTED_OCO_OPERATION = "orderListOco"
DEPRECATED_OCO_REST = "POST /api/v3/order/oco"
WEBSOCKET_NOT_MCP = "orderList.place.oco"
QUOTE_QUANTUM = Decimal("0.01")
REDUCE_CAP = Decimal("0.05")
HELD_NAKED = "held_naked"

_BLOCK_REASONS = frozenset(
    {
        "purpose_not_live_bnb_boll_fade",
        "hold_naked_until_native_oco",
        "spent_client_order",
        "canonical_position_blocks_new_id",
        "spent_order_symbol",
        "protection_tool_absent",
        "raw_api_key_rejected",
    }
)


def availability(expr: GateExpr) -> bool:
    """Requested capability. ``original_api_key`` is false; so is every other gate."""
    if expr == "original_api_key":
        return False
    if expr == "protection_tool":
        return False
    if expr == "resume_signals":
        return False
    if expr == "buy":
        return False
    if expr == "sell":
        return False
    if expr == "oco":
        return False
    if expr == "poll_naked":
        return False
    unexpected: never = expr
    raise AssertionError(unexpected)


def _ratio(original: Decimal, floored: Decimal) -> Decimal:
    return (original - floored) / original


def map_quote(raw: object) -> dict[str, Any]:
    """Floor a USDT quote to 2 decimal places. Never round up. Cap reduction at 5%."""
    number = _to_decimal(raw)
    trace: dict[str, Any] = {
        "field": "quoteOrderQty",
        "raw": None if raw is None else str(raw),
        "precision": 2,
        "floored": None,
        "within_cap": False,
        "reduce_ratio": None,
        "sent": False,
        "error": None,
    }
    if number is None:
        trace["error"] = "invalid_quote"
        return trace
    units = (number / QUOTE_QUANTUM).to_integral_value(rounding=ROUND_DOWN)
    floored = units * QUOTE_QUANTUM
    if floored <= 0 or floored > number:
        trace["error"] = "invalid_quote"
        return trace
    ratio = _ratio(number, floored)
    trace["floored"] = _decimal_text(floored)
    trace["reduce_ratio"] = _decimal_text(ratio) if ratio > 0 else "0"
    trace["within_cap"] = ratio <= REDUCE_CAP
    if not trace["within_cap"]:
        trace["error"] = "qty_reduce_exceeds_cap"
    return trace


def map_quantity(raw: object, step: object) -> dict[str, Any]:
    """Floor base qty to the caller-supplied step. No step means no size."""
    trace: dict[str, Any] = {
        "field": "quantity",
        "raw": None if raw is None else str(raw),
        "step": None if step in (None, "") else str(step),
        "floored": None,
        "within_cap": False,
        "reduce_ratio": None,
        "sent": False,
        "error": None,
    }
    if step in (None, ""):
        trace["error"] = "missing_symbol_filters"
        return trace
    number = _to_decimal(raw)
    floored_text = floor_base_qty(raw, step)
    if number is None or floored_text is None:
        trace["error"] = "invalid_quantity"
        return trace
    floored = _to_decimal(floored_text)
    if floored is None or floored > number:
        trace["error"] = "invalid_quantity"
        return trace
    ratio = _ratio(number, floored)
    trace["floored"] = floored_text
    trace["reduce_ratio"] = _decimal_text(ratio) if ratio > 0 else "0"
    trace["within_cap"] = ratio <= REDUCE_CAP
    if not trace["within_cap"]:
        trace["error"] = "qty_reduce_exceeds_cap"
    return trace


def _rows(book: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = book.get(key)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def reconcile_block(
    book: dict[str, Any] | None,
    *,
    symbol: str | None,
    client_order_id: str | None,
) -> str | None:
    """Two book checks. Either one freezes a buy. Neither one sells."""
    if not isinstance(book, dict):
        return None
    spent_clients = {
        str(row.get("client_order_id") or "")
        for row in _rows(book, "spent_orders")
    }
    loose_clients = book.get("spent_client_ids")
    if isinstance(loose_clients, list):
        spent_clients.update(str(item) for item in loose_clients)
    if client_order_id and client_order_id in spent_clients:
        return "spent_client_order"
    if symbol is None:
        return None
    for row in _rows(book, "canonical_positions"):
        same_purpose = str(row.get("purpose") or "") == PURPOSE_LIVE_BNB_BOLL_FADE
        same_symbol = str(row.get("symbol") or "") == symbol
        held = str(row.get("status") or "") == HELD_NAKED
        if same_purpose and same_symbol and held:
            return "canonical_position_blocks_new_id"
    for row in _rows(book, "spent_orders"):
        if str(row.get("symbol") or "") == symbol and str(row.get("order_id") or ""):
            return "spent_order_symbol"
    return None


def _block(kind: str, reason: str, *, tool: str | None) -> dict[str, Any]:
    return {
        "action": "block",
        "kind": kind,
        "tool": tool,
        "release": False,
        "invoke": False,
        "reason": reason,
        "documented_rest": DOCUMENTED_OCO_REST if kind == "oco" else None,
        "operation_id": DOCUMENTED_OCO_OPERATION if kind == "oco" else None,
        "in_connector": False if kind == "oco" else None,
    }


def _symbol_of(signal: dict[str, Any]) -> str | None:
    extras = signal.get("extras")
    if not isinstance(extras, dict):
        return None
    raw = extras.get("binance_symbol")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _result(
    *,
    reason: str,
    signal: dict[str, Any],
    idem_decision: str,
    client_order_id: str | None,
    symbol: str | None,
    mapping_trace: list[dict[str, Any]],
    queue: list[dict[str, Any]],
    duplicate_prevented: bool,
) -> dict[str, Any]:
    if reason not in _BLOCK_REASONS:
        raise AssertionError(reason)
    return {
        "ok": False,
        "executor": "binance_mcp_broker",
        "new_trade": False,
        "submitted": False,
        "executed": 0,
        "resume_signals": availability("resume_signals"),
        "allow_buy": availability("buy"),
        "allow_sell": availability("sell"),
        "oco_placed": False,
        "oco_promised": False,
        "poll_naked_position": availability("poll_naked"),
        "original_api_key_available": availability("original_api_key"),
        "protection_tool_present": availability("protection_tool"),
        "transport_called": False,
        "background": False,
        "reason": reason,
        "symbol": symbol,
        "idem": {
            "decision_log_id": signal.get("decision_log_id"),
            "client_order_id": client_order_id,
            "idem_decision": idem_decision,
            "duplicate_prevented": duplicate_prevented,
        },
        "mapping_trace": mapping_trace,
        "parent_queue": queue,
        "connector_tool_count": CONFIRMED_SESSION_TOOL_COUNT,
        "oco_rest_not_in_connector": DOCUMENTED_OCO_REST,
    }


class LiveMCPBroker:
    """Gate the parent consults. Instantiating it does not trade or poll."""

    background = False

    def availability(self, expr: GateExpr) -> bool:
        return availability(expr)

    def evaluate(
        self,
        signal: dict[str, Any],
        book: dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Return a blocked parent queue. ``api_key`` is rejected and not stored."""
        symbol = _symbol_of(signal)
        client_id = _client_order_id(signal)
        if api_key is not None and str(api_key).strip():
            return _result(
                reason="raw_api_key_rejected",
                signal=signal,
                idem_decision="refused",
                client_order_id=client_id,
                symbol=symbol,
                mapping_trace=[],
                queue=[],
                duplicate_prevented=False,
            )
        purpose = str(signal.get("purpose") or "").strip()
        if purpose != PURPOSE_LIVE_BNB_BOLL_FADE:
            return _result(
                reason="purpose_not_live_bnb_boll_fade",
                signal=signal,
                idem_decision="refused",
                client_order_id=client_id,
                symbol=symbol,
                mapping_trace=[],
                queue=[_block("buy", "purpose_not_live_bnb_boll_fade", tool=None)],
                duplicate_prevented=False,
            )
        action = str(signal.get("action") or "").strip().lower()
        if action == "sell":
            return _result(
                reason="hold_naked_until_native_oco",
                signal=signal,
                idem_decision="sell_blocked",
                client_order_id=client_id,
                symbol=symbol,
                mapping_trace=[],
                queue=[
                    _block("sell", "hold_naked_until_native_oco", tool="spot.newOrder")
                ],
                duplicate_prevented=False,
            )
        extras = signal.get("extras")
        if not isinstance(extras, dict):
            extras = {}
        mapping: list[dict[str, Any]] = []
        if extras.get("quote_order_qty", extras.get("quoteOrderQty")) not in (None, ""):
            mapping.append(
                map_quote(extras.get("quote_order_qty", extras.get("quoteOrderQty")))
            )
        if extras.get("quantity") not in (None, ""):
            mapping.append(map_quantity(extras.get("quantity"), extras.get("lot_step")))
        held = reconcile_block(book, symbol=symbol, client_order_id=client_id)
        reason = held or "protection_tool_absent"
        idem = held or "protection_tool_absent"
        queue = [
            _block("buy", reason, tool="spot.newOrder"),
            _block("oco", "connector_omits_orderListOco", tool=None),
            _block("sell", "hold_naked_until_native_oco", tool="spot.newOrder"),
        ]
        for item in queue:
            if item["tool"] == WEBSOCKET_NOT_MCP or item["release"] or item["invoke"]:
                raise AssertionError("blocked queue must not release a native call")
        return _result(
            reason=reason,
            signal=signal,
            idem_decision=idem,
            client_order_id=client_id,
            symbol=symbol,
            mapping_trace=mapping,
            queue=queue,
            duplicate_prevented=held is not None,
        )
