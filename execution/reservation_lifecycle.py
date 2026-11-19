"""Bind a regime-v2 reservation to one gateway buy and its later position row.

The gateway calls these functions. They do not choose an order price, cancel
an order, or flatten a position. ``unknown`` is not released here.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any, Mapping

from decision_engine.core.regime_v2_reservation import (
    attach_decision_id,
    confirm_reservation,
    get_reservation,
    release_reservation,
    reservation_db_path,
    reservation_for_decision,
)
from execution.ledger import _stored_qty_positive, _stored_qty_zero

_FLAT_STATUSES = frozenset({"closed", "oco_filled", "closed_canceled"})
# Binance codes whose documented meaning is that this request was rejected
# and no order was created. HTTP status alone is not one of these.
# -2010 is a family of matching-engine texts, not one of these codes.
_DEFINITE_NEW_ORDER_REJECT_CODES = frozenset({-1013, -1015, -1111, -1121})
# Exact -2010 texts that reject this request and do not say a client id or an
# order is already present. Any other -2010 text stays occupied.
_DEFINITE_2010_NO_ORDER_MESSAGES = frozenset({
    "Account has insufficient balance for requested action.",
    "Market is closed.",
    "Price * QTY is zero or less.",
    "IcebergQty exceeds QTY.",
    "This action is disabled on this account.",
    "Unsupported order combination",
    "Order would trigger immediately.",
    "Stop price would trigger immediately.",
    "Order would immediately match and take.",
    "The relationship of the prices for the orders is not correct.",
    "This symbol is not permitted for this account.",
    "This symbol is restricted for this account.",
    "This account may not place or cancel orders.",
    "Stop loss orders are not supported for this symbol.",
    "Take profit orders are not supported for this symbol.",
    "Iceberg orders are not supported for this symbol.",
    "Quote order qty market orders are not support for this symbol.",
    "Trailing stop orders are not supported for this symbol.",
    "Order cancel-replace is not supported.",
    "OCO orders are not supported for this symbol.",
})
_UNPROVEN_2010_MARKERS = (
    "duplicate",
    "already",
    "clientorderid",
    "client order",
    "clordid",
    "existing order",
)
# Execution result unknown even when the transport status is 400.
_UNKNOWN_EXECUTION_CODES = frozenset({-1000, -1001, -1006, -1007, -1008})


def position_is_definite_flat(row: Mapping[str, Any], reservation: Mapping[str, Any]) -> bool:
    """Flat only when status, zero quantity, and zero residual are all present.

    A missing quantity, a non-zero quantity, or a missing residual is not proof.
    """
    status = str(row.get("status") or "")
    if status not in _FLAT_STATUSES:
        return False
    if row.get("quantity") in (None, "") or row.get("residual_qty") in (None, ""):
        return False
    if not _stored_qty_zero(row.get("quantity")) or not _stored_qty_zero(row.get("residual_qty")):
        return False
    symbol = str(row.get("symbol") or "").strip().upper()
    reserved_symbol = str(reservation.get("symbol") or "").strip().upper()
    if not symbol or symbol != reserved_symbol:
        return False
    purpose = str(row.get("purpose") or "").strip()
    reserved_purpose = str(reservation.get("purpose") or "").strip()
    if not purpose or purpose != reserved_purpose:
        return False
    decision_id = str(row.get("decision_log_id") or "").strip()
    reserved_decision = str(reservation.get("decision_log_id") or "").strip()
    if not decision_id or (reserved_decision and decision_id != reserved_decision):
        return False
    return True


def position_occupies_slot(row: Mapping[str, Any]) -> bool:
    """Same keep rule as ``ExecutionLedger.list_binance_spot_buy_blocks``."""
    status = str(row.get("status") or "")
    if status not in _FLAT_STATUSES:
        return True
    if _stored_qty_zero(row.get("quantity")) or _stored_qty_positive(row.get("residual_qty")):
        return True
    return False


def _now() -> int:
    return int(time.time() * 1000)


def _identity(sig: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    extras = sig.get("extras") if isinstance(sig.get("extras"), dict) else {}
    key = str(extras.get("reservation_key") or sig.get("reservation_key") or "").strip()
    symbol = str(
        extras.get("binance_symbol") or sig.get("token_address") or ""
    ).strip().upper()
    purpose = str(sig.get("purpose") or "").strip()
    decision_id = str(sig.get("decision_log_id") or "").strip()
    mode = str(sig.get("mode") or "").strip().lower()
    return key, symbol, purpose, decision_id, mode


def _tracked(sig: Mapping[str, Any]) -> bool:
    key, _symbol, _purpose, _decision_id, _mode = _identity(sig)
    return bool(key or reservation_db_path(os.environ))


def bind_reservation_before_submit(sig: Mapping[str, Any]) -> str | None:
    """Refuse a buy whose reservation cannot be owned by this consumer mode.

    A matching live row is marked ``submit_pending`` before the adapter runs.
    A mismatch does not release the row. No key and no reservation database
    leaves the older consumer path unchanged.
    """
    if not _tracked(sig):
        return None
    path = reservation_db_path(os.environ)
    key, symbol, purpose, decision_id, mode = _identity(sig)
    if not path or not key or not symbol or not purpose or not decision_id:
        return "reservation_identity_missing"
    row = get_reservation(path, key)
    if row is None:
        return "reservation_missing"
    if row.get("released_at_ms") is not None or str(row.get("status") or "") == "released":
        return "reservation_released"
    if str(row.get("symbol") or "").upper() != symbol or str(row.get("purpose") or "") != purpose:
        return "reservation_identity_mismatch"
    stored_decision = str(row.get("decision_log_id") or "").strip()
    if stored_decision and stored_decision != decision_id:
        return "reservation_identity_mismatch"
    if not attach_decision_id(
        path, key, decision_log_id=decision_id, symbol=symbol, purpose=purpose
    ):
        return "reservation_store_error"
    row_mode = str(row.get("mode") or "live")
    permitted = int(row.get("live_order_permitted") or 0) == 1
    if mode == "dry_run":
        if row_mode != "dry_run" or permitted:
            return "reservation_mode_mismatch"
        return None
    if mode != "live_stub":
        return "reservation_mode_unconfirmed"
    if row_mode != "live" or not permitted:
        return "live_order_not_permitted"
    token = secrets.token_hex(16)
    ok, reason = confirm_reservation(
        path,
        key,
        now_ms=_now(),
        phase="submit_pending",
        symbol=symbol,
        purpose=purpose,
        decision_log_id=decision_id,
        submit_owner=token,
    )
    if not ok:
        return reason or "reservation_store_error"
    if isinstance(sig, dict):
        sig["_reservation_submit_owner"] = token
    return None


def _code_2010_proves_no_order(msg: object) -> bool:
    """True only for an exact -2010 text that does not imply an existing order."""
    if not isinstance(msg, str):
        return False
    text = msg.strip()
    if not text:
        return False
    lowered = text.casefold()
    if any(marker in lowered for marker in _UNPROVEN_2010_MARKERS):
        return False
    return text in _DEFINITE_2010_NO_ORDER_MESSAGES


def _proven_exchange_rejection(result: Mapping[str, Any]) -> bool:
    if not result.get("order_post_attempted"):
        return False
    if result.get("submitted") or result.get("exchange_order_id") or result.get("arm_position"):
        return False
    err = result.get("exchange_error")
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return False
    if code in _UNKNOWN_EXECUTION_CODES:
        return False
    if err.get("status") != 400:
        return False
    if code in _DEFINITE_NEW_ORDER_REJECT_CODES:
        return True
    if code == -2010:
        return _code_2010_proves_no_order(err.get("msg"))
    return False


def _result_occupies(result: Mapping[str, Any]) -> bool:
    if result.get("submitted") or result.get("exchange_order_id"):
        return True
    arm = result.get("arm_position")
    return isinstance(arm, dict)


def settle_reservation_after_execute(
    sig: Mapping[str, Any],
    result: Mapping[str, Any] | None,
) -> str | None:
    """Confirm, release, or leave the row after the adapter returns.

    Returns a note for the receipt. Identity mismatch and store errors do not
    release. A post that was attempted but not proven rejected becomes
    ``unknown`` and stays occupied.
    """
    if not _tracked(sig):
        return None
    path = reservation_db_path(os.environ)
    key, symbol, purpose, decision_id, mode = _identity(sig)
    if not path or not key or not symbol or not purpose:
        return "reservation_identity_missing"
    row = get_reservation(path, key)
    if row is None:
        return "reservation_missing"
    if str(row.get("symbol") or "").upper() != symbol or str(row.get("purpose") or "") != purpose:
        return "reservation_identity_mismatch"
    row_mode = str(row.get("mode") or "live")
    if mode == "dry_run" and row_mode != "dry_run":
        return "reservation_mode_mismatch"
    if mode == "live_stub" and row_mode != "live":
        return "reservation_mode_mismatch"
    owner = str(sig.get("_reservation_submit_owner") or "").strip() or None
    if result is not None and _result_occupies(result):
        ok, reason = confirm_reservation(
            path,
            key,
            now_ms=_now(),
            phase="held",
            symbol=symbol,
            purpose=purpose,
            decision_log_id=decision_id or None,
            submit_owner=owner,
        )
        return None if ok else (reason or "reservation_store_error")
    posted = result is not None and bool(result.get("order_post_attempted"))
    if posted and not _proven_exchange_rejection(result or {}):
        ok, reason = confirm_reservation(
            path,
            key,
            now_ms=_now(),
            phase="unknown",
            symbol=symbol,
            purpose=purpose,
            decision_log_id=decision_id or None,
            submit_owner=owner,
        )
        return None if ok else (reason or "reservation_store_error")
    reason_name = (
        "exchange_rejected"
        if result is not None and _proven_exchange_rejection(result)
        else "definite_no_fill"
    )
    released = release_reservation(
        path,
        key,
        now_ms=_now(),
        reason=reason_name,
        symbol=symbol,
        purpose=purpose,
        decision_log_id=decision_id or None,
        submit_owner=owner,
    )
    if released:
        return None
    current = get_reservation(path, key)
    if current is not None and str(current.get("status") or "") == "unknown":
        return None
    return "reservation_release_refused"


def settle_reservation_for_position(row: Mapping[str, Any]) -> str | None:
    """Keep the slot while the ledger row still blocks, and release a flat close."""
    path = reservation_db_path(os.environ)
    decision_id = str(row.get("decision_log_id") or "").strip()
    if not path or not decision_id:
        return None
    found, err = reservation_for_decision(path, decision_id)
    if err:
        return err
    if found is None or found.get("released_at_ms") is not None:
        return None
    key = str(found.get("signal_key") or "")
    symbol = str(found.get("symbol") or row.get("symbol") or "")
    purpose = str(found.get("purpose") or row.get("purpose") or "")
    if not position_is_definite_flat(row, found):
        if str(found.get("status") or "") == "released":
            return None
        ok, reason = confirm_reservation(
            path,
            key,
            now_ms=_now(),
            phase="held",
            symbol=symbol,
            purpose=purpose,
            decision_log_id=decision_id,
        )
        return None if ok else (reason or "reservation_store_error")
    if str(found.get("status") or "") == "unknown":
        ok, reason = confirm_reservation(
            path,
            key,
            now_ms=_now(),
            phase="held",
            symbol=symbol,
            purpose=purpose,
            decision_log_id=decision_id,
        )
        if not ok:
            return reason or "reservation_store_error"
    released = release_reservation(
        path,
        key,
        now_ms=_now(),
        reason="flat_close",
        symbol=symbol,
        purpose=purpose,
        decision_log_id=decision_id,
    )
    return None if released else "reservation_release_refused"
