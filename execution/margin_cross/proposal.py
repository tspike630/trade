"""Ruled cross-margin threshold specification.

Runtime ``approved`` stays ``False`` unless ``CROSS_MARGIN_PARAMETERS_APPROVED=1``
is explicitly set by the operator after verification.
"""

from __future__ import annotations

from typing import Any

from execution.margin_cross.policy import (
    BORROW_REPAY_MIN_SPACING_SEC,
    DEFAULT_INTEREST_HORIZON_HOURS,
    DEFAULT_MAX_SNAPSHOT_AGE_MS,
    EMERGENCY_MARGIN_LEVEL,
    MAX_ACCOUNT_LEVERAGE,
    MAX_BORROWABLE_POOL_RATIO,
    MAX_EFFECTIVE_LEVERAGE,
    MAX_TOTAL_LIABILITY_NET_RATIO,
    MIN_ENTRY_MARGIN_LEVEL,
    MIN_ORDER_NOTIONAL_USDT,
    WARN_MARGIN_LEVEL,
    parameters_approved_by_env,
)


def unapproved_threshold_proposal() -> list[dict[str, Any]]:
    """Return ruled parameter contract; ``approved`` reflects runtime env unlock."""
    unlocked = parameters_approved_by_env()
    rows: list[dict[str, Any]] = [
        {
            "item": "max_leverage",
            "ruled_value": {
                "account_ceiling": str(MAX_ACCOUNT_LEVERAGE),
                "effective_portfolio_ceiling": str(MAX_EFFECTIVE_LEVERAGE),
            },
            "suggested_for_discussion": (
                "Classic Cross Margin (MARGIN_1 <= 3x) only; never call "
                "POST /sapi/v1/margin/max-leverage; cap post-borrow effective "
                "leverage total_asset / net_asset at 1.50x."
            ),
            "why_not_installed": "Locked until CROSS_MARGIN_PARAMETERS_APPROVED=1.",
        },
        {
            "item": "min_margin_level",
            "ruled_value": {
                "entry_floor": str(MIN_ENTRY_MARGIN_LEVEL),
                "warn_floor": str(WARN_MARGIN_LEVEL),
                "emergency_floor": str(EMERGENCY_MARGIN_LEVEL),
            },
            "suggested_for_discussion": (
                "Entry/borrow requires marginLevel >= 2.50 (status EXCESSIVE/NORMAL); "
                "< 2.00 blocks new borrows and cancels unfilled resting entries; "
                "<= 1.60 triggers emergency unwind while keeping working OCO intact."
            ),
            "why_not_installed": "Locked until CROSS_MARGIN_PARAMETERS_APPROVED=1.",
        },
        {
            "item": "max_borrow_quantity",
            "ruled_value": {
                "single_borrow_net_asset_ratio": "0.24",
                "total_liability_net_asset_ratio": str(MAX_TOTAL_LIABILITY_NET_RATIO),
                "max_borrowable_pool_utilization": str(MAX_BORROWABLE_POOL_RATIO),
                "min_order_notional_usdt": str(MIN_ORDER_NOTIONAL_USDT),
            },
            "suggested_for_discussion": (
                "Single borrow <= 24% of real-time Cross Margin net asset, total liability "
                "<= 50% of net asset, and <= 80% of GET /sapi/v1/margin/maxBorrowable."
            ),
            "why_not_installed": "Locked until CROSS_MARGIN_PARAMETERS_APPROVED=1.",
        },
        {
            "item": "max_snapshot_age_ms",
            "ruled_value": DEFAULT_MAX_SNAPSHOT_AGE_MS,
            "suggested_for_discussion": (
                "5000ms snapshot TTL plus 5-state feature clocks (indicators/OBI/perp/gamma/volume_z "
                "3600s, news 90m, LSEG 5m bars 90m, CFTC COT publication calendar)."
            ),
            "why_not_installed": "Locked until CROSS_MARGIN_PARAMETERS_APPROVED=1.",
        },
        {
            "item": "interest_horizon_hours",
            "ruled_value": str(DEFAULT_INTEREST_HORIZON_HOURS),
            "suggested_for_discussion": (
                "4h interest reserve within 1 step_size over-buy bound; repay covers interest "
                "before principal and verifies borrowed + interest == 0 via GET /sapi/v1/margin/account."
            ),
            "why_not_installed": "Locked until CROSS_MARGIN_PARAMETERS_APPROVED=1.",
        },
        {
            "item": "exception_and_idempotency_policy",
            "ruled_value": {
                "borrow_repay_min_spacing_sec": str(BORROW_REPAY_MIN_SPACING_SEC),
                "timeout_retry": "forbidden_readback_only",
                "residual_shortfall_policy": "repay_from_existing_free_only_else_halt_manual",
            },
            "suggested_for_discussion": (
                "Never blind-retry timed-out borrow/order/OCO/repay; bind via txId/startTime + "
                "account liability delta; -3007 enters pending lock; -3015 / base shortfall halts "
                "without sending a second market buy."
            ),
            "why_not_installed": "Locked until CROSS_MARGIN_PARAMETERS_APPROVED=1.",
        },
        {
            "item": "emergency_stop",
            "ruled_value": "LIVE_TRADING_DISABLED",
            "suggested_for_discussion": (
                "Keep LIVE_TRADING_DISABLED active; blocks new borrows and entries while allowing "
                "repayment and already-working native OCO protection."
            ),
            "why_not_installed": "Production halt stays active until post-audit sign-off.",
        },
    ]
    for row in rows:
        row["approved"] = unlocked
    return rows
