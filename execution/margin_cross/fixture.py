"""Offline numbers for tests and the dry-run command.

Every limit here is a fixture. ``parameters_approved`` is false and the
strategy id is not an approved playbook. Nothing in this module is loaded by
the spot runner.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from execution.margin_cross.offline_client import OfflineFixtureClient
from execution.margin_cross.risk import (
    OFFLINE_FIXTURE_STRATEGY_ID,
    PURPOSE_CROSS_MARGIN_TRACK,
    REQUIRED_UNAPPROVED_LABEL,
)

NOW_MS = 1_763_520_000_000


def short_case() -> dict[str, Any]:
    """Borrow, sell, and cover an already-accrued 0.0001 BTC of interest."""
    return _case(action="OPEN_SHORT", intent_id="short-cycle-1", interest_on_borrow="0.0001")


def long_case() -> dict[str, Any]:
    raw = _case(action="OPEN_LONG", intent_id="long-cycle-1", interest_on_borrow="0")
    intent = raw["intent"]
    intent["take_profit_price"] = "100500"
    intent["stop_price"] = "99500"
    intent["stop_limit_price"] = "99400"
    return raw


def _case(*, action: str, intent_id: str, interest_on_borrow: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "uid": "fixture-uid-not-real",
        "margin_mode": "CROSS",
        "account_type": "MARGIN_1",
        "created": True,
        "trade_enabled": True,
        "borrow_enabled": True,
        "margin_level": "10",
        "account_max_leverage": "3",
        "observed_at_ms": NOW_MS - 1000,
        "assets": [
            {"asset": "BTC", "free": "0", "locked": "0", "borrowed": "0", "interest": "0"},
            {"asset": "USDT", "free": "5000", "locked": "0", "borrowed": "0", "interest": "0"},
        ],
        "max_borrowable": {"BTC": "1", "USDT": "10000"},
        "borrow_limit": {"BTC": "2", "USDT": "20000"},
        "hourly_interest_rate": {"BTC": "0.000001", "USDT": "0.000002"},
        "bid_qty": "1",
        "ask_qty": "1",
        "reference_price": "100000",
        "filters": {
            "tick_size": "0.01",
            "step_size": "0.0001",
            "min_qty": "0.0001",
            "min_notional": "5",
        },
        "fee_rate": "0.001",
        "fee_asset": "USDT",
    }
    contract = {
        "account_uid": snapshot["uid"],
        "mode": "CROSS",
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "borrow_asset": "BTC",
        "parameters_approved": False,
        "unapproved_label": REQUIRED_UNAPPROVED_LABEL,
        "max_leverage": "3",
        "min_margin_level": "3",
        "max_borrow_quantity": "0.02",
        "max_snapshot_age_ms": 5000,
        "interest_horizon_hours": "0",
        "max_base_over_liability": "0",
        "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
        "borrow_step": "0.0001",
    }
    intent = {
        "intent_id": intent_id,
        "purpose": PURPOSE_CROSS_MARGIN_TRACK,
        "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
        "account_uid": snapshot["uid"],
        "mode": "CROSS",
        "action": action,
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "quantity": "0.01",
        "limit_price": "100000",
        "take_profit_price": "99500",
        "stop_price": "100400",
        "stop_limit_price": "100500",
    }
    gate = {
        "emergency_stop": False,
        "now_ms": NOW_MS,
        "execution_mode": "offline_fixture",
    }
    client = client_from_snapshot(snapshot, Decimal(interest_on_borrow))
    return {
        "intent": intent,
        "snapshot": snapshot,
        "contract": contract,
        "gate": gate,
        "client": client,
    }


def client_from_snapshot(
    snapshot: dict[str, Any], interest_on_borrow: Decimal
) -> OfflineFixtureClient:
    client = OfflineFixtureClient()
    client.account_uid = str(snapshot["uid"])
    client.margin_mode = str(snapshot["margin_mode"])
    client.account_type = str(snapshot["account_type"])
    client.trade_enabled = bool(snapshot["trade_enabled"])
    client.borrow_enabled = bool(snapshot["borrow_enabled"])
    client.created = bool(snapshot["created"])
    client.margin_level = Decimal(str(snapshot["margin_level"]))
    client.market_price = Decimal(str(snapshot["reference_price"]))
    client.bid_qty = Decimal(str(snapshot["bid_qty"]))
    client.ask_qty = Decimal(str(snapshot["ask_qty"]))
    client.fee_rate = Decimal(str(snapshot["fee_rate"]))
    client.fee_asset = str(snapshot["fee_asset"])
    client.interest_on_borrow = interest_on_borrow
    for row in snapshot["assets"]:
        target = client.asset(str(row["asset"]))
        for field in ("free", "locked", "borrowed", "interest"):
            target[field] = Decimal(str(row[field]))
    for asset, amount in snapshot["max_borrowable"].items():
        client.max_borrow[str(asset)] = Decimal(str(amount))
    for asset, amount in snapshot["borrow_limit"].items():
        client.borrow_limit[str(asset)] = Decimal(str(amount))
    for asset, rate in snapshot["hourly_interest_rate"].items():
        client.hourly_rate[str(asset)] = Decimal(str(rate))
    return client
