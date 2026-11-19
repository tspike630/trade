"""Copy an explicit account domain from an admitted signal.

Missing or unknown domains stay off the signal. This module does not choose
cross-margin, does not read approval files, and does not submit an order.
"""

from __future__ import annotations

from typing import Any

EXPLICIT_ACCOUNT_DOMAINS = frozenset({"binance_spot", "binance_cross_margin"})


def preserve_explicit_account_domain(signal: dict[str, Any]) -> dict[str, Any]:
    """Keep only a caller-supplied spot or cross-margin domain."""
    domain = signal.get("account_domain")
    if domain not in EXPLICIT_ACCOUNT_DOMAINS:
        signal.pop("account_domain", None)
    if "spot_book_full" in signal and not isinstance(signal.get("spot_book_full"), bool):
        signal.pop("spot_book_full", None)
    return signal


def route_request_from_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Build the execution request from fields the signal already carries.

    A missing domain stays on spot. A present domain that is not spot or
    cross-margin is kept so the dispatcher can refuse it. It is not rewritten
    to spot.
    """
    raw_domain = signal.get("account_domain", None)
    spot_book_full = signal.get("spot_book_full", None)
    if not isinstance(spot_book_full, bool):
        spot_book_full = None
    domain = "binance_spot" if raw_domain in (None, "") else raw_domain
    return {
        "account_domain": domain,
        "spot_book_full": spot_book_full,
        "strategy_id": signal.get("strategy_id"),
        "execution_mode": signal.get("execution_mode"),
        "submit_spot": signal.get("submit_spot") is True,
        "submit_margin": signal.get("submit_margin") is True,
        "intent": signal.get("intent"),
        "snapshot": signal.get("snapshot"),
        "contract": signal.get("contract"),
        "gate": signal.get("gate"),
        "client": signal.get("client"),
        "offline_approval": signal.get("offline_approval"),
        "interest_on_borrow": signal.get("interest_on_borrow"),
    }
