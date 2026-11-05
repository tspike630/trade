"""Pre-trade gates for ``live_bnb_boll_fade``. No network and no secrets.

A live BUY is not a completed consumer event until a native spot OCO
(``POST /api/v3/orderList/oco``) is verified: ``orderListId >= 0`` and both
leg order ids ``>= 0``. ``spot.newOrder`` types ``MARKET``, ``STOP_LOSS``,
``STOP_LOSS_LIMIT``, ``TAKE_PROFIT``, and ``TAKE_PROFIT_LIMIT`` are not that
list.

The regular API-key book and MCP uid ``1289917075`` are different books.
A signed ``GET /api/v3/account`` that only shows ``canTrade`` does not bind
either one. This module never reports the arm live-ready.
"""

from __future__ import annotations

from typing import Any

PURPOSE_LIVE_BNB_BOLL_FADE = "live_bnb_boll_fade"
MCP_POSITION_UID = "1289917075"
REST_BOOK = "rest_api_key"
MCP_BOOK = "mcp:1289917075"

# Production cursor after the manual FET fill and the rejected batch under it.
# Startup reads strictly above this id. It is not an entry parameter.
REPLAY_FLOOR_DECISION_ID = 147840
FROZEN_DECISION_ID = 147831
FROZEN_CLIENT_ID = "bnbfade-147831"

# Single-leg ``spot.newOrder`` types. None of them is ``orderList/oco``.
SINGLE_LEG_ORDER_TYPES = frozenset(
    {
        "MARKET",
        "LIMIT",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
        "TAKE_PROFIT",
        "TAKE_PROFIT_LIMIT",
    }
)
_OCO_WORKING = frozenset({"EXECUTING", "EXEC_STARTED"})
_TERMINAL_POSITION = frozenset({"closed", "closed_dust", "oco_filled"})
_ABOVE_LEG = frozenset({"LIMIT_MAKER", "TAKE_PROFIT_LIMIT"})
_BELOW_LEG = frozenset({"STOP_LOSS_LIMIT"})

# Standing facts this process cannot close. Not an environment attestation.
EXTERNAL_BLOCKERS = (
    "rest_account_uid_not_proven",
    "mcp_uid_1289917075_is_a_separate_book",
    "mcp_connector_has_no_order_list_oco",
    "fixed_egress_does_not_represent_mcp_connector",
    "canonical_fet_close_is_worker_owned",
    "paper_off_fixed_egress_and_consumer_are_environment_checks",
)


def acceptance_report() -> dict[str, Any]:
    """Always not live-ready. A unit-test snapshot does not clear the blockers."""
    return {
        "arm": PURPOSE_LIVE_BNB_BOLL_FADE,
        "live_ready": False,
        "blockers": list(EXTERNAL_BLOCKERS),
    }


def consumer_read_cursor(
    *,
    persisted: int,
    since_id: int | None,
    live_arm: bool,
) -> int:
    """Cursor for one consumer read.

    On the live arm a lower ``since_id`` cannot pull historical decisions.
    The read starts at ``max(persisted, since, replay floor)``.
    """
    chosen = int(persisted) if since_id is None else int(since_id)
    if not live_arm:
        return chosen
    return max(chosen, int(persisted), REPLAY_FLOOR_DECISION_ID)


def replay_block(decision_id: object, client_id: str | None) -> str | None:
    """The manual FET client and its decision id are never a new order."""
    if client_id == FROZEN_CLIENT_ID:
        return "historical_client_replay"
    if isinstance(decision_id, bool) or decision_id is None:
        return None
    text = str(decision_id).strip()
    if text.isdigit() and int(text) == FROZEN_DECISION_ID:
        return "historical_client_replay"
    return None


