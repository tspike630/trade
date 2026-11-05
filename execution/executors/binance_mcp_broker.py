"""Parent gate for ``live_bnb_boll_fade``. This process does not send orders.

Trade permission on the connector is already the selected scope. The gap is
tool selection: an empty handshake is fail-closed, and a later catalog is
read from the injected probe. Native OCO is ``POST /api/v3/orderList/oco``
(operation ``orderListOco``). It is released to the parent only when that
probe's tool names include it. This module does not call ``invoke``.

``original_api_key`` means the raw adapter route is recognized. Health of
that route is a separate flag and stays unverified here. Environment
credentials are not read.

A naked hold blocks new buys. Sells stay off (``HOLD_NAKED_NO_AUTO_SELL``).
A spent client id blocks that same id. It does not retire the symbol.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any, Iterable, Literal, Protocol

from execution.executors.binance_spot import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    _client_order_id,
    _decimal_text,
    _to_decimal,
    floor_base_qty,
)
from execution.executors.binance_spot_gate import replay_block

GateExpr = Literal[
    "original_api_key",
    "original_api_key_healthy",
    "protection_tool",
    "resume_signals",
    "buy",
    "sell",
    "oco",
    "poll_naked",
]

# Last manual catalog count. Used only as a note when no probe is connected.
# Decisions read ``probe().names()``, not this number.
OBSERVED_SESSION_TOOL_COUNT = 72
DOCUMENTED_OCO_REST = "POST /api/v3/orderList/oco"
DOCUMENTED_OCO_OPERATION = "orderListOco"
WEBSOCKET_NOT_MCP = "orderList.place.oco"
OCO_TOOL_NAMES = frozenset({"spot.orderList.oco", "spot.orderListOco"})
QUOTE_QUANTUM = Decimal("0.01")
DEFAULT_QUOTE_REDUCE_CAP = Decimal("0.05")
HELD_NAKED = "held_naked"
HOLD_NAKED_NO_AUTO_SELL = True

_GATE_EXPRS: tuple[GateExpr, ...] = (
    "original_api_key",
    "original_api_key_healthy",
    "protection_tool",
    "resume_signals",
    "buy",
    "sell",
    "oco",
    "poll_naked",
)

_BLOCK_REASONS = frozenset(
    {
        "purpose_not_live_bnb_boll_fade",
        "hold_naked_until_native_oco",
        "spent_client_order",
        "naked_holds_new_buys",
        "protection_tool_absent",
        "buy_unreleased",
        "historical_client_replay",
    }
)


class ToolCatalog(Protocol):
    """Parent binding. ``names`` is read. ``invoke`` is not called here."""

    def names(self) -> Iterable[str]:
        """Tool names exposed by the current connector."""

    def invoke(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Parent-side call. Implementations in this process must not run it."""


def _oco_tool(catalog: Iterable[str]) -> str | None:
    present = {str(name) for name in catalog}
    present.discard(WEBSOCKET_NOT_MCP)
    for name in sorted(OCO_TOOL_NAMES, key=lambda item: item != "spot.orderList.oco"):
        if name in present:
            return name
    return None


def availability(expr: GateExpr, catalog: Iterable[str] | None = None) -> bool:
    """Route and catalog flags.

    ``original_api_key`` is the recognized raw route, not a health check.
    Protection flags follow ``catalog``. An omitted catalog is fail-closed.
    Buys, sells, and polling stay off in this process.
    """
    names = frozenset() if catalog is None else frozenset(str(name) for name in catalog)
    protected = _oco_tool(names) is not None
    if expr == "original_api_key":
        return True
    if expr == "original_api_key_healthy":
        return False
    if expr == "protection_tool":
        return protected
    if expr == "oco":
        return protected
    if expr == "resume_signals":
        return protected
    if expr == "buy":
        return False
    if expr == "sell":
        return False
    if expr == "poll_naked":
        return False
    unexpected: never = expr
    raise AssertionError(unexpected)


def report_availability(catalog: Iterable[str] | None = None) -> dict[str, bool]:
    """Evaluate one captured catalog. Does not call a network."""
    names = frozenset() if catalog is None else frozenset(str(name) for name in catalog)
    return {expr: availability(expr, names) for expr in _GATE_EXPRS}


def _ratio(original: Decimal, floored: Decimal) -> Decimal:
    return (original - floored) / original


def map_quote(raw: object, *, cap: Decimal | None = DEFAULT_QUOTE_REDUCE_CAP) -> dict[str, Any]:
    """Floor a USDT quote to 2 decimal places. The 5% cap is quote rounding only."""
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
    if cap is None or ratio <= cap:
        trace["within_cap"] = True
    else:
        trace["error"] = "qty_reduce_exceeds_cap"
    return trace


