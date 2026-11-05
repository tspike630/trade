"""Pre-trade gates for ``live_bnb_boll_fade``. No network and no secrets.

A live BUY is not a completed consumer event until a native spot OCO
(``POST /api/v3/orderList/oco``) is verified: ``orderListId >= 0`` and both
leg order ids ``>= 0``. ``spot.newOrder`` types ``MARKET``, ``STOP_LOSS``,
``STOP_LOSS_LIMIT``, ``TAKE_PROFIT``, and ``TAKE_PROFIT_LIMIT`` are not that
list.

The regular REST key and MCP uid ``1289917075`` (an Agentic sub-account) stay
separate. Binding reads ``GET /api/v3/account`` and sealed
``GET /sapi/v1/account/apiRestrictions``. Legacy ``permissions: ["SPOT"]``
still counts. ``TRD_GRP_080`` is an exact group name, not a prefix, and it
does not trade unless ``enableSpotAndMarginTrading`` is true. USDT free
below the 20 USDT order floor is ``funding_block``. The MCP uid is never
inferred.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from execution.verified_fet import VERIFIED_FET_EXIT

PURPOSE_LIVE_BNB_BOLL_FADE = "live_bnb_boll_fade"
MCP_POSITION_UID = "1289917075"
REST_BOOK = "rest_api_key"
MCP_BOOK = "mcp:1289917075"

# Signed read-only endpoints. This module parses payloads; it does not call them.
# Spot account: USER_DATA. The published example includes ``uid``; a payload
# without that field is still the account of the signing key.
SPOT_ACCOUNT_ENDPOINT = "GET /api/v3/account"
# API-key permissions. This is the spot-trading capability, not a symbol group.
API_RESTRICTIONS_ENDPOINT = "GET /sapi/v1/account/apiRestrictions"
# Published in binance-spot-api-docs enums.md, "Account and Symbol Permissions".
# TRD_GRP_080 is a later exact group name. It is not in that list, and a
# prefix such as "TRD_GRP_" is not a permission.
DOCUMENTED_TRD_GROUPS = frozenset(f"TRD_GRP_{index:03d}" for index in range(2, 26))
_EXACT_TRD_GROUP = re.compile(r"TRD_GRP_[0-9]{3}\Z")
# Master account only. ``subUserId`` names a child; it does not identify the
# key that signed ``GET /api/v3/account``.
SUB_ACCOUNT_LIST_ENDPOINT = "GET /sapi/v1/sub-account/list"

# Cursor pinned by the PR171 gate test. Startup reads strictly above this id.
# It is not an entry parameter and it is not a list of decision ids.
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

# Environment facts this module does not attest. A bound REST snapshot is not
# one of them. A ``closed`` canonical row is terminal and is not a blocker.
EXTERNAL_BLOCKERS = (
    "mcp_connector_has_no_order_list_oco",
    "fixed_egress_does_not_represent_mcp_connector",
    "paper_off_and_consumer_are_environment_checks",
    "live_flag_and_kill_switch_are_not_changed_here",
)

# Machine ledger reported by the operator. Not a file in this checkout.
REPLAY_SCOPE = {
    "cursor_floor": REPLAY_FLOOR_DECISION_ID,
    "frozen_decision_id": FROZEN_DECISION_ID,
    "frozen_client_id": FROZEN_CLIENT_ID,
    "decisions_above_floor_in_repo": [],
    "execution_log_rows_in_repo": None,
    "order_events_rows_in_repo": None,
    "operator_reported": {
        "execution_log_rows": 20,
        "order_events_rows": 40,
        "cursor": REPLAY_FLOOR_DECISION_ID,
        "canonical_decision": FROZEN_DECISION_ID,
        "canonical_status": "closed",
    },
}

def acceptance_report() -> dict[str, Any]:
    """Environment is not attested. REST binding does not need a uid."""
    return {
        "arm": PURPOSE_LIVE_BNB_BOLL_FADE,
        "live_ready": False,
        "rest_book_bindable_without_uid": True,
        "blockers": list(EXTERNAL_BLOCKERS),
        "replay": dict(REPLAY_SCOPE),
    }


def replay_scope() -> dict[str, Any]:
    """What the replay floor covers, and what this checkout did not invent.

    The PR171 gate test pins the cursor at 147840. This repository has no
    production sqlite, so the operator's 20 ``execution_log`` rows and 40
    ``order_events`` are not copied here. No decision id above 147840 is
    stored in the repo, and none is added to a denylist.
    """
    return {
        "cursor_floor": REPLAY_SCOPE["cursor_floor"],
        "frozen_decision_id": REPLAY_SCOPE["frozen_decision_id"],
        "frozen_client_id": REPLAY_SCOPE["frozen_client_id"],
        "decisions_above_floor_in_repo": list(REPLAY_SCOPE["decisions_above_floor_in_repo"]),
        "execution_log_rows_in_repo": REPLAY_SCOPE["execution_log_rows_in_repo"],
        "order_events_rows_in_repo": REPLAY_SCOPE["order_events_rows_in_repo"],
        "operator_reported": dict(REPLAY_SCOPE["operator_reported"]),
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


def _decimal_text(raw: object) -> str | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        number = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def snapshot_usdt_free(snapshot: object) -> str | None:
    """USDT free from an account payload. Missing means unknown, not zero."""
    if not isinstance(snapshot, dict):
        return None
    return _balance_free(snapshot, "USDT")


def _balance_free(snapshot: dict[str, Any], asset: str) -> str | None:
    rows = snapshot.get("balances")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("asset") or "") != asset:
            continue
        return _decimal_text(row.get("free"))
    return None


def spot_account_uid(snapshot: object) -> str | None:
    """Uid from ``GET /api/v3/account`` when the payload has one.

    The published example includes ``uid``. The observed production payload
    did not. Absence is not the MCP uid.
    """
    if not isinstance(snapshot, dict) or "uid" not in snapshot:
        return None
    parsed = _nonneg_id(snapshot.get("uid"))
    if parsed is None:
        return None
    return str(parsed)


def classify_sub_account_list(payload: object) -> dict[str, Any]:
    """Read a master-account ``GET /sapi/v1/sub-account/list`` body. No network.

    ``subUserId`` names a child of the master key. It does not prove that the
    key which signed ``GET /api/v3/account`` is that child.
    """
    rows = payload.get("subAccounts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {
            "endpoint": SUB_ACCOUNT_LIST_ENDPOINT,
            "role": None,
            "sub_user_ids": [],
            "error": "not_a_sub_account_list",
        }
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _nonneg_id(row.get("subUserId"))
        if parsed is None:
            continue
        ids.append(str(parsed))
    return {
        "endpoint": SUB_ACCOUNT_LIST_ENDPOINT,
        "role": "master",
        "sub_user_ids": ids,
        "lists_mcp_uid": MCP_POSITION_UID in ids,
        "error": None,
    }


def permission_kind(token: object) -> str:
    """Classify one account permission token. A prefix is not a group.

    ``SPOT`` is the legacy spot permission. ``TRD_GRP_002`` through
    ``TRD_GRP_025`` are the groups published in the spot enums. Any other
    token is a group only when it is exactly ``TRD_GRP_`` and three digits,
    such as the live account's ``TRD_GRP_080``. ``TRD_GRP_``, ``TRD_GRP_08``,
    and ``TRD_GRP_080extra`` are rejected.
    """
    if not isinstance(token, str) or token != token.strip():
        return "rejected"
    if token == "SPOT":
        return "legacy_spot"
    if token in {"MARGIN", "LEVERAGED"}:
        return "other_documented"
    if token in DOCUMENTED_TRD_GROUPS:
        return "documented_group"
    if _EXACT_TRD_GROUP.fullmatch(token):
        return "exact_group"
    return "rejected"


def spot_permission_error(permissions: object) -> str | None:
    """Spot permission form. The group name does not by itself allow trading."""
    if not isinstance(permissions, list) or not permissions:
        return "rest_permission_missing"
    kinds = [permission_kind(item) for item in permissions]
    if any(kind == "rejected" for kind in kinds):
        return "rest_permission_rejected"
    if not any(kind in {"legacy_spot", "documented_group", "exact_group"} for kind in kinds):
        return "rest_permission_missing"
    return None


def restrictions_error(restrictions: object) -> str | None:
    """Key capability from ``GET /sapi/v1/account/apiRestrictions``.

    ``enableSpotAndMarginTrading`` is the spot-trading switch. ``enableReading``
    and ``ipRestrict`` must also be true. A trading-group name cannot replace
    a missing or false switch.
    """
    if not isinstance(restrictions, dict):
        return "rest_capability_missing"
    spot = restrictions.get("enableSpotAndMarginTrading")
    if spot is None:
        return "rest_capability_missing"
    if spot is not True:
        return "rest_capability_disabled"
    if restrictions.get("enableReading") is not True:
        return "rest_reading_disabled"
    if restrictions.get("ipRestrict") is not True:
        return "rest_ip_restrict_disabled"
    return None


def funding_block(free: object, minimum: object, planned: object = None) -> str | None:
    """Refuse when the REST USDT free balance cannot fund the minimum order.

    ``minimum`` is the existing 20 USDT floor. A planned quote above the free
    balance is the same refusal. The operator book is not a balance.
    """
    free_text = _decimal_text(free)
    minimum_text = _decimal_text(minimum)
    if free_text is None or minimum_text is None:
        return None
    if Decimal(free_text) < Decimal(minimum_text):
        return "funding_block"
    planned_text = None if planned in (None, "") else _decimal_text(planned)
    if planned_text is not None and Decimal(planned_text) > Decimal(free_text):
        return "funding_block"
    return None


def bind_rest_book(
    snapshot: object,
    expected: object,
) -> tuple[dict[str, Any] | None, str | None]:
    """Bind an independent REST book. The MCP uid is never inferred.

    ``GET /api/v3/account`` supplies ``accountType``, ``canTrade``, the
    permission list, and the USDT free balance. ``GET /sapi/v1/account/apiRestrictions``
    supplies ``enableSpotAndMarginTrading``. Legacy ``permissions: ["SPOT"]``
    remains valid only together with that switch. ``uid`` ``1275399777`` may
    be recorded; ``uid`` ``1289917075`` is the MCP Agentic sub-account and
    does not bind this book.
    """
    if not isinstance(snapshot, dict):
        return None, "account_snapshot_required"
    if not isinstance(expected, dict):
        return None, "rest_balance_unbound"
    account_type = str(snapshot.get("accountType") or "").strip()
    expected_type = str(expected.get("account_type") or "SPOT").strip() or "SPOT"
    if not account_type or account_type != expected_type:
        return None, "rest_identity_incomplete"
    if snapshot.get("canTrade") is not True:
        return None, "account_cannot_trade"
    permission_error = spot_permission_error(snapshot.get("permissions"))
    if permission_error:
        return None, permission_error
    capability_error = restrictions_error(snapshot.get("restrictions"))
    if capability_error:
        return None, capability_error
    usdt = _balance_free(snapshot, "USDT")
    expected_usdt = _decimal_text(expected.get("usdt_free"))
    if usdt is None or expected_usdt is None:
        return None, "rest_balance_unbound"
    if usdt != expected_usdt:
        return None, "rest_balance_mismatch"
    uid = spot_account_uid(snapshot)
    if uid == MCP_POSITION_UID:
        return None, "account_book_mismatch"
    expected_uid = expected.get("uid")
    if expected_uid not in (None, ""):
        if uid is None or uid != str(expected_uid).strip():
            return None, "rest_uid_mismatch"
    permissions = [str(item) for item in snapshot.get("permissions") or []]
    return {
        "book": REST_BOOK,
        "endpoint": SPOT_ACCOUNT_ENDPOINT,
        "restrictions_endpoint": API_RESTRICTIONS_ENDPOINT,
        "account_type": account_type,
        "can_trade": True,
        "permissions": permissions,
        "enable_spot_and_margin_trading": True,
        "usdt_free": usdt,
        "uid": uid,
        "same_as_mcp_uid": False,
    }, None


def account_book_block(
    snapshot: object,
    book: object,
    expected: object = None,
) -> str | None:
    """Refuse a live BUY whose selected book is unbound or mixed.

    ``canTrade`` alone is not an identity. An env uid is not a snapshot.
    Selecting the MCP book does not place a native OCO. Selecting the REST
    book does not require uid ``1289917075``.
    """
    selected = str(book or "").strip()
    if not selected:
        return "account_book_unselected"
    if selected == MCP_BOOK:
        uid = spot_account_uid(snapshot) if isinstance(snapshot, dict) else None
        if uid is not None and uid != MCP_POSITION_UID:
            return "account_book_mismatch"
        return "mcp_has_no_native_oco"
    if selected != REST_BOOK:
        return "account_book_mismatch"
    _bound, error = bind_rest_book(snapshot, expected)
    return error


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
