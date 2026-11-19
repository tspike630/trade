"""Unapproved threshold suggestions. The track does not import this module."""

from __future__ import annotations

from typing import Any


def unapproved_threshold_proposal() -> list[dict[str, Any]]:
    """Discussion numbers only. ``approved`` is false on every row."""
    rows = [
        {
            "item": "max_leverage",
            "suggested_for_discussion": "Do not call POST /sapi/v1/margin/max-leverage. SDK notes classic 3x initial risk ratio 1.5 and 5x ratio 1.25.",
            "why_not_installed": "杨 has not set a leverage ceiling. The five-state 24% spot weight is not a borrow multiple.",
        },
        {
            "item": "min_margin_level",
            "suggested_for_discussion": "A floor well above the SDK's 1.5 initial ratio for 3x, for example 3, once 杨 picks the leverage.",
            "why_not_installed": "No approved distance from liquidation. The contract field has no default.",
        },
        {
            "item": "max_borrow_quantity",
            "suggested_for_discussion": "Per-asset GET /sapi/v1/margin/maxBorrowable amount, then a separate notional cap 杨 sets. USDT borrowable does not apply to other assets.",
            "why_not_installed": "No approved borrow cap.",
        },
        {
            "item": "max_snapshot_age_ms",
            "suggested_for_discussion": "5000",
            "why_not_installed": "Freshness window is part of the unapproved contract.",
        },
        {
            "item": "interest_horizon_hours",
            "suggested_for_discussion": "24",
            "why_not_installed": "The dry-run fixture uses 0 so accrued interest can repay to exact zero. That is not a policy.",
        },
        {
            "item": "max_base_over_liability",
            "suggested_for_discussion": "One lot step, only if 杨 accepts the residual base.",
            "why_not_installed": "Over-buy would leave a long after a short repay.",
        },
        {
            "item": "emergency_stop",
            "suggested_for_discussion": "Keep the production halt. This track blocks new borrow and entry when its own gate is true, and still allows repay and an already-working OCO.",
            "why_not_installed": "This package does not read or delete the production halt file.",
        },
    ]
    for row in rows:
        row["approved"] = False
    return rows
