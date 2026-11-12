"""Pre-trade gates for the live Binance spot arms. No network and no secrets.

``live_bnb_boll_fade`` and ``live_bnb_boll_classic`` may each hold several
symbols at once (一臂多仓). A healthy open blocks another BUY of that same
symbol only. It does not block a different symbol, and it does not open a
second position on the symbol already held (not 一币多仓). The other arm
may hold a different symbol. It may not hold the same symbol, healthy or
unprotected.

``BINANCE_SPOT_ALLOW_MULTI_SYMBOL`` defaults on. ``0`` restores one open
position per arm. A caller that omits the buy symbol fails closed while a
healthy open row exists.

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

import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from execution.verified_fet import VERIFIED_FET_EXIT

PURPOSE_LIVE_BNB_BOLL_FADE = "live_bnb_boll_fade"
PURPOSE_LIVE_BNB_BOLL_CLASSIC = "live_bnb_boll_classic"
LIVE_BNB_SPOT_PURPOSES = frozenset(
    {
        PURPOSE_LIVE_BNB_BOLL_FADE,
        PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    }
)
# Several symbols on one arm. Unset is on. This is not same-symbol pyramiding.
ALLOW_MULTI_SYMBOL_ENV = "BINANCE_SPOT_ALLOW_MULTI_SYMBOL"
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
_TERMINAL_POSITION = frozenset({"closed", "closed_dust", "oco_filled", "closed_canceled"})
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

def is_live_bnb_spot_purpose(purpose: object) -> bool:
    """True for a Binance spot arm this adapter may fill."""
    return str(purpose or "").strip() in LIVE_BNB_SPOT_PURPOSES


def sole_live_bnb_purpose(
    accept_purposes: frozenset[str] | set[str] | None,
) -> str | None:
    """The one live spot arm on this process, or None.

    Fade and classic each run as their own consumer. A process that lists
    both, or mixes either with a paper purpose, is not a sole arm.
    """
    if not accept_purposes or len(accept_purposes) != 1:
        return None
    purpose = next(iter(accept_purposes))
    if purpose not in LIVE_BNB_SPOT_PURPOSES:
        return None
    return purpose


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


def row_quote_usdt(row: object) -> Decimal | None:
    """Quote already taken by one spot row. Missing price or qty is unknown.

    A resting maker keeps the unfilled base reserved at its limit price and
    the filled base at the fill average. A terminal row is not reserved here;
    the caller skips those statuses.
    """
    if not isinstance(row, dict):
        return None
    if str(row.get("status") or "") in {"maker_resting", "maker_cancel_unknown"}:
        order_qty = _decimal_text(row.get("entry_order_qty"))
        limit_px = _decimal_text(row.get("entry_limit_price"))
        if order_qty is None or limit_px is None:
            return None
        filled = _decimal_text(row.get("quantity"))
        outstanding = Decimal(order_qty) - (Decimal(filled) if filled is not None else Decimal(0))
        if outstanding < 0:
            outstanding = Decimal(0)
        quote = outstanding * Decimal(limit_px)
        entry = _decimal_text(row.get("entry_price"))
        if filled is not None and entry is not None:
            quote += Decimal(filled) * Decimal(entry)
        return quote
    qty = _decimal_text(row.get("quantity"))
    price = _decimal_text(row.get("entry_price"))
    if qty is None or price is None:
        return None
    return Decimal(qty) * Decimal(price)


def partial_dust_residual(row: dict[str, Any]) -> bool:
    """A sell left a below-min remainder. That row is not a flat close.

    A dust archive with no exit fill is the older whole-qty abandon and
    stays terminal. This one still blocks the same arm.
    """
    if str(row.get("status") or "") != "closed_dust":
        return False
    residual = _decimal_text(row.get("residual_qty"))
    sold = _decimal_text(row.get("exit_filled_qty"))
    if residual is None or sold is None:
        return False
    return Decimal(residual) > 0 and Decimal(sold) > 0


def _nonterminal_spot_row(row: dict[str, Any]) -> bool:
    if partial_dust_residual(row):
        return True
    status = str(row.get("status") or "")
    if not status or status == "flat" or status in _TERMINAL_POSITION:
        return False
    return True


def reserved_quote_usdt(rows: object) -> Decimal:
    """Sum priced non-terminal quote. Unpriced rows add nothing.

    The symbol gate still sees those rows. A sealed snapshot does not.
    """
    total = Decimal(0)
    if not isinstance(rows, list):
        return total
    for row in rows:
        if not isinstance(row, dict) or not _nonterminal_spot_row(row):
            continue
        quote = row_quote_usdt(row)
        if quote is None:
            continue
        total += quote
    return total


def funding_after_reservation(
    free: object,
    minimum: object,
    planned: object,
    reserved: object,
) -> str | None:
    """``funding_block`` against free minus already reserved quote.

    Unknown free stays the old check. Reserved quote cannot raise the free
    balance. The 20 USDT floor is unchanged.
    """
    free_text = _decimal_text(free)
    reserved_text = _decimal_text(reserved)
    if free_text is None or reserved_text is None:
        return funding_block(free, minimum, planned)
    available = Decimal(free_text) - Decimal(reserved_text)
    if available < 0:
        available = Decimal(0)
    text = format(available, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return funding_block(text or "0", minimum, planned)


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


def allow_multi_symbol_per_arm() -> bool:
    """Several distinct symbols per arm. Default on.

    ``BINANCE_SPOT_ALLOW_MULTI_SYMBOL``: unset or blank is on.
    ``0`` / ``false`` / ``no`` / ``off`` restores one open position per arm.
    Any other value is off. A second BUY of a symbol the arm already holds
    stays blocked either way.
    """
    raw = os.environ.get(ALLOW_MULTI_SYMBOL_ENV)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _symbol_key(raw: object) -> str | None:
    if isinstance(raw, str):
        text = raw.strip().upper()
        if text:
            return text
    return None


def _same_symbol_open_block(row: dict[str, Any], symbol: str | None) -> str | None:
    """Healthy open blocks that symbol, or the whole arm when multi-symbol is off.

    A missing buy symbol or a held row with no symbol fails closed.
    """
    if not allow_multi_symbol_per_arm():
        return "position_open_blocks_buy"
    buy = _symbol_key(symbol)
    held = _symbol_key(row.get("symbol"))
    if buy is None or held is None or buy == held:
        return "position_open_blocks_buy"
    return None


def _row_blocks_arm(row: dict[str, Any], arm: str) -> bool:
    """True when ``row`` belongs to ``arm``.

    A tagged row blocks only that purpose. An untagged row is a legacy fade
    hold: it still blocks fade, and it does not block classic.
    """
    raw = str(row.get("purpose") or "").strip()
    if raw:
        return raw == arm
    return arm == PURPOSE_LIVE_BNB_BOLL_FADE


def buy_exposure_block(
    positions: object,
    canonical: object,
    *,
    purpose: str | None = None,
    symbol: str | None = None,
) -> str | None:
    """Block a new BUY while this arm's exposure is naked, unbound, or unfinished.

    With ``BINANCE_SPOT_ALLOW_MULTI_SYMBOL`` on (the default), one arm may
    hold several symbols. A healthy ``oco_working``, verified ``open``, or
    ``exit_submitted`` row blocks only a BUY of that same symbol. A different
    symbol is not refused for that row. This is not a second position on the
    same symbol. The other arm's rows are not read here.

    Omitting ``symbol`` fails closed while any healthy open row exists, which
    is the previous one-open-per-arm result. ``0`` on the flag restores that
    result even when the symbols differ.

    Unsafe rows still block the whole arm: ``held_naked``, a non-terminal
    row with no entry order id, ``qty_unconfirmed``, ``oco_failed``,
    ``oco_unknown``, and an ``open`` that is not a verified exchange OCO.
    A flat book is not by itself permission to buy. Terminal rows are
    ignored. This does not write a close for the manual FET fill.

    ``purpose`` defaults to ``live_bnb_boll_fade`` so existing callers keep
    the fade book.
    """
    requested = str(purpose or "").strip()
    arm = requested if requested in LIVE_BNB_SPOT_PURPOSES else PURPOSE_LIVE_BNB_BOLL_FADE
    rows = canonical if isinstance(canonical, list) else []
    for row in rows:
        if not isinstance(row, dict) or not _row_blocks_arm(row, arm):
            continue
        status = str(row.get("status") or "")
        if status == "held_naked":
            return "naked_holds_new_buys"
        if status and status not in (_TERMINAL_POSITION | {"flat", "closed"}):
            if not str(row.get("entry_order_id") or "").strip():
                return "unbound_position"
    held = positions if isinstance(positions, list) else []
    for row in held:
        if not isinstance(row, dict) or not _row_blocks_arm(row, arm):
            continue
        status = str(row.get("status") or "")
        if partial_dust_residual(row):
            return "unprotected_position_blocks_buy"
        if not status or status in _TERMINAL_POSITION:
            continue
        if status == "qty_unconfirmed":
            return "parent_fill_incomplete"
        if status == "maker_cancel_unknown":
            return "maker_resting_blocks_buy"
        if status == "maker_resting":
            if row_quote_usdt(row) is None:
                return "maker_resting_blocks_buy"
            filled = _decimal_text(row.get("quantity"))
            if filled is not None and Decimal(filled) > 0:
                return "unprotected_position_blocks_buy"
            if _same_symbol_open_block(row, symbol):
                return "maker_resting_blocks_buy"
            continue
        if status == "time_stop_unknown":
            return "time_stop_unconfirmed_blocks_buy"
        if status in {"oco_failed", "oco_unknown"}:
            return "oco_failed_blocks_buy"
        if status == "open":
            if str(row.get("protection") or "") != "exchange_oco" or not _position_ids_verified(row):
                return "unprotected_position_blocks_buy"
            blocked = _same_symbol_open_block(row, symbol)
            if blocked:
                return blocked
            continue
        if status in {"oco_working", "exit_submitted"}:
            blocked = _same_symbol_open_block(row, symbol)
            if blocked:
                return blocked
            continue
        return "unprotected_position_blocks_buy"
    return None


def account_unresolved_halt(rows: object) -> str | None:
    """Block every new BUY while this account has an unresolved spot row.

    A verified working OCO needs the list id, both leg ids, and a positive
    quantity. A status write is not that evidence. A positive ``residual_qty``
    stays unresolved under every status. There is no authenticated human-clear
    record on this book, so a status change alone does not clear the halt.
    """
    held = rows if isinstance(rows, list) else []
    for row in held:
        if not isinstance(row, dict):
            continue
        purpose = str(row.get("purpose") or "").strip()
        if purpose and purpose not in LIVE_BNB_SPOT_PURPOSES:
            continue
        reason = _unresolved_row_reason(row)
        if reason:
            return reason
    return None


def _zeroed_entry(row: dict[str, Any]) -> bool:
    """A local write of quantity 0 and no residual does not close an entry.

    A real full exit has ``exit_filled_qty`` covering ``entry_order_qty``.
    There is no separate human-clear record.
    """
    if not str(row.get("entry_order_id") or "").strip():
        return False
    if str(row.get("quantity") or "").strip() not in {"0", "0.0"}:
        return False
    if _positive_qty(row.get("residual_qty")):
        return False
    original = _decimal_text(row.get("entry_order_qty"))
    sold = _decimal_text(row.get("exit_filled_qty"))
    if original is not None and sold is not None and Decimal(sold) >= Decimal(original):
        return False
    return True


def _positive_qty(raw: object) -> bool:
    text = _decimal_text(raw)
    return text is not None and Decimal(text) > 0


_LIVE_LEG_STATUS = frozenset({"NEW", "PARTIALLY_FILLED", "PENDING_NEW"})


def oco_readback_covers(
    payload: object,
    *,
    symbol: str,
    held_net: Decimal,
    list_id: object,
) -> bool:
    """True only for this call's exchange list, not for fields stored on the row.

    The list must be working, name the same symbol, have two live legs, and
    the leg quantity must cover ``held_net``. A smaller local quantity does
    not lower that bar.
    """
    if not isinstance(payload, dict) or held_net <= 0:
        return False
    if str(payload.get("listOrderStatus") or "").upper() != "EXECUTING":
        return False
    if str(payload.get("listStatusType") or "").upper() not in {"EXEC_STARTED", "EXECUTING"}:
        return False
    if str(payload.get("orderListId") or "") != str(list_id or "").strip():
        return False
    reported = str(payload.get("symbol") or "").strip().upper()
    if not reported or reported != str(symbol or "").strip().upper():
        return False
    reports = payload.get("orderReports")
    if not isinstance(reports, list):
        return False
    above = None
    below = None
    for row in reports:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("type") or "").upper()
        if above is None and kind == "LIMIT_MAKER":
            above = row
        if below is None and kind == "STOP_LOSS_LIMIT":
            below = row
    if above is None or below is None:
        return False
    covered = Decimal("0")
    for leg in (above, below):
        if str(leg.get("symbol") or reported).strip().upper() != reported:
            return False
        if str(leg.get("status") or "").upper() not in _LIVE_LEG_STATUS:
            return False
        if _nonneg_id(leg.get("orderId")) is None:
            return False
        qty = _decimal_text(leg.get("origQty"))
        if qty is None:
            return False
        covered = max(covered, Decimal(qty))
    return covered >= held_net


def held_net_qty(row: dict[str, Any]) -> Decimal:
    """Original net when it was recorded. A later local quantity is not the target."""
    for key in ("entry_order_qty", "quantity"):
        text = _decimal_text(row.get(key))
        if text is not None and Decimal(text) > 0:
            return Decimal(text)
    return Decimal("0")


def _unresolved_row_reason(row: dict[str, Any]) -> str | None:
    if _positive_qty(row.get("residual_qty")):
        return "unprotected_position_blocks_buy"
    if partial_dust_residual(row):
        return "unprotected_position_blocks_buy"
    status = str(row.get("status") or "")
    if _zeroed_entry(row):
        return "unprotected_position_blocks_buy"
    if status == "oco_working":
        if _position_ids_verified(row) and str(row.get("protection") or "") == "exchange_oco" and _positive_qty(row.get("quantity")):
            return None
        return "oco_failed_blocks_buy"
    if status == "open" and _position_ids_verified(row) and str(row.get("protection") or "") == "exchange_oco" and _positive_qty(row.get("quantity")):
        return None
    if status == "closed_canceled" and _positive_qty(row.get("quantity")):
        return "unprotected_position_blocks_buy"
    if not status or status in _TERMINAL_POSITION or status == "flat":
        return None
    if status == "held_naked":
        return "naked_holds_new_buys"
    if status == "qty_unconfirmed":
        return "parent_fill_incomplete"
    if status in {"oco_failed", "oco_unknown"}:
        return "oco_failed_blocks_buy"
    if status == "time_stop_unknown":
        return "time_stop_unconfirmed_blocks_buy"
    if status == "maker_cancel_unknown":
        return "maker_resting_blocks_buy"
    if status == "maker_resting":
        filled = _decimal_text(row.get("quantity"))
        if filled is not None and Decimal(filled) > 0:
            return "unprotected_position_blocks_buy"
        return None
    if status == "exit_submitted":
        return "unprotected_position_blocks_buy"
    return "unprotected_position_blocks_buy"


def cross_arm_symbol_block(
    rows: object,
    *,
    purpose: str | None = None,
    symbol: str | None = None,
) -> str | None:
    """Refuse a BUY when the other arm already has this symbol open.

    Healthy and unprotected non-terminal rows both count. A different symbol
    on the other arm does not. A missing buy symbol, or a held row with no
    symbol, fails closed. Same-arm rows are ignored; ``buy_exposure_block``
    still owns those. ``MULTI_SYMBOL`` stays on for distinct symbols.
    """
    requested = str(purpose or "").strip()
    arm = requested if requested in LIVE_BNB_SPOT_PURPOSES else PURPOSE_LIVE_BNB_BOLL_FADE
    held = rows if isinstance(rows, list) else []
    for row in held:
        if not isinstance(row, dict) or _row_blocks_arm(row, arm):
            continue
        if not _nonterminal_spot_row(row):
            continue
        other = str(row.get("purpose") or "").strip()
        if other and other not in LIVE_BNB_SPOT_PURPOSES and other != "":
            continue
        buy = _symbol_key(symbol)
        held_symbol = _symbol_key(row.get("symbol"))
        if buy is None or held_symbol is None or buy == held_symbol:
            return "cross_arm_symbol_blocks_buy"
    return None
