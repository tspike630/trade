"""REST payloads checked against binance-sdk-margin-trading 8.12.0 and official REST docs.

The SDK speaks snake_case Python arguments. The signed REST query uses the
camelCase names below. This module does not import the SDK and does not send
HTTP. OTO / OTOCO builders exist so their conditional parameters are tested;
the track does not submit them.

Evidence:
- package version 8.12.0 in ``clients/margin_trading/pyproject.toml``
  https://github.com/binance/binance-connector-python/blob/master/clients/margin_trading/pyproject.toml
- changelog 8.12.0 (2026-11-17)
  https://github.com/binance/binance-connector-python/blob/master/clients/margin_trading/CHANGELOG.md
- Official Trade / Borrow-Repay / Account Margin REST API catalogs:
  https://developers.binance.com/en/docs/catalog/core-trading-margin-trading/api/rest-api/trade
  https://developers.binance.com/en/docs/catalog/core-trading-margin-trading/api/rest-api/borrow-repay
  https://developers.binance.com/en/docs/catalog/core-trading-margin-trading/api/rest-api/account
  https://developers.binance.com/en/docs/products/margin-trading/best-practice

Key wire constraints:
- ``isIsolated`` is ``"FALSE"`` for Cross Margin. ``symbol`` on borrow-repay is
  isolated-only and is omitted.
- ``GET /sapi/v1/margin/orderList`` specifies ``symbol`` as mandatory for isolated
  margin and **not supported for cross margin**; ``build_query_oco_payload`` omits
  ``symbol`` accordingly.
- ``GET /sapi/v1/margin/borrow-repay`` requires ``txId`` or ``startTime``.
- Order and OCO ``sideEffectType`` is fixed to ``NO_SIDE_EFFECT``; borrow and repay
  are always explicit ledgered operations.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import re

from execution.margin_cross.money import api_amount

SDK_VERSION = "8.12.0"
CROSS_ISOLATED_FLAG = "FALSE"
NO_SIDE_EFFECT = "NO_SIDE_EFFECT"
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,36}$")

ORDER_TYPES = frozenset(
    {
        "LIMIT",
        "MARKET",
        "STOP_LOSS",
        "STOP_LOSS_LIMIT",
        "TAKE_PROFIT",
        "TAKE_PROFIT_LIMIT",
        "LIMIT_MAKER",
    }
)
ORDER_SIDES = frozenset({"BUY", "SELL"})
TIME_IN_FORCE = frozenset({"GTC", "IOC", "FOK"})
ORDER_SIDE_EFFECTS = frozenset(
    {"NO_SIDE_EFFECT", "MARGIN_BUY", "AUTO_REPAY", "AUTO_BORROW_REPAY"}
)
OTO_SIDE_EFFECTS = frozenset({"NO_SIDE_EFFECT", "MARGIN_BUY", "AUTO_BORROW_REPAY"})
OTO_WORKING_TYPES = frozenset({"LIMIT", "LIMIT_MAKER"})
OTO_PENDING_TYPES = ORDER_TYPES
OTOCO_PENDING_TYPES = frozenset({"LIMIT_MAKER", "STOP_LOSS", "STOP_LOSS_LIMIT"})

EVIDENCE_LINKS = (
    "https://github.com/binance/binance-connector-python/blob/master/clients/margin_trading/pyproject.toml",
    "https://github.com/binance/binance-connector-python/blob/master/clients/margin_trading/CHANGELOG.md",
    "https://github.com/binance/binance-connector-python/blob/master/clients/margin_trading/src/binance_sdk_margin_trading/rest_api/api/trade_api.py",
    "https://github.com/binance/binance-connector-python/blob/master/clients/margin_trading/src/binance_sdk_margin_trading/rest_api/api/borrow_repay_api.py",
    "https://github.com/binance/binance-connector-python/blob/master/clients/margin_trading/src/binance_sdk_margin_trading/rest_api/api/account_api.py",
    "https://developers.binance.com/en/docs/catalog/core-trading-margin-trading/api/rest-api/trade",
    "https://developers.binance.com/en/docs/catalog/core-trading-margin-trading/api/rest-api/borrow-repay",
    "https://developers.binance.com/en/docs/catalog/core-trading-margin-trading/api/rest-api/account",
    "https://developers.binance.com/en/docs/products/margin-trading/best-practice",
    "https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md",
)


def make_client_id(intent_id: str, role: str) -> str:
    digest = hashlib.sha256(f"{intent_id}|{role}".encode("utf-8")).hexdigest()
    client_id = "cm" + digest[:30]
    if CLIENT_ID_RE.fullmatch(client_id) is None:
        raise RuntimeError("client id formatter drifted")
    return client_id


def _reject_side_effect(value: str, allowed: frozenset[str]) -> str | None:
    if value not in allowed:
        return "side_effect_not_in_official_enum"
    if value != NO_SIDE_EFFECT:
        return "side_effect_forbidden_explicit_borrow_repay_only"
    return None


def build_borrow_payload(asset: str, amount: Decimal) -> dict[str, str]:
    if not asset or amount <= 0:
        raise ValueError("borrow_payload_invalid")
    return {
        "asset": asset,
        "isIsolated": CROSS_ISOLATED_FLAG,
        "amount": api_amount(amount),
        "type": "BORROW",
    }


def build_repay_payload(asset: str, amount: Decimal) -> dict[str, str]:
    if not asset or amount <= 0:
        raise ValueError("repay_payload_invalid")
    return {
        "asset": asset,
        "isIsolated": CROSS_ISOLATED_FLAG,
        "amount": api_amount(amount),
        "type": "REPAY",
    }


def build_limit_payload(
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    client_order_id: str,
    time_in_force: str = "GTC",
) -> dict[str, str]:
    if side not in ORDER_SIDES:
        raise ValueError("side_invalid")
    if time_in_force not in TIME_IN_FORCE:
        raise ValueError("time_in_force_invalid")
    if CLIENT_ID_RE.fullmatch(client_order_id) is None:
        raise ValueError("client_order_id_invalid")
    if quantity <= 0 or price <= 0:
        raise ValueError("limit_payload_invalid")
    return {
        "symbol": symbol,
        "isIsolated": CROSS_ISOLATED_FLAG,
        "side": side,
        "type": "LIMIT",
        "quantity": api_amount(quantity),
        "price": api_amount(price),
        "timeInForce": time_in_force,
        "newClientOrderId": client_order_id,
        "newOrderRespType": "FULL",
        "sideEffectType": NO_SIDE_EFFECT,
    }


def build_oco_payload(
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    limit_price: Decimal,
    stop_price: Decimal,
    stop_limit_price: Decimal,
    list_client_order_id: str,
    limit_client_order_id: str,
    stop_client_order_id: str,
) -> dict[str, str]:
    if side not in ORDER_SIDES:
        raise ValueError("side_invalid")
    for client_id in (
        list_client_order_id,
        limit_client_order_id,
        stop_client_order_id,
    ):
        if CLIENT_ID_RE.fullmatch(client_id) is None:
            raise ValueError("client_order_id_invalid")
    if min(quantity, limit_price, stop_price, stop_limit_price) <= 0:
        raise ValueError("oco_payload_invalid")
    return {
        "symbol": symbol,
        "isIsolated": CROSS_ISOLATED_FLAG,
        "side": side,
        "quantity": api_amount(quantity),
        "price": api_amount(limit_price),
        "stopPrice": api_amount(stop_price),
        "stopLimitPrice": api_amount(stop_limit_price),
        "stopLimitTimeInForce": "GTC",
        "listClientOrderId": list_client_order_id,
        "limitClientOrderId": limit_client_order_id,
        "stopClientOrderId": stop_client_order_id,
        "newOrderRespType": "FULL",
        "sideEffectType": NO_SIDE_EFFECT,
    }


def build_query_order_payload(symbol: str, client_order_id: str) -> dict[str, str]:
    return {
        "symbol": symbol,
        "isIsolated": CROSS_ISOLATED_FLAG,
        "origClientOrderId": client_order_id,
    }


def build_query_oco_payload(symbol: str, list_client_order_id: str) -> dict[str, str]:
    del symbol  # Cross-margin GET /sapi/v1/margin/orderList forbids symbol.
    return {
        "isIsolated": CROSS_ISOLATED_FLAG,
        "origClientOrderId": list_client_order_id,
    }


def build_cancel_oco_payload(symbol: str, list_client_order_id: str) -> dict[str, str]:
    return {
        "symbol": symbol,
        "isIsolated": CROSS_ISOLATED_FLAG,
        "listClientOrderId": list_client_order_id,
    }


def build_cancel_payload(symbol: str, client_order_id: str) -> dict[str, str]:
    return {
        "symbol": symbol,
        "isIsolated": CROSS_ISOLATED_FLAG,
        "origClientOrderId": client_order_id,
    }


def build_query_open_orders_payload(symbol: str) -> dict[str, str]:
    return {
        "symbol": symbol,
        "isIsolated": CROSS_ISOLATED_FLAG,
    }


def build_query_trades_payload(symbol: str, order_id: object) -> dict[str, object]:
    return {
        "symbol": symbol,
        "isIsolated": CROSS_ISOLATED_FLAG,
        "orderId": order_id,
    }


def build_max_borrow_payload(asset: str) -> dict[str, str]:
    return {"asset": asset}


def build_hourly_interest_payload(asset: str) -> dict[str, str]:
    return {"assets": asset, "isIsolated": CROSS_ISOLATED_FLAG}


def build_interest_history_payload(
    asset: str, *, start_time_ms: int | None = None, size: int = 50
) -> dict[str, str]:
    out = {"asset": asset, "size": str(int(size))}
    if start_time_ms is not None and start_time_ms >= 0:
        out["startTime"] = str(int(start_time_ms))
    return out


def build_borrow_query_payload(
    asset: str,
    borrow_or_repay: str,
    *,
    tx_id: int | None = None,
    start_time_ms: int | None = None,
) -> dict[str, str]:
    if borrow_or_repay not in {"BORROW", "REPAY"}:
        raise ValueError("borrow_query_type_invalid")
    payload = {"type": borrow_or_repay, "asset": asset}
    if tx_id is not None and tx_id > 0:
        payload["txId"] = str(int(tx_id))
    elif start_time_ms is not None and start_time_ms >= 0:
        payload["startTime"] = str(int(start_time_ms))
    return payload


def _require(payload: dict[str, object], field: str, reasons: list[str]) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or value == "":
        reasons.append(f"{field}_required")
        return ""
    return value


def validate_oto_payload(payload: dict[str, object]) -> list[str]:
    """Conditional REST requirements from the OTO docstring. Not submitted."""
    reasons: list[str] = []
    working_type = _require(payload, "workingType", reasons)
    pending_type = _require(payload, "pendingType", reasons)
    _require(payload, "symbol", reasons)
    _require(payload, "workingSide", reasons)
    _require(payload, "workingPrice", reasons)
    _require(payload, "workingQuantity", reasons)
    _require(payload, "pendingSide", reasons)
    _require(payload, "pendingQuantity", reasons)
    if payload.get("isIsolated") not in (None, CROSS_ISOLATED_FLAG):
        reasons.append("oto_isolated_rejected")
    effect = payload.get("sideEffectType", NO_SIDE_EFFECT)
    if not isinstance(effect, str):
        reasons.append("side_effect_invalid")
    else:
        problem = _reject_side_effect(effect, OTO_SIDE_EFFECTS)
        if problem:
            reasons.append(problem)
    if working_type and working_type not in OTO_WORKING_TYPES:
        reasons.append("working_type_invalid")
    if working_type == "LIMIT" and not payload.get("workingTimeInForce"):
        reasons.append("workingTimeInForce_required")
    if "workingIcebergQty" in payload and payload.get("workingTimeInForce") != "GTC":
        reasons.append("working_iceberg_requires_gtc")
    if pending_type and pending_type not in OTO_PENDING_TYPES:
        reasons.append("pending_type_invalid")
    if pending_type == "MARKET" and payload.get("quoteOrderQty") not in (None, ""):
        reasons.append("pending_market_quote_qty_forbidden")
    if pending_type == "LIMIT":
        if not payload.get("pendingPrice"):
            reasons.append("pendingPrice_required")
        if not payload.get("pendingTimeInForce"):
            reasons.append("pendingTimeInForce_required")
    if pending_type in {"STOP_LOSS", "TAKE_PROFIT"} and not (
        payload.get("pendingStopPrice") or payload.get("pendingTrailingDelta")
    ):
        reasons.append("pending_stop_or_trailing_required")
    if pending_type in {"STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}:
        if not payload.get("pendingPrice"):
            reasons.append("pendingPrice_required")
        if not payload.get("pendingTimeInForce"):
            reasons.append("pendingTimeInForce_required")
        if not (payload.get("pendingStopPrice") or payload.get("pendingTrailingDelta")):
            reasons.append("pending_stop_or_trailing_required")
    if payload.get("pendingTrailingDelta") and not payload.get("pendingPrice"):
        reasons.append("pendingPrice_required_with_trailing")
    return reasons


def validate_otoco_payload(payload: dict[str, object]) -> list[str]:
    """OTOCO conditional requirements. The track does not submit these."""
    reasons: list[str] = []
    working_type = _require(payload, "workingType", reasons)
    above = _require(payload, "pendingAboveType", reasons)
    below = _require(payload, "pendingBelowType", reasons)
    _require(payload, "symbol", reasons)
    _require(payload, "workingSide", reasons)
    _require(payload, "workingPrice", reasons)
    _require(payload, "workingQuantity", reasons)
    _require(payload, "pendingSide", reasons)
    _require(payload, "pendingQuantity", reasons)
    if payload.get("isIsolated") not in (None, CROSS_ISOLATED_FLAG):
        reasons.append("otoco_isolated_rejected")
    effect = payload.get("sideEffectType", NO_SIDE_EFFECT)
    if not isinstance(effect, str):
        reasons.append("side_effect_invalid")
    elif effect == "AUTO_REPAY":
        reasons.append("auto_repay_not_in_otoco_enum")
    else:
        problem = _reject_side_effect(effect, OTO_SIDE_EFFECTS)
        if problem:
            reasons.append(problem)
    if working_type and working_type not in OTO_WORKING_TYPES:
        reasons.append("working_type_invalid")
    if working_type == "LIMIT" and not payload.get("workingTimeInForce"):
        reasons.append("workingTimeInForce_required")
    if above and above not in OTOCO_PENDING_TYPES:
        reasons.append("pending_above_type_invalid")
    if below and below not in OTOCO_PENDING_TYPES:
        reasons.append("pending_below_type_invalid")
    _otoco_leg(payload, "pendingAbove", above, reasons)
    _otoco_leg(payload, "pendingBelow", below, reasons)
    return reasons


def _otoco_leg(
    payload: dict[str, object], prefix: str, leg_type: str, reasons: list[str]
) -> None:
    if leg_type == "LIMIT_MAKER" and not payload.get(f"{prefix}Price"):
        reasons.append(f"{prefix}Price_required")
    if leg_type == "STOP_LOSS" and not (
        payload.get(f"{prefix}StopPrice") or payload.get(f"{prefix}TrailingDelta")
    ):
        reasons.append(f"{prefix}_stop_or_trailing_required")
    if leg_type == "STOP_LOSS_LIMIT":
        if not payload.get(f"{prefix}Price"):
            reasons.append(f"{prefix}Price_required")
        if not payload.get(f"{prefix}TimeInForce"):
            reasons.append(f"{prefix}TimeInForce_required")
        if not (
            payload.get(f"{prefix}StopPrice") or payload.get(f"{prefix}TrailingDelta")
        ):
            reasons.append(f"{prefix}_stop_or_trailing_required")
    if payload.get(f"{prefix}TrailingDelta") and not payload.get(f"{prefix}Price"):
        reasons.append(f"{prefix}Price_required_with_trailing")
