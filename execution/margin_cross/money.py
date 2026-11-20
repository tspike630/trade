"""Decimal parsing. Floats are rejected so binary dust cannot enter a payload."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING


def parse_decimal(value: object, field: str) -> tuple[Decimal | None, str | None]:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        return None, f"{field}_invalid"
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped == "" or stripped.lower() in {"nan", "inf", "+inf", "-inf", "infinity"}:
            return None, f"{field}_not_finite"
        try:
            number = Decimal(stripped)
        except InvalidOperation:
            return None, f"{field}_invalid"
    else:
        return None, f"{field}_invalid"
    if not number.is_finite():
        return None, f"{field}_not_finite"
    return number, None


def api_amount(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def on_grid(value: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return False
    units = value / step
    return units == units.to_integral_value()


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal | None:
    if step <= 0 or not value.is_finite():
        return None
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step
