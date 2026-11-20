"""Unapproved candidate cross-margin threshold proposals for operator review.

None of the candidate values in this file are active or pre-approved. Runtime
execution never imports this module and requires an explicit operator-supplied
zero-default ``CROSS_MARGIN_APPROVED_CONFIG_JSON`` file plus
``CROSS_MARGIN_PARAMETERS_APPROVED=1``.
"""

from __future__ import annotations

from typing import Any

from execution.margin_cross.policy import (
    APPROVED_CONFIG_PATH_ENV,
    BORROW_REPAY_MIN_SPACING_SEC,
    HARD_CLASSIC_CROSS_LEVERAGE_CEILING,
    PARAMETERS_APPROVED_ENV,
)


def unapproved_threshold_proposal() -> list[dict[str, Any]]:
    """Return candidate discussion proposals; ``approved`` is unconditionally ``False``."""
    why_locked = (
        f"Unapproved proposal only; nothing is installed by default. Operator must "
        f"explicitly supply a zero-default JSON file via {APPROVED_CONFIG_PATH_ENV} "
        f"and set {PARAMETERS_APPROVED_ENV}=1."
    )
    return [
        {
            "item": "max_leverage",
            "approved": False,
            "candidate_for_operator_review": {
                "account_ceiling": str(HARD_CLASSIC_CROSS_LEVERAGE_CEILING),
                "effective_portfolio_ceiling": "1.50",
            },
            "suggested_for_discussion": (
                "Classic Cross Margin (MARGIN_1 <= 3x) only; never call "
                "POST /sapi/v1/margin/max-leverage; candidate post-borrow effective "
                "leverage ceiling 1.50x."
            ),
            "why_not_installed": why_locked,
        },
        {
            "item": "min_margin_level",
            "approved": False,
            "candidate_for_operator_review": {
                "entry_floor": "2.50",
                "warn_floor": "2.00",
                "emergency_floor": "1.60",
            },
            "suggested_for_discussion": (
                "Candidate margin-level ladder: entry >= 2.50, warn < 2.00 "
                "(cancel unfilled resting entry), emergency <= 1.60."
            ),
            "why_not_installed": why_locked,
        },
        {
            "item": "max_borrow_quantity",
            "approved": False,
            "candidate_for_operator_review": {
                "base_position_pct": "0.24",
                "single_borrow_net_asset_ratio": "0.24",
                "total_liability_net_asset_ratio": "0.50",
                "max_borrowable_pool_utilization": "0.80",
                "min_order_notional_usdt": "20",
            },
            "suggested_for_discussion": (
                "Candidate sizing/borrow caps for discussion only; not active until "
                "explicitly written into operator-approved config."
            ),
            "why_not_installed": why_locked,
        },
        {
            "item": "max_snapshot_age_ms",
            "approved": False,
            "candidate_for_operator_review": 5000,
            "suggested_for_discussion": (
                "Candidate 5000ms account snapshot TTL plus 5-state feature clocks."
            ),
            "why_not_installed": why_locked,
        },
        {
            "item": "interest_horizon_hours",
            "approved": False,
            "candidate_for_operator_review": "4",
            "suggested_for_discussion": (
                "Candidate 4h interest reserve within 1 step_size over-buy bound."
            ),
            "why_not_installed": why_locked,
        },
        {
            "item": "exception_and_idempotency_policy",
            "approved": False,
            "candidate_for_operator_review": {
                "borrow_repay_min_spacing_sec": str(BORROW_REPAY_MIN_SPACING_SEC),
                "timeout_retry": "forbidden_readback_only",
                "same_asset_external_liability": "halt_no_shared_repay",
            },
            "suggested_for_discussion": (
                "Never blind-retry timed-out writes; isolate per-intent principal and "
                "refuse shared-bucket auto-repay if external same-asset borrow is present."
            ),
            "why_not_installed": why_locked,
        },
        {
            "item": "emergency_stop",
            "approved": False,
            "candidate_for_operator_review": "LIVE_TRADING_DISABLED",
            "suggested_for_discussion": (
                "Keep LIVE_TRADING_DISABLED active; blocks new borrows and entries while "
                "allowing repayment and already-working native OCO protection."
            ),
            "why_not_installed": "Production halt stays active until post-audit sign-off.",
        },
    ]
