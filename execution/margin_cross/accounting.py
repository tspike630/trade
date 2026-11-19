"""Liability, cover quantity, and flatness. Dust debt stays debt."""

from __future__ import annotations

from decimal import Decimal

from execution.margin_cross.money import ceil_to_step
from execution.margin_cross.risk import AssetRow


def liability(row: AssetRow | None) -> Decimal:
    if row is None:
        return Decimal(0)
    return row.borrowed + row.interest


def any_liability(assets: dict[str, AssetRow]) -> bool:
    return any(liability(row) > 0 for row in assets.values())


def cover_buy_quantity(
    *,
    borrowed: Decimal,
    interest: Decimal,
    hourly_rate: Decimal,
    horizon_hours: Decimal,
    step: Decimal,
    fee_rate: Decimal,
    fee_on_base: bool,
    max_base_over_liability: Decimal,
) -> tuple[Decimal | None, str]:
    current = borrowed + interest
    if current <= 0:
        return None, "no_liability_to_cover"
    if hourly_rate < 0 or horizon_hours < 0 or fee_rate < 0 or fee_rate >= 1:
        return None, "cover_inputs_invalid"
    reserve = borrowed * hourly_rate * horizon_hours
    target = current + reserve
    raw = target / (Decimal(1) - fee_rate) if fee_on_base else target
    quantity = ceil_to_step(raw, step)
    if quantity is None or quantity <= 0:
        return None, "cover_qty_invalid"
    received = quantity * (Decimal(1) - fee_rate) if fee_on_base else quantity
    residual = received - current
    if residual < 0:
        return None, "cover_does_not_pay_liability"
    if residual > max_base_over_liability:
        return None, "cover_exceeds_max_base_over_liability"
    return quantity, "ok"


def quote_needed(quantity: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    return quantity * price * (Decimal(1) + fee_rate)
