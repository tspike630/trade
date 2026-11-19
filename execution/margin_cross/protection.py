"""Native margin protection plans.

Short protection is a BUY OCO: the limit leg is below the reference and the
stop leg is above it. Long protection is the opposite SELL OCO. A working plan
is not a cancel/replace instruction.

OTOCO is intentionally not selected. Official pending quantity is not documented
as independent of the working quantity, so a cover size that includes interest
cannot be assumed to fit an OTOCO list.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from execution.margin_cross.accounting import quote_needed
from execution.margin_cross.api_semantics import build_oco_payload, make_client_id
from execution.margin_cross.money import on_grid
from execution.margin_cross.risk import Intent


@dataclass(frozen=True)
class ProtectionPlan:
    kind: str
    side: str
    quantity: Decimal
    payload: dict[str, str]
    list_client_order_id: str
    limit_client_order_id: str
    stop_client_order_id: str


def plan_oco(
    intent: Intent,
    *,
    quantity: Decimal,
    reference_price: Decimal,
    tick: Decimal,
    step: Decimal,
    min_notional: Decimal,
    fee_rate: Decimal,
    free_quote: Decimal,
    ask_qty: Decimal,
    bid_qty: Decimal,
) -> tuple[ProtectionPlan | None, str]:
    if quantity <= 0 or not on_grid(quantity, step):
        return None, "protection_qty_off_step"
    side = "BUY" if intent.action == "OPEN_SHORT" else "SELL"
    if side == "BUY":
        if not (
            intent.take_profit_price < reference_price < intent.stop_price
            and intent.stop_limit_price >= intent.stop_price
        ):
            return None, "short_protection_price_geometry"
        if quantity > ask_qty:
            return None, "buy_liquidity_insufficient"
        worst = quote_needed(quantity, intent.stop_limit_price, fee_rate)
        if free_quote < worst:
            return None, "quote_cannot_fund_stop_buy"
    else:
        if not (
            intent.take_profit_price > reference_price > intent.stop_price
            and intent.stop_limit_price <= intent.stop_price
        ):
            return None, "long_protection_price_geometry"
        if quantity > bid_qty:
            return None, "sell_liquidity_insufficient"
    if quantity * min(intent.take_profit_price, intent.stop_limit_price) < min_notional:
        return None, "protection_notional_below_min"
    if not on_grid(intent.take_profit_price, tick) or not on_grid(intent.stop_price, tick):
        return None, "protection_price_off_tick"
    if not on_grid(intent.stop_limit_price, tick):
        return None, "protection_price_off_tick"
    list_id = make_client_id(intent.intent_id, "oco-list")
    limit_id = make_client_id(intent.intent_id, "oco-limit")
    stop_id = make_client_id(intent.intent_id, "oco-stop")
    payload = build_oco_payload(
        symbol=intent.symbol,
        side=side,
        quantity=quantity,
        limit_price=intent.take_profit_price,
        stop_price=intent.stop_price,
        stop_limit_price=intent.stop_limit_price,
        list_client_order_id=list_id,
        limit_client_order_id=limit_id,
        stop_client_order_id=stop_id,
    )
    return (
        ProtectionPlan("MARGIN_OCO", side, quantity, payload, list_id, limit_id, stop_id),
        "ok",
    )


def otoco_submission_block() -> str:
    return "otoco_not_submitted_qty_coupling_unverified"
