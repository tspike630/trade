"""Route one admitted signal to spot or to the existing cross-margin entry.

Unconfigured signals stay on spot. Cross-margin runs only when the signal
names that domain. This module does not set production switches and does not
pick the 8-strategy book or the 3-short book unless ``spot_book_full`` is
an explicit boolean on the signal.
"""

from __future__ import annotations

from typing import Any, Callable

from execution.margin_cross.entry import open_offline_routed
from execution.margin_cross.policy import (
    SUPPORTED_REGIME_STRATEGIES,
    SUPPORTED_SHORT_STRATEGIES,
)
from execution.margin_cross.risk import OFFLINE_FIXTURE_STRATEGY_ID
from execution.margin_cross.strategy_gate import build_approved_position_plan

SpotSubmit = Callable[[dict[str, Any]], dict[str, Any]]

# Names the offline fixture must supply. Values stay on the fixture.
_OFFLINE_APPROVAL_KEYS = (
    "approval_scope",
    "max_leverage",
    "effective_leverage",
    "total_liability_net",
    "single_borrow_net",
    "borrowable_pool",
    "entry_margin_level",
    "warn_margin_level",
    "emergency_margin_level",
    "base_position_pct",
    "min_notional",
    "snapshot_max_age_ms",
    "interest_horizon_hours",
    "enable_state2_long",
    "enable_state4_long",
    "fill_tp_formula_b",
)


def _offline_approval_ok(signal: dict[str, Any]) -> bool:
    approval = signal.get("offline_approval")
    if not isinstance(approval, dict) or approval.get("parameters_approved") is not True:
        return False
    return all(approval.get(key) not in (None, "") for key in _OFFLINE_APPROVAL_KEYS)


def _refuse(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "venue": None,
        "spot_called": False,
        "margin_called": False,
        "entry": None,
    }


def _topology_block(strategy_id: str, spot_book_full: bool, execution_mode: str) -> str | None:
    if spot_book_full:
        if strategy_id not in SUPPORTED_SHORT_STRATEGIES:
            return "spot_full_only_three_shorts"
        return None
    if execution_mode == "offline_fixture" and strategy_id == OFFLINE_FIXTURE_STRATEGY_ID:
        return None
    if strategy_id not in SUPPORTED_REGIME_STRATEGIES:
        return "strategy_not_in_cross_margin_set"
    return None


def dispatch_account_route(
    signal: dict[str, Any],
    *,
    ledger_path: str,
    spot_submit: SpotSubmit | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send the signal to one venue. Default and spot never call margin."""
    domain = signal.get("account_domain")
    if domain in (None, "", "binance_spot"):
        if signal.get("submit_margin") is True:
            return _refuse("duplicate_spot_and_margin")
        if spot_submit is None:
            return _refuse("spot_submit_missing")
        spot_result = spot_submit(signal)
        return {
            "ok": True,
            "reason": None,
            "venue": "binance_spot",
            "spot_called": True,
            "margin_called": False,
            "entry": "spot_submit",
            "spot_result": spot_result,
        }
    if domain != "binance_cross_margin":
        return _refuse("account_domain_invalid")
    if signal.get("submit_spot") is True:
        return _refuse("duplicate_spot_and_margin")
    if not isinstance(signal.get("spot_book_full"), bool):
        return _refuse("topology_unspecified")

    strategy_id = str(signal.get("strategy_id") or "")
    execution_mode = str(signal.get("execution_mode") or "")
    blocked = _topology_block(strategy_id, bool(signal.get("spot_book_full")), execution_mode)
    if blocked is not None:
        return _refuse(blocked)

    intent = signal.get("intent") if isinstance(signal.get("intent"), dict) else None
    snapshot = signal.get("snapshot") if isinstance(signal.get("snapshot"), dict) else None
    if intent is not None and snapshot is not None:
        intent_uid = str(intent.get("account_uid") or "")
        snap_uid = str(snapshot.get("uid") or "")
        if intent_uid and snap_uid and intent_uid != snap_uid:
            return _refuse("account_uid_mismatch")
        intent_strategy = str(intent.get("strategy_id") or "")
        if intent_strategy and intent_strategy != strategy_id:
            return _refuse("strategy_payload_mismatch")

    if execution_mode == "offline_fixture":
        if not _offline_approval_ok(signal):
            return _refuse("parameters_not_approved")
        opened = open_offline_routed(ledger_path, signal)
        return {
            "ok": bool(opened.get("ok")),
            "reason": opened.get("reason"),
            "venue": "binance_cross_margin",
            "spot_called": False,
            "margin_called": bool(opened.get("margin_called")),
            "entry": opened.get("entry"),
            "open": opened.get("open"),
            "writes": opened.get("writes"),
        }

    plan = build_approved_position_plan(
        intent_id=str(signal.get("intent_id") or "route-unapproved"),
        symbol=str(signal.get("symbol") or "BTCUSDT"),
        base_asset=str(signal.get("base_asset") or "BTC"),
        quote_asset=str(signal.get("quote_asset") or "USDT"),
        requested_action=str(signal.get("requested_action") or "OPEN_SHORT"),
        snapshot_raw=snapshot or {"uid": ""},
        market_row={},
        clocks={},
        now_ms=int(signal.get("now_ms") or 0),
        planned_take_profit=signal.get("planned_take_profit") or 0,
        stop_price=signal.get("stop_price") or 0,
        stop_limit_price=signal.get("stop_limit_price") or 0,
        risk_config=signal.get("approved_risk_config"),
        environ=environ,
    )
    return {
        "ok": False,
        "reason": plan.reasons[0] if plan.reasons else "margin_not_admitted",
        "venue": "binance_cross_margin",
        "spot_called": False,
        "margin_called": False,
        "entry": "execution.margin_cross.strategy_gate.build_approved_position_plan",
        "reasons": list(plan.reasons),
    }
