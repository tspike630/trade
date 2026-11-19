"""Cross-margin state machine.

Unknown writes are queried and then left alone. A second borrow, order, OCO,
or repay is not sent for the same ledger key. Working native protection is
not cancelled. Live HTTP is not available; ``performs_network_io`` must be
false or construction fails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from execution.margin_cross.accounting import any_liability, cover_buy_quantity, liability
from execution.margin_cross.api_semantics import (
    build_borrow_payload,
    build_borrow_query_payload,
    build_cancel_payload,
    build_hourly_interest_payload,
    build_limit_payload,
    build_max_borrow_payload,
    build_query_oco_payload,
    build_query_order_payload,
    build_repay_payload,
    make_client_id,
    validate_otoco_payload,
)
from execution.margin_cross.errors import (
    LiveNetworkDisabled,
    MarginExchangeError,
    MarginTransportTimeout,
)
from execution.margin_cross.ledger import Ledger
from execution.margin_cross.money import api_amount, parse_decimal
from execution.margin_cross.protection import otoco_submission_block, plan_oco
from execution.margin_cross.risk import (
    OFFLINE_EXECUTION_MODE,
    AssetRow,
    Contract,
    Gate,
    Intent,
    Snapshot,
    contract_identity_reasons,
    parse_contract,
    parse_gate,
    parse_intent,
    parse_snapshot,
)


@dataclass
class TrackResult:
    intent_id: str
    state: str
    phase_ok: bool
    account_flat: bool | None
    protection_confirmed: bool
    live_writes_blocked: bool
    reason: str
    borrowed: str
    interest: str
    base_free: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "state": self.state,
            "phase_ok": self.phase_ok,
            "account_flat": self.account_flat,
            "protection_confirmed": self.protection_confirmed,
            "live_writes_blocked": self.live_writes_blocked,
            "reason": self.reason,
            "borrowed": self.borrowed,
            "interest": self.interest,
            "base_free": self.base_free,
            "detail": self.detail,
        }


class CrossMarginTrack:
    def __init__(self, ledger_path: str, client: Any) -> None:
        if getattr(client, "performs_network_io", True) is not False:
            raise LiveNetworkDisabled(
                "cross-margin live transport is disabled in this build"
            )
        self.client = client
        self.ledger = Ledger(ledger_path)

    def submit_otoco(self, payload: Mapping[str, Any]) -> TrackResult:
        """OTOCO is parsed and then refused. No client call."""
        reasons = validate_otoco_payload(dict(payload))
        return self._result(
            intent_id="",
            state="otoco_not_submitted",
            reason=otoco_submission_block(),
            detail={"validation": reasons},
        )

    def open_position(
        self,
        intent_raw: Mapping[str, Any],
        snapshot_raw: Mapping[str, Any],
        contract_raw: Mapping[str, Any],
        gate_raw: Mapping[str, Any],
    ) -> TrackResult:
        parsed = self._parse(intent_raw, snapshot_raw, contract_raw, gate_raw)
        if isinstance(parsed, TrackResult):
            return parsed
        intent, snapshot, contract, gate = parsed
        if gate.execution_mode != OFFLINE_EXECUTION_MODE:
            return self._result(intent.intent_id, "live_network_disabled", "live_network_disabled")
        row = self.ledger.intent_row(intent.intent_id)
        if row is None:
            reasons = contract_identity_reasons(intent, contract, snapshot, gate)
            if reasons:
                return self._result(
                    intent.intent_id,
                    "locked",
                    reasons[0],
                    detail={"reasons": reasons},
                )
            if gate.emergency_stop:
                return self._result(
                    intent.intent_id,
                    "emergency_stop_blocks_new_risk",
                    "emergency_stop_blocks_new_risk",
                )
            mismatch = self._match_fresh_account(snapshot, contract)
            if mismatch:
                return self._result(intent.intent_id, "locked", mismatch)
            self.ledger.insert_intent(
                intent_id=intent.intent_id,
                purpose=intent.purpose,
                canonical=intent.canonical(),
                state="accepted",
                payload={
                    "intent": dict(intent_raw),
                    "snapshot": dict(snapshot_raw),
                    "contract": dict(contract_raw),
                },
                now_ms=gate.now_ms,
                baseline_base=api_amount(snapshot.assets[intent.base_asset].free),
            )
        else:
            if str(row["canonical"]) != intent.canonical():
                return self._result(intent.intent_id, "intent_conflict", "intent_identity_conflict")
            stored = json.loads(str(row["payload_json"]))
            restored = self._parse(stored["intent"], stored["snapshot"], stored["contract"], gate_raw)
            if isinstance(restored, TrackResult):
                return restored
            intent, snapshot, contract, gate = restored
        return self._continue(intent, snapshot, contract, gate)

    def complete_exit(self, intent_id: str, gate_raw: Mapping[str, Any]) -> TrackResult:
        gate, gate_reasons = parse_gate(gate_raw)
        if gate is None or gate_reasons or gate.execution_mode != OFFLINE_EXECUTION_MODE:
            return self._result(intent_id, "live_network_disabled", "live_network_disabled")
        row = self.ledger.intent_row(intent_id)
        if row is None:
            return self._result(intent_id, "not_ready", "intent_missing")
        stored = json.loads(str(row["payload_json"]))
        parsed = self._parse(stored["intent"], stored["snapshot"], stored["contract"], gate_raw)
        if isinstance(parsed, TrackResult):
            return parsed
        intent, snapshot, contract, gate = parsed
        list_id = make_client_id(intent.intent_id, "oco-list")
        if self.ledger.peek(intent.intent_id, "oco", list_id) is None:
            return self._result(intent.intent_id, "not_ready", "protection_not_placed", snapshot=snapshot)
        try:
            listed = self.client.query_oco(build_query_oco_payload(intent.symbol, list_id))
        except MarginExchangeError:
            return self._result(intent.intent_id, "protection_unknown", "oco_query_failed", snapshot=snapshot)
        if listed.get("listOrderStatus") == "EXECUTING":
            return self._result(
                intent.intent_id,
                "protection_still_working",
                "protection_left_in_place",
                protection_confirmed=True,
                snapshot=snapshot,
            )
        if listed.get("listOrderStatus") != "ALL_DONE":
            return self._result(intent.intent_id, "exit_not_filled", "exit_not_filled", snapshot=snapshot)
        filled = self._filled_protection_leg(intent, listed)
        if isinstance(filled, TrackResult):
            return filled
        return self._repay_after_close(intent, snapshot, contract, gate)

    def _continue(
        self, intent: Intent, snapshot: Snapshot, contract: Contract, gate: Gate
    ) -> TrackResult:
        if intent.action == "OPEN_SHORT":
            borrowed = self._ensure_borrow(intent, snapshot, contract, gate)
            if isinstance(borrowed, TrackResult):
                return borrowed
        entry = self._ensure_entry(intent, snapshot, contract, gate)
        if isinstance(entry, TrackResult):
            return entry
        return self._ensure_protection(intent, snapshot, contract, gate, entry)

    def _ensure_borrow(
        self, intent: Intent, snapshot: Snapshot, contract: Contract, gate: Gate
    ) -> TrackResult | None:
        client_key = make_client_id(intent.intent_id, "borrow")
        existing = self.ledger.peek(intent.intent_id, "borrow", client_key)
        payload = build_borrow_payload(contract.borrow_asset, intent.quantity)
        if existing is None:
            if gate.emergency_stop:
                return self._result(
                    intent.intent_id,
                    "emergency_stop_blocks_new_risk",
                    "emergency_stop_blocks_new_risk",
                    snapshot=snapshot,
                )
            blocked = self._new_risk_block(contract)
            if blocked:
                return self._result(intent.intent_id, "locked", blocked, snapshot=snapshot)
            flag = self.ledger.begin_operation(intent.intent_id, "borrow", client_key, payload)
            if flag != "inserted":
                return self._result(intent.intent_id, "borrow_unknown", "borrow_not_inserted")
            try:
                response = self.client.borrow(payload)
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, "borrow", client_key, "unknown", None)
                if not self._bind_transfer("BORROW", contract.borrow_asset, intent.quantity):
                    return self._result(
                        intent.intent_id, "borrow_unknown", "borrow_unconfirmed", snapshot=snapshot
                    )
                self.ledger.mark(intent.intent_id, "borrow", client_key, "confirmed", {"bound": True})
            except MarginExchangeError as exc:
                self.ledger.mark(
                    intent.intent_id,
                    "borrow",
                    client_key,
                    "rejected",
                    {"code": exc.code, "message": exc.message},
                )
                return self._result(
                    intent.intent_id,
                    "borrow_rejected",
                    f"borrow_rejected_{exc.code}",
                    snapshot=snapshot,
                )
            else:
                self.ledger.mark(intent.intent_id, "borrow", client_key, "confirmed", response)
        else:
            state = str(existing["state"])
            if state == "rejected":
                return self._result(intent.intent_id, "borrow_rejected", "borrow_rejected", snapshot=snapshot)
            if state != "confirmed" and not self._bind_transfer(
                "BORROW", contract.borrow_asset, intent.quantity
            ):
                return self._result(
                    intent.intent_id, "borrow_unknown", "borrow_unconfirmed", snapshot=snapshot
                )
            if state != "confirmed":
                self.ledger.mark(intent.intent_id, "borrow", client_key, "confirmed", {"bound": True})
        if getattr(self.client, "interrupt_after", None) == "borrow":
            return self._result(
                intent.intent_id,
                "interrupted_after_borrow",
                "interrupted_after_borrow",
                snapshot=snapshot,
            )
        return None

    def _ensure_entry(
        self, intent: Intent, snapshot: Snapshot, contract: Contract, gate: Gate
    ) -> TrackResult | Decimal:
        client_key = make_client_id(intent.intent_id, "entry")
        existing = self.ledger.peek(intent.intent_id, "entry", client_key)
        side = "SELL" if intent.action == "OPEN_SHORT" else "BUY"
        if existing is None:
            if gate.emergency_stop:
                if intent.action == "OPEN_SHORT":
                    return self._unwind(
                        intent, snapshot, contract, gate, "emergency_stop_blocks_new_risk"
                    )
                return self._result(
                    intent.intent_id,
                    "emergency_stop_blocks_new_risk",
                    "emergency_stop_blocks_new_risk",
                    snapshot=snapshot,
                )
            blocked = self._new_risk_block(contract)
            if blocked:
                if intent.action == "OPEN_SHORT":
                    return self._unwind(intent, snapshot, contract, gate, blocked)
                return self._result(intent.intent_id, "locked", blocked, snapshot=snapshot)
            if side == "SELL" and intent.quantity > self.client.bid_qty:
                return self._unwind(intent, snapshot, contract, gate, "sell_liquidity_insufficient")
            if side == "BUY" and intent.quantity > self.client.ask_qty:
                return self._result(
                    intent.intent_id, "locked", "buy_liquidity_insufficient", snapshot=snapshot
                )
            payload = build_limit_payload(
                symbol=intent.symbol,
                side=side,
                quantity=intent.quantity,
                price=intent.limit_price,
                client_order_id=client_key,
            )
            self.ledger.begin_operation(intent.intent_id, "entry", client_key, payload)
            try:
                self.client.new_order(payload)
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, "entry", client_key, "unknown", None)
            except MarginExchangeError as exc:
                self.ledger.mark(
                    intent.intent_id,
                    "entry",
                    client_key,
                    "rejected",
                    {"code": exc.code, "message": exc.message},
                )
                if intent.action == "OPEN_SHORT":
                    return self._unwind(intent, snapshot, contract, gate, f"entry_rejected_{exc.code}")
                return self._result(
                    intent.intent_id,
                    "entry_rejected",
                    f"entry_rejected_{exc.code}",
                    snapshot=snapshot,
                )
            else:
                self.ledger.mark(intent.intent_id, "entry", client_key, "awaiting_evidence", {})
        elif str(existing["state"]) == "rejected":
            if intent.action == "OPEN_SHORT":
                return self._unwind(intent, snapshot, contract, gate, "entry_rejected")
            return self._result(intent.intent_id, "entry_rejected", "entry_rejected", snapshot=snapshot)
        resolved = self._resolve_entry(intent, snapshot, contract, gate, client_key, side)
        if isinstance(resolved, TrackResult):
            return resolved
        if getattr(self.client, "interrupt_after", None) == "entry":
            return self._result(
                intent.intent_id, "interrupted_after_entry", "interrupted_after_entry", snapshot=snapshot
            )
        if intent.action == "OPEN_SHORT":
            borrow_amount = intent.quantity
            unsold = borrow_amount - resolved
            if unsold < 0:
                return self._result(intent.intent_id, "fill_mismatch", "sold_more_than_borrowed", snapshot=snapshot)
            if unsold > 0:
                partial = self._repay_amount(
                    intent, snapshot, contract, gate, "partial_repay", "partial-repay", unsold
                )
                if isinstance(partial, TrackResult):
                    return partial
        return resolved

    def _resolve_entry(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        gate: Gate,
        client_key: str,
        side: str,
    ) -> TrackResult | Decimal:
        row = self.ledger.peek(intent.intent_id, "entry", client_key)
        unknown = row is not None and str(row["state"]) == "unknown"
        try:
            order = self.client.query_order(build_query_order_payload(intent.symbol, client_key))
        except MarginExchangeError as exc:
            if exc.code == -2013 and unknown:
                return self._result(intent.intent_id, "entry_unknown", "entry_unconfirmed", snapshot=snapshot)
            return self._result(intent.intent_id, "fill_unconfirmed", "order_query_failed", snapshot=snapshot)
        if not _is_cross(order.get("isIsolated")) or _unexpected_borrow(order):
            self.ledger.mark(intent.intent_id, "entry", client_key, "rejected", {"violation": True})
            return self._result(
                intent.intent_id, "side_effect_violation", "side_effect_or_isolated", snapshot=snapshot
            )
        if order.get("side") != side:
            return self._result(intent.intent_id, "fill_mismatch", "entry_side_mismatch", snapshot=snapshot)
        status = str(order.get("status") or "")
        if status == "NEW":
            if gate.emergency_stop:
                return self._cancel_and_unwind(intent, snapshot, contract, gate, client_key)
            return self._result(intent.intent_id, "entry_resting", "entry_resting", snapshot=snapshot)
        if status == "PARTIALLY_FILLED":
            cancelled = self._cancel_remainder(intent, snapshot, client_key)
            if isinstance(cancelled, TrackResult):
                return cancelled
            order = cancelled
            status = str(order.get("status") or "")
        executed, problem = parse_decimal(order.get("executedQty"), "executedQty")
        if problem or executed is None or executed < 0:
            return self._result(intent.intent_id, "fill_unconfirmed", "executed_qty_invalid", snapshot=snapshot)
        if status in {"CANCELED", "EXPIRED", "REJECTED"} and executed == 0:
            if intent.action == "OPEN_SHORT":
                return self._unwind(intent, snapshot, contract, gate, "entry_not_filled")
            return self._result(intent.intent_id, "entry_rejected", "entry_not_filled", snapshot=snapshot)
        if status not in {"FILLED", "CANCELED", "EXPIRED"}:
            return self._result(intent.intent_id, "fill_unconfirmed", "entry_status_open", snapshot=snapshot)
        trades_ok = self._trades_match(intent.symbol, order, executed)
        if trades_ok is False:
            self.ledger.mark(intent.intent_id, "entry", client_key, "mismatch", {})
            return self._result(intent.intent_id, "fill_mismatch", "trade_qty_mismatch", snapshot=snapshot)
        if trades_ok is None:
            self.ledger.mark(intent.intent_id, "entry", client_key, "awaiting_evidence", {})
            return self._result(intent.intent_id, "fill_unconfirmed", "trades_delayed", snapshot=snapshot)
        self.ledger.mark(
            intent.intent_id,
            "entry",
            client_key,
            "confirmed",
            {"executedQty": api_amount(executed)},
        )
        return executed

    def _cancel_remainder(
        self, intent: Intent, snapshot: Snapshot, entry_key: str
    ) -> TrackResult | dict[str, Any]:
        cancel_key = make_client_id(intent.intent_id, "entry-cancel")
        existing = self.ledger.peek(intent.intent_id, "entry_cancel", cancel_key)
        payload = build_cancel_payload(intent.symbol, entry_key)
        if existing is None:
            self.ledger.begin_operation(intent.intent_id, "entry_cancel", cancel_key, payload)
            try:
                self.client.cancel_order(payload)
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, "entry_cancel", cancel_key, "unknown", None)
            except MarginExchangeError as exc:
                self.ledger.mark(
                    intent.intent_id,
                    "entry_cancel",
                    cancel_key,
                    "unknown",
                    {"code": exc.code},
                )
            else:
                self.ledger.mark(intent.intent_id, "entry_cancel", cancel_key, "confirmed", {})
        try:
            order = self.client.query_order(build_query_order_payload(intent.symbol, entry_key))
        except MarginExchangeError:
            return self._result(
                intent.intent_id, "partial_cancel_unknown", "partial_cancel_unknown", snapshot=snapshot
            )
        if order.get("status") in {"NEW", "PARTIALLY_FILLED"}:
            return self._result(
                intent.intent_id, "partial_cancel_unknown", "partial_cancel_unknown", snapshot=snapshot
            )
        self.ledger.mark(intent.intent_id, "entry_cancel", cancel_key, "confirmed", {})
        return order

    def _cancel_and_unwind(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        gate: Gate,
        entry_key: str,
    ) -> TrackResult:
        cancelled = self._cancel_remainder(intent, snapshot, entry_key)
        if isinstance(cancelled, TrackResult):
            return cancelled
        executed, _ = parse_decimal(cancelled.get("executedQty"), "executedQty")
        if executed and executed > 0:
            return self._result(
                intent.intent_id, "fill_unconfirmed", "resting_cancel_left_a_fill", snapshot=snapshot
            )
        return self._unwind(intent, snapshot, contract, gate, "emergency_cancelled_entry")

    def _ensure_protection(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        gate: Gate,
        executed: Decimal,
    ) -> TrackResult:
        del gate
        view = self._view()
        if isinstance(view, TrackResult):
            return view
        assets, _level = view
        if intent.action == "OPEN_SHORT":
            quantity, reason = self._short_cover(intent, snapshot, contract, assets)
            if quantity is None:
                return self._result(intent.intent_id, "protection_blocked", reason, snapshot=snapshot)
        else:
            base = assets.get(intent.base_asset)
            if base is None or base.free != executed:
                return self._result(
                    intent.intent_id,
                    "protection_blocked",
                    "long_free_base_mismatch",
                    snapshot=snapshot,
                )
            quantity = executed
        rate_problem = self._interest_matches(snapshot, contract)
        if intent.action == "OPEN_SHORT" and rate_problem:
            return self._result(intent.intent_id, "protection_blocked", rate_problem, snapshot=snapshot)
        plan, plan_reason = plan_oco(
            intent,
            quantity=quantity,
            reference_price=snapshot.reference_price,
            tick=snapshot.filters.tick_size,
            step=snapshot.filters.step_size,
            min_notional=snapshot.filters.min_notional,
            fee_rate=snapshot.fee_rate,
            free_quote=assets[intent.quote_asset].free,
            ask_qty=self.client.ask_qty,
            bid_qty=self.client.bid_qty,
        )
        if plan is None:
            if intent.action == "OPEN_SHORT" and plan_reason in {
                "quote_cannot_fund_stop_buy",
                "buy_liquidity_insufficient",
            }:
                return self._flatten(intent, snapshot, contract, quantity, plan_reason)
            return self._result(intent.intent_id, "protection_blocked", plan_reason, snapshot=snapshot)
        existing = self.ledger.peek(intent.intent_id, "oco", plan.list_client_order_id)
        if existing is not None and str(existing["state"]) == "rejected":
            return self._flatten(intent, snapshot, contract, quantity, "oco_rejected")
        if existing is None:
            self.ledger.begin_operation(
                intent.intent_id, "oco", plan.list_client_order_id, plan.payload
            )
            try:
                self.client.new_oco(plan.payload)
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, "oco", plan.list_client_order_id, "unknown", None)
            except MarginExchangeError as exc:
                self.ledger.mark(
                    intent.intent_id,
                    "oco",
                    plan.list_client_order_id,
                    "rejected",
                    {"code": exc.code},
                )
                return self._flatten(intent, snapshot, contract, quantity, f"oco_rejected_{exc.code}")
            else:
                self.ledger.mark(intent.intent_id, "oco", plan.list_client_order_id, "awaiting_evidence", {})
        if not self._oco_is_live(intent, plan.list_client_order_id, plan.side, quantity):
            current = self.ledger.peek(intent.intent_id, "oco", plan.list_client_order_id)
            if current is not None and str(current["state"]) != "unknown":
                self.ledger.mark(intent.intent_id, "oco", plan.list_client_order_id, "unknown", None)
            return self._result(intent.intent_id, "protection_unknown", "oco_unconfirmed", snapshot=snapshot)
        self.ledger.mark(intent.intent_id, "oco", plan.list_client_order_id, "confirmed", {})
        self.ledger.set_state(intent.intent_id, "protected", _now(self.ledger, intent.intent_id))
        return self._result(
            intent.intent_id,
            "protected",
            "protected",
            phase_ok=True,
            protection_confirmed=True,
            snapshot=snapshot,
            detail={"protection_side": plan.side, "protection_qty": api_amount(quantity)},
        )

    def _flatten(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        quantity: Decimal,
        reason: str,
    ) -> TrackResult:
        client_key = make_client_id(intent.intent_id, "flatten")
        existing = self.ledger.peek(intent.intent_id, "flatten", client_key)
        if existing is not None and str(existing["state"]) == "rejected":
            return self._result(
                intent.intent_id, "protection_rejected_naked", reason, snapshot=snapshot
            )
        payload = build_limit_payload(
            symbol=intent.symbol,
            side="BUY",
            quantity=quantity,
            price=snapshot.reference_price,
            client_order_id=client_key,
            time_in_force="IOC",
        )
        if existing is None:
            self.ledger.begin_operation(intent.intent_id, "flatten", client_key, payload)
            try:
                self.client.new_order(payload)
            except (MarginTransportTimeout, MarginExchangeError) as exc:
                state = "unknown" if isinstance(exc, MarginTransportTimeout) else "rejected"
                self.ledger.mark(intent.intent_id, "flatten", client_key, state, {})
                if state == "rejected":
                    return self._result(
                        intent.intent_id, "protection_rejected_naked", reason, snapshot=snapshot
                    )
                return self._result(
                    intent.intent_id, "protection_unknown", "flatten_unconfirmed", snapshot=snapshot
                )
            self.ledger.mark(intent.intent_id, "flatten", client_key, "awaiting_evidence", {})
        elif str(existing["state"]) == "unknown":
            return self._result(
                intent.intent_id, "protection_unknown", "flatten_unconfirmed", snapshot=snapshot
            )
        try:
            order = self.client.query_order(build_query_order_payload(intent.symbol, client_key))
        except MarginExchangeError:
            return self._result(
                intent.intent_id, "protection_unknown", "flatten_unconfirmed", snapshot=snapshot
            )
        executed, _ = parse_decimal(order.get("executedQty"), "executedQty")
        if order.get("status") != "FILLED" or executed != quantity:
            self.ledger.mark(intent.intent_id, "flatten", client_key, "rejected", {})
            return self._result(
                intent.intent_id, "protection_rejected_naked", reason, snapshot=snapshot
            )
        if self._trades_match(intent.symbol, order, quantity) is not True:
            return self._result(intent.intent_id, "fill_unconfirmed", "flatten_trades", snapshot=snapshot)
        repaid = self._repay_full_liability(intent, snapshot, contract, "flatten-repay")
        if isinstance(repaid, TrackResult):
            return repaid
        flat = self._is_flat(intent)
        return self._result(
            intent.intent_id,
            "protection_failed_flattened" if flat else "not_flat",
            reason if not flat else "protection_failed_flattened",
            account_flat=flat,
            snapshot=snapshot,
        )

    def _unwind(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        gate: Gate,
        reason: str,
    ) -> TrackResult:
        view = self._view()
        if isinstance(view, TrackResult):
            return view
        assets, _level = view
        base = assets[intent.base_asset]
        amount = min(base.free, liability(base))
        if amount <= 0:
            flat = self._is_flat(intent)
            state = "risk_dropped_unwound" if reason == "margin_level_below_contract" else "entry_rejected_unwound"
            if reason.startswith("emergency") or reason == "emergency_stop_blocks_new_risk":
                state = "emergency_unwound"
            return self._result(
                intent.intent_id,
                state if flat else "residual_liability_after_unwind",
                reason,
                account_flat=flat,
                snapshot=snapshot,
            )
        repaid = self._repay_amount(
            intent, snapshot, contract, gate, "unwind_repay", "unwind-repay", amount
        )
        if isinstance(repaid, TrackResult):
            return repaid
        flat = self._is_flat(intent)
        if reason == "margin_level_below_contract":
            state = "risk_dropped_unwound" if flat else "residual_liability_after_unwind"
        elif "emergency" in reason:
            state = "emergency_unwound" if flat else "residual_liability_after_unwind"
        else:
            state = "entry_rejected_unwound" if flat else "residual_liability_after_unwind"
        return self._result(
            intent.intent_id, state, reason, account_flat=flat, snapshot=snapshot
        )

    def _repay_after_close(
        self, intent: Intent, snapshot: Snapshot, contract: Contract, gate: Gate
    ) -> TrackResult:
        view = self._view()
        if isinstance(view, TrackResult):
            return view
        assets, _level = view
        debt = liability(assets.get(intent.base_asset)) if intent.action == "OPEN_SHORT" else Decimal(0)
        if any_liability(assets) and intent.action != "OPEN_SHORT":
            return self._result(intent.intent_id, "not_flat", "unexpected_liability", snapshot=snapshot)
        if debt > 0:
            base = assets[intent.base_asset]
            if base.free < debt:
                return self._result(
                    intent.intent_id,
                    "not_flat",
                    "base_short_of_liability",
                    snapshot=snapshot,
                    borrowed=api_amount(base.borrowed),
                    interest=api_amount(base.interest),
                    base_free=api_amount(base.free),
                )
            repaid = self._repay_amount(
                intent, snapshot, contract, gate, "close_repay", "close-repay", debt
            )
            if isinstance(repaid, TrackResult):
                return repaid
        flat = self._is_flat(intent)
        return self._result(
            intent.intent_id,
            "clean_flat" if flat else "not_flat",
            "clean_flat" if flat else "not_flat",
            phase_ok=flat,
            account_flat=flat,
            snapshot=snapshot,
        )

    def _repay_full_liability(
        self, intent: Intent, snapshot: Snapshot, contract: Contract, role: str
    ) -> TrackResult | None:
        view = self._view()
        if isinstance(view, TrackResult):
            return view
        assets, _level = view
        base = assets[intent.base_asset]
        debt = liability(base)
        if debt == 0:
            return None
        if base.free < debt:
            return self._result(
                intent.intent_id, "not_flat", "base_short_of_liability", snapshot=snapshot
            )
        gate_holder = self.ledger.intent_row(intent.intent_id)
        del gate_holder
        dummy_gate = Gate(False, 0, OFFLINE_EXECUTION_MODE)
        return self._repay_amount(intent, snapshot, contract, dummy_gate, "close_repay", role, debt)

    def _repay_amount(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        gate: Gate,
        op_type: str,
        role: str,
        amount: Decimal,
    ) -> TrackResult | None:
        del gate
        if amount <= 0:
            return None
        client_key = make_client_id(intent.intent_id, role)
        payload = build_repay_payload(intent.base_asset, amount)
        existing = self.ledger.peek(intent.intent_id, op_type, client_key)
        if existing is None:
            self.ledger.begin_operation(intent.intent_id, op_type, client_key, payload)
            try:
                response = self.client.repay(payload)
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, op_type, client_key, "unknown", None)
                if not self._bind_transfer("REPAY", intent.base_asset, amount):
                    return self._result(intent.intent_id, "repay_unknown", "repay_unconfirmed", snapshot=snapshot)
                self.ledger.mark(intent.intent_id, op_type, client_key, "confirmed", {"bound": True})
            except MarginExchangeError as exc:
                self.ledger.mark(
                    intent.intent_id, op_type, client_key, "rejected", {"code": exc.code}
                )
                return self._result(
                    intent.intent_id,
                    "repay_rejected",
                    f"repay_rejected_{exc.code}",
                    snapshot=snapshot,
                )
            else:
                self.ledger.mark(intent.intent_id, op_type, client_key, "confirmed", response)
        elif str(existing["state"]) == "rejected":
            return self._result(intent.intent_id, "repay_rejected", "repay_rejected", snapshot=snapshot)
        elif str(existing["state"]) != "confirmed":
            if not self._bind_transfer("REPAY", intent.base_asset, amount):
                return self._result(intent.intent_id, "repay_unknown", "repay_unconfirmed", snapshot=snapshot)
            self.ledger.mark(intent.intent_id, op_type, client_key, "confirmed", {"bound": True})
        view = self._view()
        if isinstance(view, TrackResult):
            return view
        assets, _level = view
        if liability(assets.get(intent.base_asset)) > 0 and op_type == "close_repay":
            return self._result(
                intent.intent_id, "repay_unconfirmed", "liability_remains_after_repay", snapshot=snapshot
            )
        return None

    def _short_cover(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        assets: dict[str, AssetRow],
    ) -> tuple[Decimal | None, str]:
        base = assets.get(intent.base_asset)
        if base is None:
            return None, "base_asset_missing"
        rate = snapshot.hourly_interest_rate.get(contract.borrow_asset)
        if rate is None:
            return None, "asset_interest_rate_missing"
        return cover_buy_quantity(
            borrowed=base.borrowed,
            interest=base.interest,
            hourly_rate=rate,
            horizon_hours=contract.interest_horizon_hours,
            step=snapshot.filters.step_size,
            fee_rate=snapshot.fee_rate,
            fee_on_base=snapshot.fee_asset == intent.base_asset,
            max_base_over_liability=contract.max_base_over_liability,
        )

    def _interest_matches(self, snapshot: Snapshot, contract: Contract) -> str:
        try:
            rows = self.client.query_hourly_interest(
                build_hourly_interest_payload(contract.borrow_asset)
            )
        except MarginExchangeError:
            return "interest_query_failed"
        if not rows:
            return "interest_query_failed"
        number, problem = parse_decimal(rows[0].get("nextHourlyInterestRate"), "rate")
        expected = snapshot.hourly_interest_rate.get(contract.borrow_asset)
        if problem or number is None or expected is None or number != expected:
            return "interest_rate_mismatch"
        return ""

    def _oco_is_live(self, intent: Intent, list_id: str, side: str, quantity: Decimal) -> bool:
        try:
            listed = self.client.query_oco(build_query_oco_payload(intent.symbol, list_id))
        except MarginExchangeError:
            return False
        if listed.get("listOrderStatus") != "EXECUTING":
            return False
        orders = listed.get("orders")
        if not isinstance(orders, list) or len(orders) != 2:
            return False
        for item in orders:
            if not isinstance(item, Mapping):
                return False
            try:
                order = self.client.query_order(
                    build_query_order_payload(intent.symbol, str(item.get("clientOrderId")))
                )
            except MarginExchangeError:
                return False
            qty, problem = parse_decimal(order.get("origQty"), "origQty")
            if (
                problem
                or qty != quantity
                or order.get("side") != side
                or order.get("status") != "NEW"
                or not _is_cross(order.get("isIsolated"))
                or _unexpected_borrow(order)
            ):
                return False
        return True

    def _filled_protection_leg(self, intent: Intent, listed: Mapping[str, Any]) -> TrackResult | None:
        orders = listed.get("orders")
        if not isinstance(orders, list):
            return self._result(intent.intent_id, "exit_not_filled", "oco_orders_missing")
        filled = 0
        expected_side = "BUY" if intent.action == "OPEN_SHORT" else "SELL"
        for item in orders:
            if not isinstance(item, Mapping):
                return self._result(intent.intent_id, "exit_not_filled", "oco_orders_missing")
            try:
                order = self.client.query_order(
                    build_query_order_payload(intent.symbol, str(item.get("clientOrderId")))
                )
            except MarginExchangeError:
                return self._result(intent.intent_id, "fill_unconfirmed", "protection_order_query_failed")
            if order.get("side") != expected_side:
                return self._result(intent.intent_id, "fill_mismatch", "protection_side_mismatch")
            if order.get("status") == "FILLED":
                executed, _ = parse_decimal(order.get("executedQty"), "executedQty")
                if executed is None or self._trades_match(intent.symbol, order, executed) is not True:
                    return self._result(intent.intent_id, "fill_unconfirmed", "protection_trades")
                filled += 1
        if filled != 1:
            return self._result(intent.intent_id, "fill_mismatch", "protection_leg_conflict")
        return None

    def _trades_match(
        self, symbol: str, order: Mapping[str, Any], executed: Decimal
    ) -> bool | None:
        try:
            trades = self.client.query_trades({"symbol": symbol, "orderId": order.get("orderId")})
        except MarginExchangeError:
            return None
        if not trades:
            return None
        total = Decimal(0)
        for trade in trades:
            qty, problem = parse_decimal(trade.get("qty"), "qty")
            if problem or qty is None:
                return False
            asset = str(trade.get("commissionAsset") or "")
            if asset not in {"", symbol[:-4], "USDT"} and not symbol.endswith("USDT"):
                return False
            total += qty
        if total != executed:
            return False
        return True

    def _bind_transfer(self, kind: str, asset: str, amount: Decimal) -> bool:
        try:
            body = self.client.query_borrow_repay(build_borrow_query_payload(asset, kind))
        except MarginExchangeError:
            return False
        matches = []
        for row in body.get("rows") or []:
            if row.get("status") != "CONFIRMED" or row.get("asset") != asset or row.get("type") != kind:
                continue
            number, problem = parse_decimal(row.get("amount"), "amount")
            if problem or number != amount:
                continue
            matches.append(row)
        return len(matches) == 1

    def _new_risk_block(self, contract: Contract) -> str:
        view = self._view()
        if isinstance(view, TrackResult):
            return view.reason
        _assets, level = view
        account = self.client.query_account()
        if account.get("tradeEnabled") is not True or account.get("borrowEnabled") is not True:
            return "account_flags_closed"
        if not str(account.get("accountType") or "").startswith("MARGIN"):
            return "account_type_not_cross_margin"
        if "ISOLATED" in str(account.get("accountType") or "").upper():
            return "isolated_account_rejected"
        if level < contract.min_margin_level:
            return "margin_level_below_contract"
        return ""

    def _match_fresh_account(self, snapshot: Snapshot, contract: Contract) -> str:
        if getattr(self.client, "account_uid", None) != snapshot.uid:
            return "uid_not_bound_on_client"
        if getattr(self.client, "margin_mode", None) != "CROSS":
            return "client_mode_not_cross"
        try:
            account = self.client.query_account()
            borrow = self.client.query_max_borrow(build_max_borrow_payload(contract.borrow_asset))
            rates = self.client.query_hourly_interest(
                build_hourly_interest_payload(contract.borrow_asset)
            )
        except MarginExchangeError:
            return "account_read_failed"
        if account.get("tradeEnabled") is not snapshot.trade_enabled:
            return "account_readback_mismatch"
        if account.get("borrowEnabled") is not snapshot.borrow_enabled:
            return "account_readback_mismatch"
        if account.get("created") is not snapshot.created:
            return "account_readback_mismatch"
        if account.get("accountType") != snapshot.account_type:
            return "account_readback_mismatch"
        level, problem = parse_decimal(account.get("marginLevel"), "marginLevel")
        if problem or level != snapshot.margin_level:
            return "account_readback_mismatch"
        wire = _wire_assets(account)
        if isinstance(wire, str):
            return wire
        for asset in (intent_assets(snapshot)):
            left = snapshot.assets.get(asset)
            right = wire.get(asset)
            if left is None or right is None:
                return "account_readback_mismatch"
            if (left.free, left.locked, left.borrowed, left.interest) != (
                right.free,
                right.locked,
                right.borrowed,
                right.interest,
            ):
                return "account_readback_mismatch"
        amount, amount_problem = parse_decimal(borrow.get("amount"), "amount")
        expected = snapshot.max_borrowable.get(contract.borrow_asset)
        if amount_problem or expected is None or amount != expected:
            return "asset_borrow_readback_mismatch"
        if not rates:
            return "interest_rate_mismatch"
        rate, rate_problem = parse_decimal(rates[0].get("nextHourlyInterestRate"), "rate")
        expected_rate = snapshot.hourly_interest_rate.get(contract.borrow_asset)
        if rate_problem or expected_rate is None or rate != expected_rate:
            return "interest_rate_mismatch"
        return ""

    def _view(self) -> tuple[dict[str, AssetRow], Decimal] | TrackResult:
        try:
            account = self.client.query_account()
        except MarginExchangeError:
            return self._result("", "locked", "account_read_failed")
        assets = _wire_assets(account)
        if isinstance(assets, str):
            return self._result("", "locked", assets)
        level, problem = parse_decimal(account.get("marginLevel"), "marginLevel")
        if problem or level is None:
            return self._result("", "locked", "margin_level_invalid")
        return assets, level

    def _is_flat(self, intent: Intent) -> bool:
        row = self.ledger.intent_row(intent.intent_id)
        if row is None or row["baseline_base"] is None:
            return False
        baseline, problem = parse_decimal(row["baseline_base"], "baseline")
        if problem or baseline is None:
            return False
        view = self._view()
        if isinstance(view, TrackResult):
            return False
        assets, _level = view
        if any_liability(assets):
            return False
        for asset_row in assets.values():
            if asset_row.locked != 0:
                return False
        base = assets.get(intent.base_asset)
        if base is None or base.free != baseline:
            return False
        try:
            open_orders = self.client.query_open_orders(
                {"symbol": intent.symbol, "isIsolated": "FALSE"}
            )
        except MarginExchangeError:
            return False
        return not open_orders

    def _parse(
        self,
        intent_raw: Mapping[str, Any],
        snapshot_raw: Mapping[str, Any],
        contract_raw: Mapping[str, Any],
        gate_raw: Mapping[str, Any],
    ) -> tuple[Intent, Snapshot, Contract, Gate] | TrackResult:
        intent, intent_reasons = parse_intent(intent_raw)
        snapshot, snapshot_reasons = parse_snapshot(snapshot_raw)
        contract, contract_reasons = parse_contract(contract_raw)
        gate, gate_reasons = parse_gate(gate_raw)
        reasons = intent_reasons + snapshot_reasons + contract_reasons + gate_reasons
        if reasons or intent is None or snapshot is None or contract is None or gate is None:
            return self._result(
                str(intent_raw.get("intent_id") or ""),
                "locked",
                reasons[0] if reasons else "invalid",
                detail={"reasons": reasons},
            )
        return intent, snapshot, contract, gate

    def _result(
        self,
        intent_id: str,
        state: str,
        reason: str,
        *,
        phase_ok: bool = False,
        account_flat: bool | None = None,
        protection_confirmed: bool = False,
        snapshot: Snapshot | None = None,
        borrowed: str = "0",
        interest: str = "0",
        base_free: str = "0",
        detail: dict[str, Any] | None = None,
    ) -> TrackResult:
        stored = self.ledger.intent_row(intent_id) if intent_id else None
        if snapshot is not None and stored is not None:
            view = self._view()
            if not isinstance(view, TrackResult):
                assets, _level = view
                payload = json.loads(str(stored["payload_json"]))
                base = assets.get(str(payload["intent"]["base_asset"]))
                if base is not None:
                    borrowed = api_amount(base.borrowed)
                    interest = api_amount(base.interest)
                    base_free = api_amount(base.free)
                if account_flat is None and not _unknown_state(state):
                    account_flat = self._is_flat(_intent_for_flat(payload["intent"]))
        if _unknown_state(state):
            account_flat = None
        if intent_id:
            self.ledger.set_state(intent_id, state, _now(self.ledger, intent_id))
        return TrackResult(
            intent_id=intent_id,
            state=state,
            phase_ok=phase_ok,
            account_flat=account_flat,
            protection_confirmed=protection_confirmed,
            live_writes_blocked=True,
            reason=reason,
            borrowed=borrowed,
            interest=interest,
            base_free=base_free,
            detail={} if detail is None else detail,
        )


def _unknown_state(state: str) -> bool:
    return state.endswith("_unknown") or state in {"fill_unconfirmed"}


def _is_cross(value: object) -> bool:
    return value in (False, "FALSE", "false")


def _unexpected_borrow(payload: Mapping[str, Any]) -> bool:
    if "marginBuyBorrowAmount" not in payload:
        return False
    number, problem = parse_decimal(payload.get("marginBuyBorrowAmount"), "marginBuyBorrowAmount")
    if problem or number is None:
        return True
    return number > 0


def _wire_assets(account: Mapping[str, Any]) -> dict[str, AssetRow] | str:
    raw_assets = account.get("userAssets")
    if not isinstance(raw_assets, list):
        return "account_assets_invalid"
    parsed: dict[str, AssetRow] = {}
    for item in raw_assets:
        if not isinstance(item, Mapping):
            return "account_assets_invalid"
        asset = item.get("asset")
        if not isinstance(asset, str):
            return "account_assets_invalid"
        values = []
        for field in ("free", "locked", "borrowed", "interest"):
            number, problem = parse_decimal(item.get(field), field)
            if problem or number is None or number < 0:
                return "account_assets_invalid"
            values.append(number)
        parsed[asset] = AssetRow(asset, values[0], values[1], values[2], values[3])
    return parsed


def intent_assets(snapshot: Snapshot) -> list[str]:
    return sorted(snapshot.assets)


def _now(ledger: Ledger, intent_id: str) -> int:
    row = ledger.intent_row(intent_id)
    if row is None:
        return 0
    return int(row["updated_ms"])


def _intent_for_flat(raw: Mapping[str, Any]) -> Intent:
    parsed, _reasons = parse_intent(raw)
    if parsed is None:
        raise RuntimeError("stored intent failed to parse")
    return parsed