def map_quantity(raw: object, step: object) -> dict[str, Any]:
    """Floor base qty to the supplied step. The quote cap does not apply."""
    trace: dict[str, Any] = {
        "field": "quantity",
        "raw": None if raw is None else str(raw),
        "step": None if step in (None, "") else str(step),
        "floored": None,
        "allow_base_floor": False,
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
    trace["allow_base_floor"] = True
    return trace


def _rows(book: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = book.get(key)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _naked_rows(book: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(book, dict):
        return []
    held: list[dict[str, Any]] = []
    for row in _rows(book, "canonical_positions"):
        same_purpose = str(row.get("purpose") or "") == PURPOSE_LIVE_BNB_BOLL_FADE
        if same_purpose and str(row.get("status") or "") == HELD_NAKED:
            held.append(row)
    return held


def _spent_clients(book: dict[str, Any]) -> set[str]:
    clients = {
        str(row.get("client_order_id") or "")
        for row in _rows(book, "spent_orders")
    }
    loose = book.get("spent_client_ids")
    if isinstance(loose, list):
        clients.update(str(item) for item in loose)
    clients.discard("")
    return clients


def reconcile_block(
    book: dict[str, Any] | None,
    *,
    client_order_id: str | None,
) -> str | None:
    """Same client id, or any naked hold. A spent fill does not retire the symbol."""
    if not isinstance(book, dict):
        return None
    if client_order_id and client_order_id in _spent_clients(book):
        return "spent_client_order"
    if replay_block(None, client_order_id):
        return "historical_client_replay"
    if _naked_rows(book):
        return "naked_holds_new_buys"
    return None


def _queue_item(
    kind: str,
    reason: str,
    *,
    tool: str | None,
    release: bool,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": "parent_invoke" if release else "block",
        "kind": kind,
        "tool": tool,
        "release": release,
        "invoke": False,
        "reason": reason,
        "arguments": arguments or {},
        "dry_plan": kind == "oco" and release,
        "documented_rest": DOCUMENTED_OCO_REST if kind == "oco" else None,
        "operation_id": DOCUMENTED_OCO_OPERATION if kind == "oco" else None,
        "in_connector": bool(tool) if kind == "oco" else None,
    }


def _oco_arguments(row: dict[str, Any]) -> dict[str, Any]:
    symbol = row.get("symbol")
    arguments: dict[str, Any] = {"side": "SELL"}
    if isinstance(symbol, str) and symbol.strip():
        arguments["symbol"] = symbol.strip()
    quantity = row.get("quantity", row.get("executed_qty"))
    if quantity not in (None, ""):
        arguments["quantity"] = str(quantity)
    take_profit = row.get("take_profit_price")
    if take_profit not in (None, ""):
        arguments["aboveType"] = "LIMIT_MAKER"
        arguments["abovePrice"] = str(take_profit)
    stop = row.get("stop_price")
    if stop not in (None, ""):
        arguments["belowType"] = "STOP_LOSS_LIMIT"
        arguments["belowStopPrice"] = str(stop)
    return arguments


def _symbol_of(signal: dict[str, Any]) -> str | None:
    extras = signal.get("extras")
    if not isinstance(extras, dict):
        return None
    raw = extras.get("binance_symbol")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def parent_calls(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    """Queue rows the parent may invoke. Nothing in them has been called."""
    queue = evaluation.get("parent_queue")
    if not isinstance(queue, list):
        return []
    return [
        item
        for item in queue
        if isinstance(item, dict) and item.get("release") is True and item.get("invoke") is False
    ]


class LiveMCPBroker:
    """Read a tool catalog and return a parent queue. Construction does not trade."""

    background = False

    def __init__(
        self,
        transport: ToolCatalog | None = None,
        *,
        quote_reduce_cap: Decimal | None = DEFAULT_QUOTE_REDUCE_CAP,
    ) -> None:
        self._transport = transport
        self.quote_reduce_cap = quote_reduce_cap
        self._catalog: frozenset[str] = frozenset()
        self.handshake = False
        self.transport_called = False

    def probe(self) -> dict[str, Any]:
        """Read ``transport.names()``. No transport means an empty fail-closed catalog."""
        if self._transport is None:
            self._catalog = frozenset()
            self.handshake = False
        else:
            try:
                named = self._transport.names()
            except Exception:
                named = ()
                self.handshake = False
            else:
                self.handshake = True
            self._catalog = frozenset(str(name) for name in named)
            self._catalog -= {WEBSOCKET_NOT_MCP}
        return {
            "handshake": self.handshake,
            "catalog": sorted(self._catalog),
            "availability": report_availability(self._catalog),
            "empty_handshake_observed_tool_count": (
                None if self.handshake else OBSERVED_SESSION_TOOL_COUNT
            ),
        }

    def availability(self, expr: GateExpr) -> bool:
        return availability(expr, self._catalog)

    def evaluate(
        self,
        signal: dict[str, Any],
        book: dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Build the parent queue. ``api_key`` is ignored and not stored."""
        del api_key
        probed = self.probe()
        report = probed["availability"]
        symbol = _symbol_of(signal)
        client_id = _client_order_id(signal)
        oco_name = _oco_tool(self._catalog)
        naked = _naked_rows(book)
        purpose = str(signal.get("purpose") or "").strip()
        if purpose != PURPOSE_LIVE_BNB_BOLL_FADE:
            return self._result(
                reason="purpose_not_live_bnb_boll_fade",
                signal=signal,
                idem_decision="refused",
                client_order_id=client_id,
                symbol=symbol,
                mapping_trace=[],
                queue=[_queue_item("buy", "purpose_not_live_bnb_boll_fade", tool=None, release=False)],
                duplicate_prevented=False,
                report=report,
            )
        action = str(signal.get("action") or "").strip().lower()
        if action == "sell":
            if not HOLD_NAKED_NO_AUTO_SELL:
                raise AssertionError("auto sell is not a path in this process")
            return self._result(
                reason="hold_naked_until_native_oco",
                signal=signal,
                idem_decision="sell_blocked",
                client_order_id=client_id,
                symbol=symbol,
                mapping_trace=[],
                queue=[
                    _queue_item(
                        "sell",
                        "hold_naked_until_native_oco",
                        tool="spot.newOrder",
                        release=False,
                    )
                ],
                duplicate_prevented=False,
                report=report,
            )
        extras = signal.get("extras")
        if not isinstance(extras, dict):
            extras = {}
        mapping: list[dict[str, Any]] = []
        if extras.get("quote_order_qty", extras.get("quoteOrderQty")) not in (None, ""):
            mapping.append(
                map_quote(
                    extras.get("quote_order_qty", extras.get("quoteOrderQty")),
                    cap=self.quote_reduce_cap,
                )
            )
        if extras.get("quantity") not in (None, ""):
            mapping.append(map_quantity(extras.get("quantity"), extras.get("lot_step")))
        held = reconcile_block(book, client_order_id=client_id)
        if held is not None:
            reason = held
        elif oco_name is None:
            reason = "protection_tool_absent"
        else:
            reason = "buy_unreleased"
        oco_release = bool(oco_name) and bool(naked)
        oco_args = _oco_arguments(naked[0]) if oco_release and naked else {}
        queue = [
            _queue_item("buy", reason, tool="spot.newOrder", release=False),
            _queue_item(
                "oco",
                "native_oco_for_held" if oco_release else "connector_omits_orderListOco",
                tool=oco_name if oco_release else None,
                release=oco_release,
                arguments=oco_args,
            ),
            _queue_item(
                "sell",
                "hold_naked_until_native_oco",
                tool="spot.newOrder",
                release=False,
            ),
        ]
        self._guard_queue(queue, oco_name=oco_name)
        return self._result(
            reason=reason,
            signal=signal,
            idem_decision=held or ("native_oco_queued" if oco_release else "protection_tool_absent"),
            client_order_id=client_id,
            symbol=symbol,
            mapping_trace=mapping,
            queue=queue,
            duplicate_prevented=held is not None,
            report=report,
        )

    def _guard_queue(self, queue: list[dict[str, Any]], *, oco_name: str | None) -> None:
        for item in queue:
            if item["invoke"]:
                raise AssertionError("queue must not invoke")
            if item["tool"] == WEBSOCKET_NOT_MCP:
                raise AssertionError("websocket method is not an MCP tool")
            if item["kind"] in {"buy", "sell"} and item["release"]:
                raise AssertionError("buy and sell stay unreleased")
            if item["kind"] == "oco" and item["release"] and not oco_name:
                raise AssertionError("oco release requires a catalog tool")

    def _result(
        self,
        *,
        reason: str,
        signal: dict[str, Any],
        idem_decision: str,
        client_order_id: str | None,
        symbol: str | None,
        mapping_trace: list[dict[str, Any]],
        queue: list[dict[str, Any]],
        duplicate_prevented: bool,
        report: dict[str, bool],
    ) -> dict[str, Any]:
        if reason not in _BLOCK_REASONS:
            raise AssertionError(reason)
        return {
            "ok": False,
            "executor": "binance_mcp_broker",
            "new_trade": False,
            "submitted": False,
            "executed": 0,
            "resume_signals": report["resume_signals"],
            "allow_buy": report["buy"],
            "allow_sell": report["sell"],
            "oco_placed": False,
            "oco_promised": False,
            "poll_naked_position": report["poll_naked"],
            "original_api_key_available": report["original_api_key"],
            "original_api_key_healthy": report["original_api_key_healthy"],
            "protection_tool_present": report["protection_tool"],
            "availability": report,
            "handshake": self.handshake,
            "transport_called": self.transport_called,
            "background": False,
            "auto_sell": not HOLD_NAKED_NO_AUTO_SELL,
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
            "catalog_size": len(self._catalog),
            "documented_oco_rest": DOCUMENTED_OCO_REST,
        }
