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
        "intent_id": signal.get("intent_id"),
        "decision_log_id": signal.get("decision_log_id"),
        "symbol": signal.get("symbol"),
        "base_asset": signal.get("base_asset"),
        "quote_asset": signal.get("quote_asset"),
        "requested_action": signal.get("requested_action"),
        "entry_tier": signal.get("entry_tier"),
        "limit_price": signal.get("limit_price"),
        "take_profit_price": signal.get("take_profit_price"),
        "stop_price": signal.get("stop_price"),
        "stop_limit_price": signal.get("stop_limit_price"),
        "closed_bar_close": signal.get("closed_bar_close"),
        "cross_margin_price_contract": signal.get("cross_margin_price_contract"),
        "market_row": signal.get("market_row"),
        "clocks": signal.get("clocks"),
        "now_ms": signal.get("now_ms"),
        "expected_uid": signal.get("expected_uid"),
        "fee_rate": signal.get("fee_rate"),
        "fee_asset": signal.get("fee_asset"),
    }
