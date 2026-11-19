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
    """Build the execution request from fields the signal already carries."""
    kept = preserve_explicit_account_domain(dict(signal))
    domain = kept.get("account_domain") or "binance_spot"
    return {
        "account_domain": domain,
        "spot_book_full": kept.get("spot_book_full"),
        "strategy_id": kept.get("strategy_id"),
        "execution_mode": kept.get("execution_mode"),
        "submit_spot": kept.get("submit_spot") is True,
        "submit_margin": kept.get("submit_margin") is True,
        "intent": kept.get("intent"),
        "snapshot": kept.get("snapshot"),
        "contract": kept.get("contract"),
        "gate": kept.get("gate"),
        "client": kept.get("client"),
    }