def _nonneg_id(raw: object) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _reports(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("orderReports")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _leg_order_ids(payload: dict[str, Any]) -> tuple[object, object]:
    above: object = None
    below: object = None
    for row in _reports(payload):
        kind = str(row.get("type") or "").upper()
        if above is None and kind in _ABOVE_LEG:
            above = row.get("orderId")
        if below is None and kind in _BELOW_LEG:
            below = row.get("orderId")
    orders = payload.get("orders")
    if isinstance(orders, list):
        ids = [
            item.get("orderId")
            for item in orders
            if isinstance(item, dict) and item.get("orderId") is not None
        ]
        if above is None and len(ids) >= 1:
            above = ids[0]
        if below is None and len(ids) >= 2:
            below = ids[1]
    return above, below


def _working_list(payload: dict[str, Any]) -> bool:
    status = str(payload.get("listOrderStatus") or "").upper()
    kind = str(payload.get("listStatusType") or "").upper()
    if status in {"ALL_DONE", "REJECT"} or kind == "REJECT":
        return False
    return status == "EXECUTING" or kind in _OCO_WORKING


def native_oco_verified(payload: object) -> tuple[dict[str, int] | None, str | None]:
    """Accept one native OCO receipt. A single ``newOrder`` body is not one.

    Returns ``({order_list_id, above_order_id, below_order_id}, None)`` or
    ``(None, error)``. No network.
    """
    if not isinstance(payload, dict):
        return None, "native_oco_unverified"
    order_type = str(payload.get("type") or "").upper()
    list_id = payload.get("orderListId")
    if order_type in SINGLE_LEG_ORDER_TYPES and list_id is None:
        return None, "mcp_order_not_oco"
    parsed_list = _nonneg_id(list_id)
    if parsed_list is None:
        return None, "native_oco_unverified"
    if not _working_list(payload):
        return None, "native_oco_unverified"
    above_raw, below_raw = _leg_order_ids(payload)
    above = _nonneg_id(above_raw)
    below = _nonneg_id(below_raw)
    if above is None or below is None or above == below:
        return None, "native_oco_unverified"
    return {
        "order_list_id": parsed_list,
        "above_order_id": above,
        "below_order_id": below,
    }, None


def account_book_block(snapshot: object, book: object) -> str | None:
    """Refuse a live BUY whose account book is unbound or mixed.

    ``canTrade`` and a zero FET balance are not an identity. The MCP uid is
    not implied by the regular API key. An env uid is not a snapshot.
    """
    selected = str(book or "").strip()
    if not selected:
        return "account_book_unselected"
    if not isinstance(snapshot, dict):
        return "account_uid_unbound"
    uid = _nonneg_id(snapshot.get("uid"))
    if uid is None:
        return "account_uid_unbound"
    uid_text = str(uid)
    if selected == MCP_BOOK:
        if uid_text != MCP_POSITION_UID:
            return "account_book_mismatch"
        return "mcp_has_no_native_oco"
    if selected == REST_BOOK:
        if uid_text == MCP_POSITION_UID:
            return "account_book_mismatch"
        return None
    return "account_book_mismatch"


def _position_ids_verified(row: dict[str, Any]) -> bool:
    return (
        _nonneg_id(row.get("oco_order_list_id")) is not None
        and _nonneg_id(row.get("oco_above_order_id")) is not None
        and _nonneg_id(row.get("oco_below_order_id")) is not None
    )


def buy_exposure_block(
    positions: object,
    canonical: object,
) -> str | None:
    """Block a new BUY while any live exposure is naked, unbound, or unfinished.

    A flat book is not by itself permission to buy. Terminal rows are ignored.
    This does not write a close for the manual FET fill.
    """
    rows = canonical if isinstance(canonical, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        purpose = str(row.get("purpose") or "")
        if purpose and purpose != PURPOSE_LIVE_BNB_BOLL_FADE:
            continue
        status = str(row.get("status") or "")
        if status == "held_naked":
            return "naked_holds_new_buys"
        if status and status not in (_TERMINAL_POSITION | {"flat", "closed"}):
            if not str(row.get("entry_order_id") or "").strip():
                return "unbound_position"
    held = positions if isinstance(positions, list) else []
    for row in held:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        if not status or status in _TERMINAL_POSITION:
            continue
        if status == "qty_unconfirmed":
            return "parent_fill_incomplete"
        if status in {"oco_failed", "oco_unknown"}:
            return "oco_failed_blocks_buy"
        if status == "open":
            if str(row.get("protection") or "") != "exchange_oco" or not _position_ids_verified(row):
                return "unprotected_position_blocks_buy"
            return "position_open_blocks_buy"
        if status in {"oco_working", "exit_submitted"}:
            return "position_open_blocks_buy"
        return "unprotected_position_blocks_buy"
    return None
