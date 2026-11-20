"""Cross-margin state machine with timeout & restart idempotency guarantees.

Invariants enforced across crashes, timeouts, and multi-tick restarts:
1. A second borrow, entry order, cancel, OCO, flatten, or repay is NEVER sent
   for the same ledger key.
2. Any unresolved, unknown, naked, or unflat intent in the ledger blocks new
   intents across ticks/restarts, and any ``protected`` intent blocks same-symbol
   pyramiding.
3. Network timeouts on both writes and reads transition cleanly to indeterminate
   states and reconcile strictly via official readback (``borrow-repay`` with
   ``startTime`` & ``prior_tx_ids`` + account liability delta, ``order``,
   ``orderList``, ``myTrades``, ``openOrders``, ``account``).
4. Working native OCO protection is never cancelled or replaced.
5. Live network transport stays disabled unless ``allow_live_network=True``,
   ``CROSS_MARGIN_PARAMETERS_APPROVED=1``, ``CROSS_MARGIN_LIVE_ENABLED=1``,
   and ``LIVE_TRADING_DISABLED`` is absent (while protective exit/repay of an
   already-open intent remains permitted unless ``CROSS_MARGIN_BLOCK_EXIT_ON_KILL=1``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_FLOOR
import json
import os
from typing import Any

from execution.margin_cross.accounting import any_liability, cover_buy_quantity, liability
from execution.margin_cross.api_semantics import (
    build_borrow_payload,
    build_borrow_query_payload,
    build_cancel_payload,
    build_hourly_interest_payload,
    build_interest_history_payload,
    build_limit_payload,
    build_max_borrow_payload,
    build_query_oco_payload,
    build_query_open_orders_payload,
    build_query_order_payload,
    build_query_trades_payload,
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
from execution.margin_cross.policy import (
    APPROVED_DRY_RUN_MODE,
    EMERGENCY_MARGIN_LEVEL_STATUSES,
    LIVE_EXECUTION_MODE,
    PENDING_TRANSACTION_CODE,
    ApprovedRiskConfig,
    fill_tp_formula_b_enabled,
    kill_switch_active,
    live_gate_reasons,
    load_approved_risk_config,
    parameters_approved_by_env,
)
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
from execution.margin_cross.strategy_gate import apply_formula_b_take_profit

_CLEAN_TERMINAL_STATES = frozenset(
    {
        "clean_flat",
        "protection_failed_flattened",
        "entry_rejected_unwound",
        "risk_dropped_unwound",
        "emergency_unwound",
        "locked",
        "borrow_rejected",
        "entry_rejected",
        "otoco_not_submitted",
        "live_network_disabled",
        "emergency_stop_blocks_new_risk",
        "intent_conflict",
    }
)
_WORKING_PROTECTED_STATES = frozenset({"protected", "protection_still_working"})


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
    def __init__(
        self,
        ledger_path: str,
        client: Any,
        *,
        allow_live_network: bool = False,
        kill_switch_path: str | os.PathLike[str] | None = None,
        approved_config: ApprovedRiskConfig | None = None,
        now_ms: int | None = None,
    ) -> None:
        self._network_client = getattr(client, "performs_network_io", True) is not False
        self._allow_live_network = bool(allow_live_network)
        self._kill_switch_path = kill_switch_path
        self._approved_config = approved_config
        signer_ok = bool(callable(getattr(client, "is_signer_attested", None)) and client.is_signer_attested(now_ms=now_ms))
        if self._network_client:
            reasons = live_gate_reasons(
                allow_live_network=self._allow_live_network,
                for_protective_or_repay=True,
                explicit_kill_path=self._kill_switch_path,
                approved_config=self._approved_config,
                now_ms=now_ms,
                signer_attested=signer_ok,
            )
            if reasons:
                raise LiveNetworkDisabled(
                    f"cross-margin live transport is disabled ({reasons[0]})"
                )
        self.client = client
        self.ledger = Ledger(ledger_path)

    def _mode_block(
        self,
        execution_mode: str,
        *,
        for_protective_or_repay: bool,
        now_ms: int | None = None,
    ) -> str | None:
        if execution_mode == OFFLINE_EXECUTION_MODE:
            if self._network_client:
                return "live_network_disabled"
            return None
        if execution_mode == APPROVED_DRY_RUN_MODE:
            cfg = self._approved_config or load_approved_risk_config(now_ms=now_ms)[0]
            if self._network_client or not parameters_approved_by_env() or cfg is None:
                return "live_network_disabled"
            return None
        if execution_mode == LIVE_EXECUTION_MODE:
            if not self._network_client:
                return "live_network_disabled"
            signer_ok = bool(callable(getattr(self.client, "is_signer_attested", None)) and self.client.is_signer_attested(now_ms=now_ms))
            reasons = live_gate_reasons(
                allow_live_network=self._allow_live_network,
                for_protective_or_repay=for_protective_or_repay,
                explicit_kill_path=self._kill_switch_path,
                approved_config=self._approved_config,
                now_ms=now_ms,
                signer_attested=signer_ok,
            )
            if "kill_switch_active" in reasons and not for_protective_or_repay:
                return "emergency_stop_blocks_new_risk"
            if reasons:
                return "live_network_disabled"
            return None
        return "live_network_disabled"

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
        row = self.ledger.intent_row(intent.intent_id)
        mode_err = self._mode_block(
            gate.execution_mode,
            for_protective_or_repay=row is not None,
            now_ms=gate.now_ms,
        )
        if mode_err == "emergency_stop_blocks_new_risk":
            return self._result(
                intent.intent_id,
                "emergency_stop_blocks_new_risk",
                "emergency_stop_blocks_new_risk",
            )
        if mode_err:
            return self._result(intent.intent_id, "live_network_disabled", "live_network_disabled")

        if row is None:
            reasons = contract_identity_reasons(intent, contract, snapshot, gate, approved_config=self._approved_config)
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
            prior_block = self._blocking_ledger_intent(intent.intent_id, intent.symbol, intent.base_asset)
            if prior_block:
                return self._result(intent.intent_id, "locked", prior_block)
            if any_liability(snapshot.assets):
                return self._result(
                    intent.intent_id, "locked", "existing_liability_blocks_new_open"
                )
            mismatch = self._match_fresh_account(snapshot, contract, intent)
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
            if gate.execution_mode == LIVE_EXECUTION_MODE:
                kill_on, _name = kill_switch_active(self._kill_switch_path)
                if kill_on and not gate.emergency_stop:
                    gate = Gate(
                        emergency_stop=True,
                        now_ms=gate.now_ms,
                        execution_mode=gate.execution_mode,
                    )
        return self._continue(intent, snapshot, contract, gate)

    def complete_exit(self, intent_id: str, gate_raw: Mapping[str, Any]) -> TrackResult:
        gate, gate_reasons = parse_gate(gate_raw)
        if gate is None or gate_reasons:
            return self._result(intent_id, "live_network_disabled", "live_network_disabled")
        mode_err = self._mode_block(gate.execution_mode, for_protective_or_repay=True, now_ms=gate.now_ms)
        if mode_err:
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
            return self._result(
                intent.intent_id, "not_ready", "protection_not_placed", snapshot=snapshot
            )
        try:
            listed = self.client.query_oco(build_query_oco_payload(intent.symbol, list_id))
        except (MarginExchangeError, MarginTransportTimeout):
            return self._result(
                intent.intent_id, "protection_unknown", "oco_query_failed", snapshot=snapshot
            )
        if (
            listed.get("symbol") != intent.symbol
            or not _is_cross(listed.get("isIsolated", False))
            or str(listed.get("listClientOrderId") or "") != list_id
        ):
            return self._result(
                intent.intent_id,
                "side_effect_violation",
                "oco_symbol_or_isolated_mismatch",
                snapshot=snapshot,
            )
        if listed.get("listOrderStatus") == "EXECUTING":
            return self._result(
                intent.intent_id,
                "protection_still_working",
                "protection_left_in_place",
                protection_confirmed=True,
                snapshot=snapshot,
            )
        if listed.get("listOrderStatus") != "ALL_DONE":
            return self._result(
                intent.intent_id, "exit_not_filled", "exit_not_filled", snapshot=snapshot
            )
        filled = self._filled_protection_leg(intent, snapshot, listed)
        if isinstance(filled, TrackResult):
            return filled
        return self._repay_after_close(intent, snapshot, contract, gate, filled)

    def _blocking_ledger_intent(
        self, current_intent_id: str, symbol: str, base_asset: str = ""
    ) -> str:
        for item in self.ledger.list_intents():
            other_id = str(item["intent_id"])
            if other_id == current_intent_id:
                continue
            state = str(item["state"])
            if state in _CLEAN_TERMINAL_STATES:
                continue
            if state in _WORKING_PROTECTED_STATES:
                try:
                    stored = json.loads(str(item["payload_json"]))
                    other_sym = str(stored.get("intent", {}).get("symbol") or "")
                    other_base = str(stored.get("intent", {}).get("base_asset") or "")
                    other_borrow = str(stored.get("contract", {}).get("borrow_asset") or "")
                except (TypeError, ValueError, AttributeError):
                    other_sym = ""
                    other_base = ""
                    other_borrow = ""
                if not other_sym or other_sym == symbol:
                    return "same_symbol_position_open"
                if base_asset and base_asset in {other_base, other_borrow}:
                    return "same_base_asset_position_open"
                continue
            return "unresolved_intent_blocks_new_risk"
        return ""

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

    def _capture_transfer_baseline(
        self, kind: str, asset: str, now_ms: int
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "started_ms": int(now_ms),
            "prior_tx_ids": [],
            "prior_interest_tx_ids": [],
            "pre_borrowed": None,
            "pre_interest": None,
            "br_captured": False,
            "ih_captured": False,
        }
        try:
            body = self.client.query_borrow_repay(build_borrow_query_payload(asset, kind))
        except (MarginExchangeError, MarginTransportTimeout):
            body = None
        if isinstance(body, Mapping) and isinstance(body.get("rows"), list):
            prior_ids: list[str] = []
            for row in body.get("rows") or []:
                if isinstance(row, Mapping) and row.get("txId") not in (None, ""):
                    prior_ids.append(str(row["txId"]))
            meta["prior_tx_ids"] = prior_ids
            meta["br_captured"] = True
        if kind == "BORROW" and hasattr(self.client, "query_interest_history"):
            try:
                ih_body = self.client.query_interest_history(
                    build_interest_history_payload(asset)
                )
            except (MarginExchangeError, MarginTransportTimeout):
                ih_body = None
            if isinstance(ih_body, Mapping) and isinstance(ih_body.get("rows"), list):
                prior_ih: list[str] = []
                for ih_row in ih_body.get("rows") or []:
                    if isinstance(ih_row, Mapping) and ih_row.get("txId") not in (None, ""):
                        prior_ih.append(str(ih_row["txId"]))
                meta["prior_interest_tx_ids"] = prior_ih
                meta["ih_captured"] = True
        view = self._view()
        if not isinstance(view, TrackResult):
            assets, _level = view
            asset_row = assets.get(asset)
            if asset_row is not None:
                meta["pre_borrowed"] = api_amount(asset_row.borrowed)
                meta["pre_interest"] = api_amount(asset_row.interest)
        return meta

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
            baseline = self._capture_transfer_baseline(
                "BORROW", contract.borrow_asset, gate.now_ms
            )
            stored_req = {**payload, "_meta": baseline}
            flag = self.ledger.begin_operation(
                intent.intent_id, "borrow", client_key, stored_req
            )
            if flag != "inserted":
                return self._result(intent.intent_id, "borrow_unknown", "borrow_not_inserted")
            try:
                response = self.client.borrow(payload)
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, "borrow", client_key, "unknown", None)
                if not self._bind_transfer(
                    "BORROW", contract.borrow_asset, intent.quantity, baseline
                ):
                    return self._result(
                        intent.intent_id,
                        "borrow_unknown",
                        "borrow_unconfirmed",
                        snapshot=snapshot,
                    )
                self.ledger.mark(intent.intent_id, "borrow", client_key, "confirmed", {"bound": True})
            except MarginExchangeError as exc:
                if exc.is_indeterminate or exc.is_rate_limited:
                    self.ledger.mark(intent.intent_id, "borrow", client_key, "unknown", None)
                    if not self._bind_transfer(
                        "BORROW", contract.borrow_asset, intent.quantity, baseline
                    ):
                        return self._result(
                            intent.intent_id,
                            "borrow_unknown",
                            "borrow_unconfirmed",
                            snapshot=snapshot,
                        )
                    self.ledger.mark(
                        intent.intent_id, "borrow", client_key, "confirmed", {"bound": True}
                    )
                elif exc.code == PENDING_TRANSACTION_CODE:
                    self.ledger.mark(
                        intent.intent_id,
                        "borrow",
                        client_key,
                        "pending_lock",
                        {"code": exc.code, "message": exc.message},
                    )
                    return self._result(
                        intent.intent_id,
                        "borrow_unknown",
                        "borrow_pending_transaction_lock",
                        snapshot=snapshot,
                    )
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
                return self._result(
                    intent.intent_id, "borrow_rejected", "borrow_rejected", snapshot=snapshot
                )
            op_meta = _extract_op_meta(existing)
            if state != "confirmed" and not self._bind_transfer(
                "BORROW", contract.borrow_asset, intent.quantity, op_meta
            ):
                return self._result(
                    intent.intent_id,
                    "borrow_unknown",
                    "borrow_unconfirmed",
                    snapshot=snapshot,
                )
            if state != "confirmed":
                self.ledger.mark(
                    intent.intent_id, "borrow", client_key, "confirmed", {"bound": True}
                )
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
                return self._unwind(
                    intent, snapshot, contract, gate, "sell_liquidity_insufficient"
                )
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
                if exc.is_indeterminate:
                    self.ledger.mark(intent.intent_id, "entry", client_key, "unknown", None)
                else:
                    self.ledger.mark(
                        intent.intent_id,
                        "entry",
                        client_key,
                        "rejected",
                        {"code": exc.code, "message": exc.message},
                    )
                    if intent.action == "OPEN_SHORT":
                        return self._unwind(
                            intent, snapshot, contract, gate, f"entry_rejected_{exc.code}"
                        )
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
            return self._result(
                intent.intent_id, "entry_rejected", "entry_rejected", snapshot=snapshot
            )
        resolved = self._resolve_entry(intent, snapshot, contract, gate, client_key, side)
        if isinstance(resolved, TrackResult):
            return resolved
        if getattr(self.client, "interrupt_after", None) == "entry":
            return self._result(
                intent.intent_id,
                "interrupted_after_entry",
                "interrupted_after_entry",
                snapshot=snapshot,
            )
        return self._repay_unsold_short_remainder(intent, snapshot, contract, gate, resolved)

    def _repay_unsold_short_remainder(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        gate: Gate,
        executed: Decimal,
    ) -> TrackResult | Decimal:
        if intent.action == "OPEN_SHORT":
            borrow_amount = intent.quantity
            unsold = borrow_amount - executed
            if unsold < 0:
                return self._result(
                    intent.intent_id,
                    "fill_mismatch",
                    "sold_more_than_borrowed",
                    snapshot=snapshot,
                )
            if unsold > 0:
                partial = self._repay_amount(
                    intent, snapshot, contract, gate, "partial_repay", "partial-repay", unsold
                )
                if isinstance(partial, TrackResult):
                    return partial
        return executed

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
        indeterminate = row is not None and str(row["state"]) in {"unknown", "started"}
        try:
            order = self.client.query_order(build_query_order_payload(intent.symbol, client_key))
        except MarginTransportTimeout:
            return self._result(
                intent.intent_id,
                "entry_unknown" if indeterminate else "fill_unconfirmed",
                "order_query_timeout",
                snapshot=snapshot,
            )
        except MarginExchangeError as exc:
            if indeterminate and (exc.code == -2013 or exc.is_indeterminate or exc.is_rate_limited):
                return self._result(
                    intent.intent_id, "entry_unknown", "entry_unconfirmed", snapshot=snapshot
                )
            return self._result(
                intent.intent_id, "fill_unconfirmed", "order_query_failed", snapshot=snapshot
            )
        if not _is_cross(order.get("isIsolated")) or _unexpected_borrow(order):
            self.ledger.mark(
                intent.intent_id, "entry", client_key, "rejected", {"violation": True}
            )
            return self._result(
                intent.intent_id,
                "side_effect_violation",
                "side_effect_or_isolated",
                snapshot=snapshot,
            )
        orig_qty, orig_err = parse_decimal(order.get("origQty"), "origQty")
        if (
            order.get("symbol") != intent.symbol
            or str(order.get("clientOrderId") or "") != client_key
            or orig_err
            or orig_qty != intent.quantity
        ):
            return self._result(
                intent.intent_id, "fill_mismatch", "entry_identity_mismatch", snapshot=snapshot
            )
        if order.get("side") != side:
            return self._result(
                intent.intent_id, "fill_mismatch", "entry_side_mismatch", snapshot=snapshot
            )
        status = str(order.get("status") or "")
        if str(row["state"] if row is not None else "") == "confirmed" and status == "NEW":
            # A confirmed fill is not put back to resting by a later NEW read.
            stored = {}
            try:
                stored = json.loads(str(row["response_json"] or "{}"))
            except (TypeError, ValueError):
                stored = {}
            executed, problem = parse_decimal(stored.get("executedQty"), "executedQty")
            if problem or executed is None or executed <= 0:
                return self._result(
                    intent.intent_id, "fill_unconfirmed", "confirmed_qty_unreadable", snapshot=snapshot
                )
            return executed
        if status == "NEW":
            if gate.emergency_stop or self._resting_margin_degraded(contract):
                return self._cancel_and_unwind(
                    intent, snapshot, contract, gate, client_key
                )
            return self._result(
                intent.intent_id, "entry_resting", "entry_resting", snapshot=snapshot
            )
        if status == "PARTIALLY_FILLED":
            cancelled = self._cancel_remainder(intent, snapshot, client_key)
            if isinstance(cancelled, TrackResult):
                return cancelled
            order = cancelled
            status = str(order.get("status") or "")
        executed, problem = parse_decimal(order.get("executedQty"), "executedQty")
        if problem or executed is None or executed < 0:
            return self._result(
                intent.intent_id, "fill_unconfirmed", "executed_qty_invalid", snapshot=snapshot
            )
        if status in {"CANCELED", "EXPIRED", "REJECTED"} and executed == 0:
            if intent.action == "OPEN_SHORT":
                return self._unwind(intent, snapshot, contract, gate, "entry_not_filled")
            return self._result(
                intent.intent_id, "entry_rejected", "entry_not_filled", snapshot=snapshot
            )
        if status not in {"FILLED", "CANCELED", "EXPIRED"}:
            return self._result(
                intent.intent_id, "fill_unconfirmed", "entry_status_open", snapshot=snapshot
            )
        trades_ok = self._trades_match(intent.symbol, order, executed)
        if trades_ok is False:
            self.ledger.mark(intent.intent_id, "entry", client_key, "mismatch", {})
            return self._result(
                intent.intent_id, "fill_mismatch", "trade_qty_mismatch", snapshot=snapshot
            )
        if trades_ok is None:
            self.ledger.mark(intent.intent_id, "entry", client_key, "awaiting_evidence", {})
            return self._result(
                intent.intent_id, "fill_unconfirmed", "trades_delayed", snapshot=snapshot
            )
        self.ledger.mark(
            intent.intent_id,
            "entry",
            client_key,
            "confirmed",
            {
                "executedQty": api_amount(executed),
                "orderId": order.get("orderId"),
            },
        )
        return executed

    def _resting_margin_degraded(self, contract: Contract) -> bool:
        if not contract.parameters_approved or contract.warn_margin_level is None:
            return False
        view = self._view()
        if isinstance(view, TrackResult):
            return False
        _assets, level = view
        return level < contract.warn_margin_level

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
        except (MarginExchangeError, MarginTransportTimeout):
            return self._result(
                intent.intent_id,
                "partial_cancel_unknown",
                "partial_cancel_unknown",
                snapshot=snapshot,
            )
        if order.get("status") in {"NEW", "PARTIALLY_FILLED"}:
            return self._result(
                intent.intent_id,
                "partial_cancel_unknown",
                "partial_cancel_unknown",
                snapshot=snapshot,
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
        executed, err = parse_decimal(cancelled.get("executedQty"), "executedQty")
        if err or executed is None or executed < 0:
            return self._result(
                intent.intent_id, "fill_unconfirmed", "executed_qty_invalid", snapshot=snapshot
            )
        if executed > 0:
            # Cancel raced against a real fill: verify trades, repay unsold borrow,
            # and arm protective OCO immediately so exposure is never left naked.
            trades_ok = self._trades_match(intent.symbol, cancelled, executed)
            if trades_ok is False:
                self.ledger.mark(intent.intent_id, "entry", entry_key, "mismatch", {})
                return self._result(
                    intent.intent_id, "fill_mismatch", "trade_qty_mismatch", snapshot=snapshot
                )
            if trades_ok is None:
                self.ledger.mark(intent.intent_id, "entry", entry_key, "awaiting_evidence", {})
                return self._result(
                    intent.intent_id, "fill_unconfirmed", "trades_delayed", snapshot=snapshot
                )
            self.ledger.mark(
                intent.intent_id,
                "entry",
                entry_key,
                "confirmed",
                {
                    "executedQty": api_amount(executed),
                    "orderId": cancelled.get("orderId"),
                },
            )
            settled = self._repay_unsold_short_remainder(
                intent, snapshot, contract, gate, executed
            )
            if isinstance(settled, TrackResult):
                return settled
            return self._result_or_protect(intent, snapshot, contract, gate, settled)
        if intent.action == "OPEN_SHORT":
            return self._unwind(intent, snapshot, contract, gate, "emergency_cancelled_entry")
        return self._result(
            intent.intent_id,
            "emergency_unwound",
            "emergency_cancelled_entry",
            account_flat=self._is_flat(intent),
            snapshot=snapshot,
        )

    def _result_or_protect(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        gate: Gate,
        executed: Decimal,
    ) -> TrackResult:
        return self._ensure_protection(intent, snapshot, contract, gate, executed)

    def _effective_protection_intent(
        self, intent: Intent, snapshot: Snapshot, contract: Contract
    ) -> Intent:
        if not (contract.formula_b_enabled or fill_tp_formula_b_enabled()):
            return intent
        entry_key = make_client_id(intent.intent_id, "entry")
        try:
            order = self.client.query_order(build_query_order_payload(intent.symbol, entry_key))
        except (MarginExchangeError, MarginTransportTimeout):
            return intent
        avg_fill = self._weighted_fill_price(intent.symbol, order)
        if avg_fill is None or avg_fill <= 0:
            return intent
        adjusted_tp = apply_formula_b_take_profit(
            action=intent.action,
            planned_entry=intent.limit_price,
            planned_tp=intent.take_profit_price,
            actual_fill=avg_fill,
            tick_size=snapshot.filters.tick_size,
            tp_multiplier=Decimal("1"),
        )
        if adjusted_tp is None:
            return intent
        return replace(intent, take_profit_price=adjusted_tp)

    def _weighted_fill_price(self, symbol: str, order: Mapping[str, Any]) -> Decimal | None:
        try:
            trades = self.client.query_trades(
                build_query_trades_payload(symbol, order.get("orderId"))
            )
        except (MarginExchangeError, MarginTransportTimeout):
            return None
        if not trades:
            return None
        total_qty = Decimal(0)
        total_quote = Decimal(0)
        for trade in trades:
            qty, q_err = parse_decimal(trade.get("qty"), "qty")
            px, p_err = parse_decimal(trade.get("price"), "price")
            if q_err or p_err or qty is None or px is None or qty <= 0 or px <= 0:
                return None
            total_qty += qty
            total_quote += qty * px
        if total_qty <= 0:
            return None
        return total_quote / total_qty

    def _long_net_base(self, intent: Intent, executed: Decimal) -> Decimal | None:
        """Filled base minus base-asset commission. None when the fill is unconfirmed."""
        client_key = make_client_id(intent.intent_id, "entry")
        try:
            order = self.client.query_order(build_query_order_payload(intent.symbol, client_key))
        except (MarginExchangeError, MarginTransportTimeout):
            return None
        return self._net_base_from_order(intent.symbol, intent.base_asset, order, executed)

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
        list_id = make_client_id(intent.intent_id, "oco-list")
        existing = self.ledger.peek(intent.intent_id, "oco", list_id)
        lot_residual = Decimal(0)
        if existing is not None:
            stored_qty, qty_problem = parse_decimal(
                json.loads(str(existing["request_json"])).get("quantity"), "quantity"
            )
            if qty_problem or stored_qty is None or stored_qty <= 0:
                return self._result(
                    intent.intent_id,
                    "protection_blocked",
                    "protection_qty_unreadable",
                    snapshot=snapshot,
                )
            quantity = stored_qty
            stored_residual, residual_err = parse_decimal(
                json.loads(str(existing["request_json"])).get("oco_lot_residual_qty") or "0",
                "oco_lot_residual_qty",
            )
            lot_residual = stored_residual if not residual_err and stored_residual is not None else Decimal(0)
        elif intent.action == "OPEN_SHORT":
            quantity, reason = self._short_cover(intent, snapshot, contract, assets)
            if quantity is None:
                return self._result(
                    intent.intent_id, "protection_blocked", reason, snapshot=snapshot
                )
        else:
            net = self._long_net_base(intent, executed)
            if net is None:
                return self._result(
                    intent.intent_id,
                    "protection_blocked",
                    "long_base_fee_unconfirmed",
                    snapshot=snapshot,
                )
            floored, residual = _floor_lot(net, snapshot.filters.step_size)
            base = assets.get(intent.base_asset)
            baseline = self._baseline_base_qty(intent.intent_id)
            held = None if base is None or baseline is None else base.free - baseline
            # Free must cover the floored lot, not the pre-floor net.
            # Held above the fill's own net is someone else's base.
            if (
                base is None
                or baseline is None
                or held is None
                or floored <= 0
                or residual < 0
                or residual >= snapshot.filters.step_size
                or held < floored
                or held > net
            ):
                return self._result(
                    intent.intent_id,
                    "protection_blocked",
                    "long_free_base_mismatch",
                    snapshot=snapshot,
                )
            quantity = floored
            lot_residual = residual
        rate_problem = self._interest_matches(snapshot, contract)
        if intent.action == "OPEN_SHORT" and rate_problem:
            return self._result(
                intent.intent_id, "protection_blocked", rate_problem, snapshot=snapshot
            )
        if existing is None:
            plan_intent = self._effective_protection_intent(intent, snapshot, contract)
        else:
            plan_intent = _stored_oco_intent(intent, existing)
        plan, plan_reason = plan_oco(
            plan_intent,
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
            return self._result(
                intent.intent_id, "protection_blocked", plan_reason, snapshot=snapshot
            )
        if existing is not None and str(existing["state"]) == "rejected":
            # A stored code-only reject is not proof the list is absent.
            # Do not start a buyback. An already-started flatten may only resume.
            flatten_row = self.ledger.peek(
                intent.intent_id, "flatten", make_client_id(intent.intent_id, "flatten")
            )
            if flatten_row is None:
                return self._result(
                    intent.intent_id,
                    "protection_unknown",
                    "oco_reject_not_proven",
                    snapshot=snapshot,
                )
            return self._flatten(intent, snapshot, contract, quantity, "oco_rejected")
        if existing is None:
            stored_request = dict(plan.payload)
            stored_request["oco_lot_residual_qty"] = api_amount(lot_residual)
            self.ledger.begin_operation(
                intent.intent_id, "oco", plan.list_client_order_id, stored_request
            )
            try:
                self.client.new_oco(plan.payload)
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, "oco", plan.list_client_order_id, "unknown", None)
            except MarginExchangeError as exc:
                # No integer code, message variant, or empty openOrders read proves
                # the POST missed the book. Occupy the slot and do not buy back.
                # A later query may still bind a list that actually exists.
                self.ledger.mark(
                    intent.intent_id,
                    "oco",
                    plan.list_client_order_id,
                    "unknown",
                    {"code": exc.code, "message": exc.message},
                )
            else:
                self.ledger.mark(
                    intent.intent_id, "oco", plan.list_client_order_id, "awaiting_evidence", {}
                )
        if not self._oco_is_live(intent, plan.list_client_order_id, plan.side, quantity):
            current = self.ledger.peek(intent.intent_id, "oco", plan.list_client_order_id)
            if current is not None and str(current["state"]) != "unknown":
                self.ledger.mark(
                    intent.intent_id, "oco", plan.list_client_order_id, "unknown", None
                )
            return self._result(
                intent.intent_id, "protection_unknown", "oco_unconfirmed", snapshot=snapshot
            )
        self.ledger.mark(
            intent.intent_id,
            "oco",
            plan.list_client_order_id,
            "confirmed",
            {"take_profit_price": plan.payload["price"], "stop_price": plan.payload["stopPrice"]},
        )
        self.ledger.set_state(intent.intent_id, "protected", _now(self.ledger, intent.intent_id))
        return self._result(
            intent.intent_id,
            "protected",
            "protected",
            phase_ok=True,
            protection_confirmed=True,
            snapshot=snapshot,
            detail={
                "protection_side": plan.side,
                "protection_qty": api_amount(quantity),
                "take_profit_price": plan.payload["price"],
                "stop_price": plan.payload["stopPrice"],
                "oco_lot_residual_qty": api_amount(lot_residual),
            },
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
                intent.intent_id,
                "protection_rejected_naked",
                reason,
                snapshot=snapshot,
                detail={"unprotected_qty": api_amount(quantity), "manual_action": "cover_and_repay"},
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
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, "flatten", client_key, "unknown", None)
            except MarginExchangeError as exc:
                if exc.is_indeterminate:
                    self.ledger.mark(intent.intent_id, "flatten", client_key, "unknown", None)
                else:
                    self.ledger.mark(
                        intent.intent_id, "flatten", client_key, "rejected", {"code": exc.code}
                    )
                    return self._result(
                        intent.intent_id,
                        "protection_rejected_naked",
                        reason,
                        snapshot=snapshot,
                        detail={
                            "unprotected_qty": api_amount(quantity),
                            "manual_action": "cover_and_repay",
                        },
                    )
            else:
                self.ledger.mark(
                    intent.intent_id, "flatten", client_key, "awaiting_evidence", {}
                )

        row_after = self.ledger.peek(intent.intent_id, "flatten", client_key)
        was_indeterminate = row_after is not None and str(row_after["state"]) in {
            "unknown",
            "started",
        }
        try:
            order = self.client.query_order(build_query_order_payload(intent.symbol, client_key))
        except (MarginExchangeError, MarginTransportTimeout):
            return self._result(
                intent.intent_id, "protection_unknown", "flatten_unconfirmed", snapshot=snapshot
            )
        if (
            order.get("symbol") != intent.symbol
            or str(order.get("clientOrderId") or "") != client_key
            or order.get("side") != "BUY"
            or not _is_cross(order.get("isIsolated"))
        ):
            return self._result(
                intent.intent_id, "protection_unknown", "flatten_identity_mismatch", snapshot=snapshot
            )
        executed, _ = parse_decimal(order.get("executedQty"), "executedQty")
        if order.get("status") != "FILLED" or executed != quantity:
            if was_indeterminate and order.get("status") in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}:
                return self._result(
                    intent.intent_id, "protection_unknown", "flatten_unconfirmed", snapshot=snapshot
                )
            self.ledger.mark(intent.intent_id, "flatten", client_key, "rejected", {})
            rem = quantity - (executed or Decimal(0))
            return self._result(
                intent.intent_id,
                "protection_rejected_naked",
                reason,
                snapshot=snapshot,
                detail={
                    "unprotected_qty": api_amount(max(Decimal(0), rem)),
                    "flatten_executed_qty": api_amount(executed or Decimal(0)),
                    "manual_action": "cover_and_repay",
                },
            )
        net_flatten = self._net_base_from_order(
            intent.symbol, intent.base_asset, order, quantity
        )
        if net_flatten is None:
            return self._result(
                intent.intent_id, "fill_unconfirmed", "flatten_trades", snapshot=snapshot
            )
        self.ledger.mark(
            intent.intent_id,
            "flatten",
            client_key,
            "confirmed",
            {"executedQty": api_amount(quantity), "netBase": api_amount(net_flatten)},
        )
        repaid = self._repay_full_liability(
            intent, snapshot, contract, net_flatten, "flatten-repay"
        )
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

    def _baseline_base_qty(self, intent_id: str) -> Decimal | None:
        row = self.ledger.intent_row(intent_id)
        if row is None or row["baseline_base"] is None:
            return None
        val, err = parse_decimal(row["baseline_base"], "baseline_base")
        if err or val is None or val < 0:
            return None
        return val

    def _expected_open_borrowed(self, intent: Intent) -> Decimal | None:
        borrow_row = self.ledger.peek(
            intent.intent_id, "borrow", make_client_id(intent.intent_id, "borrow")
        )
        if borrow_row is None or str(borrow_row["state"]) != "confirmed":
            return Decimal(0)
        expected = intent.quantity
        for op_type, role in (("partial_repay", "partial-repay"), ("unwind_repay", "unwind-repay")):
            op_row = self.ledger.peek(intent.intent_id, op_type, make_client_id(intent.intent_id, role))
            if op_row is None or str(op_row["state"]) != "confirmed":
                continue
            recorded = _extract_op_post_borrowed(op_row)
            if recorded is not None:
                expected = recorded
            else:
                repaid_amt = _extract_op_amount(op_row)
                if repaid_amt is None:
                    return None
                expected = max(Decimal(0), expected - repaid_amt)
        for close_role in ("close-repay", "flatten-repay"):
            close_row = self.ledger.peek(
                intent.intent_id, "close_repay", make_client_id(intent.intent_id, close_role)
            )
            if close_row is not None and str(close_row["state"]) == "confirmed":
                return Decimal(0)
        return expected

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
        unwind_existing = self.ledger.peek(
            intent.intent_id, "unwind_repay", make_client_id(intent.intent_id, "unwind-repay")
        )
        baseline_base = self._baseline_base_qty(intent.intent_id)
        if baseline_base is None:
            return self._result(
                intent.intent_id,
                "residual_liability_after_unwind",
                "baseline_base_missing",
                account_flat=False,
                snapshot=snapshot,
            )
        if unwind_existing is None:
            expected_borrowed = self._expected_open_borrowed(intent)
            if expected_borrowed is None or base.borrowed != expected_borrowed:
                return self._result(
                    intent.intent_id,
                    "residual_liability_after_unwind",
                    "same_asset_external_liability_conflict",
                    account_flat=False,
                    snapshot=snapshot,
                    detail={
                        "expected_borrowed": api_amount(expected_borrowed or Decimal(0)),
                        "observed_borrowed": api_amount(base.borrowed),
                    },
                )
            own_avail = max(Decimal(0), base.free - baseline_base)
            amount = min(own_avail, expected_borrowed + base.interest)
        else:
            amount = _extract_op_amount(unwind_existing) or Decimal(0)
        if amount <= 0:
            flat = self._is_flat(intent)
            state = (
                "risk_dropped_unwound"
                if reason.startswith("margin_level_")
                else "entry_rejected_unwound"
            )
            if reason.startswith("emergency") or reason == "emergency_stop_blocks_new_risk":
                state = "emergency_unwound"
            return self._result(
                intent.intent_id,
                state if flat else "residual_liability_after_unwind",
                reason,
                account_flat=flat,
                snapshot=snapshot,
            )
        if unwind_existing is None and base.interest > 0:
            int_err = self._verify_attributed_interest(
                intent, expected_borrowed or Decimal(0), base.interest
            )
            if int_err:
                return self._result(
                    intent.intent_id,
                    "residual_liability_after_unwind",
                    int_err,
                    account_flat=False,
                    snapshot=snapshot,
                )
        repaid = self._repay_amount(
            intent, snapshot, contract, gate, "unwind_repay", "unwind-repay", amount
        )
        if isinstance(repaid, TrackResult):
            return repaid
        flat = self._is_flat(intent)
        if reason.startswith("margin_level_"):
            state = "risk_dropped_unwound" if flat else "residual_liability_after_unwind"
        elif "emergency" in reason:
            state = "emergency_unwound" if flat else "residual_liability_after_unwind"
        else:
            state = "entry_rejected_unwound" if flat else "residual_liability_after_unwind"
        return self._result(
            intent.intent_id, state, reason, account_flat=flat, snapshot=snapshot
        )

    def _repay_after_close(
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        gate: Gate,
        net_cover_base: Decimal,
    ) -> TrackResult:
        view = self._view()
        if isinstance(view, TrackResult):
            return view
        assets, _level = view
        if intent.action != "OPEN_SHORT":
            if any_liability(assets):
                return self._result(
                    intent.intent_id,
                    "not_flat",
                    "unexpected_liability",
                    account_flat=False,
                    snapshot=snapshot,
                )
            flat = self._is_flat(intent)
            residual = self._stored_lot_residual(intent.intent_id)
            detail = None
            if flat and residual is not None:
                detail = {"oco_lot_residual_qty": api_amount(residual)}
            return self._result(
                intent.intent_id,
                "clean_flat" if flat else "not_flat",
                "clean_flat" if flat else "not_flat",
                phase_ok=flat,
                account_flat=flat,
                snapshot=snapshot,
                detail=detail,
            )

        base = assets[intent.base_asset]
        baseline_base = self._baseline_base_qty(intent.intent_id)
        if baseline_base is None:
            return self._result(
                intent.intent_id,
                "not_flat",
                "baseline_base_missing",
                account_flat=False,
                snapshot=snapshot,
            )
        close_key = make_client_id(intent.intent_id, "close-repay")
        close_existing = self.ledger.peek(intent.intent_id, "close_repay", close_key)
        if close_existing is not None:
            prior_amt = _extract_op_amount(close_existing) or Decimal(0)
            if prior_amt > 0:
                repaid = self._repay_amount(
                    intent, snapshot, contract, gate, "close_repay", "close-repay", prior_amt
                )
                if isinstance(repaid, TrackResult):
                    return repaid
        else:
            expected_borrowed = self._expected_open_borrowed(intent)
            if expected_borrowed is None or base.borrowed != expected_borrowed:
                return self._result(
                    intent.intent_id,
                    "not_flat",
                    "same_asset_external_liability_conflict",
                    phase_ok=False,
                    account_flat=False,
                    snapshot=snapshot,
                    detail={
                        "expected_borrowed": api_amount(expected_borrowed or Decimal(0)),
                        "observed_borrowed": api_amount(base.borrowed),
                        "cover_net_base": api_amount(net_cover_base),
                        "manual_action": "reconcile_shared_asset_liability",
                    },
                )
            own_debt = expected_borrowed + base.interest
            intent_available_base = min(
                base.free,
                max(Decimal(0), base.free - baseline_base),
                net_cover_base,
            )
            if own_debt > 0:
                if intent_available_base < own_debt:
                    shortfall = own_debt - intent_available_base
                    return self._result(
                        intent.intent_id,
                        "not_flat",
                        "base_short_of_liability",
                        phase_ok=False,
                        account_flat=False,
                        snapshot=snapshot,
                        borrowed=api_amount(base.borrowed),
                        interest=api_amount(base.interest),
                        base_free=api_amount(base.free),
                        detail={
                            "liability_shortfall": api_amount(shortfall),
                            "manual_action": "top_up_base_and_repay",
                        },
                    )
                if base.interest > 0:
                    int_err = self._verify_attributed_interest(
                        intent, expected_borrowed, base.interest
                    )
                    if int_err:
                        return self._result(
                            intent.intent_id,
                            "not_flat",
                            int_err,
                            phase_ok=False,
                            account_flat=False,
                            snapshot=snapshot,
                            borrowed=api_amount(base.borrowed),
                            interest=api_amount(base.interest),
                            base_free=api_amount(base.free),
                            detail={
                                "observed_interest": api_amount(base.interest),
                                "manual_action": "reconcile_interest_history_and_repay",
                            },
                        )
                repaid = self._repay_amount(
                    intent, snapshot, contract, gate, "close_repay", "close-repay", own_debt
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
        self,
        intent: Intent,
        snapshot: Snapshot,
        contract: Contract,
        flatten_net_base: Decimal,
        role: str,
    ) -> TrackResult | None:
        view = self._view()
        if isinstance(view, TrackResult):
            return view
        assets, _level = view
        base = assets[intent.base_asset]
        baseline_base = self._baseline_base_qty(intent.intent_id)
        if baseline_base is None:
            return self._result(
                intent.intent_id,
                "not_flat",
                "baseline_base_missing",
                account_flat=False,
                snapshot=snapshot,
            )
        flat_key = make_client_id(intent.intent_id, role)
        flat_existing = self.ledger.peek(intent.intent_id, "close_repay", flat_key)
        if flat_existing is not None:
            prior_amt = _extract_op_amount(flat_existing) or Decimal(0)
            if prior_amt <= 0:
                return None
            dummy_gate = Gate(False, _now(self.ledger, intent.intent_id), OFFLINE_EXECUTION_MODE)
            return self._repay_amount(
                intent, snapshot, contract, dummy_gate, "close_repay", role, prior_amt
            )
        expected_borrowed = self._expected_open_borrowed(intent)
        if expected_borrowed is None or base.borrowed != expected_borrowed:
            return self._result(
                intent.intent_id,
                "not_flat",
                "same_asset_external_liability_conflict",
                account_flat=False,
                snapshot=snapshot,
                detail={
                    "expected_borrowed": api_amount(expected_borrowed or Decimal(0)),
                    "observed_borrowed": api_amount(base.borrowed),
                },
            )
        own_debt = expected_borrowed + base.interest
        if own_debt == 0:
            return None
        intent_available_base = min(
            base.free,
            max(Decimal(0), base.free - baseline_base),
            flatten_net_base,
        )
        if intent_available_base < own_debt:
            return self._result(
                intent.intent_id,
                "not_flat",
                "base_short_of_liability",
                account_flat=False,
                snapshot=snapshot,
                detail={
                    "liability_shortfall": api_amount(own_debt - intent_available_base),
                    "manual_action": "top_up_base_and_repay",
                },
            )
        if base.interest > 0:
            int_err = self._verify_attributed_interest(
                intent, expected_borrowed, base.interest
            )
            if int_err:
                return self._result(
                    intent.intent_id,
                    "not_flat",
                    int_err,
                    account_flat=False,
                    snapshot=snapshot,
                )
        dummy_gate = Gate(False, _now(self.ledger, intent.intent_id), OFFLINE_EXECUTION_MODE)
        return self._repay_amount(
            intent, snapshot, contract, dummy_gate, "close_repay", role, own_debt
        )

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
        del contract
        if amount <= 0:
            return None
        client_key = make_client_id(intent.intent_id, role)
        payload = build_repay_payload(intent.base_asset, amount)
        existing = self.ledger.peek(intent.intent_id, op_type, client_key)
        if existing is None:
            view_before = self._view()
            if isinstance(view_before, TrackResult):
                return view_before
            assets_before, _lvl_before = view_before
            base_before = assets_before.get(intent.base_asset)
            expected_before = self._expected_open_borrowed(intent)
            if (
                base_before is not None
                and expected_before is not None
                and base_before.borrowed != expected_before
            ):
                return self._result(
                    intent.intent_id,
                    "not_flat" if op_type == "close_repay" else "residual_liability_after_unwind",
                    "same_asset_external_liability_conflict",
                    account_flat=False,
                    snapshot=snapshot,
                    detail={
                        "expected_borrowed": api_amount(expected_before),
                        "observed_borrowed": api_amount(base_before.borrowed),
                    },
                )
            if base_before is None or base_before.free < amount:
                return self._result(
                    intent.intent_id,
                    "not_flat",
                    "base_short_of_liability",
                    snapshot=snapshot,
                )
            baseline = self._capture_transfer_baseline(
                "REPAY", intent.base_asset, gate.now_ms
            )
            stored_req = {**payload, "_meta": baseline}
            self.ledger.begin_operation(intent.intent_id, op_type, client_key, stored_req)
            try:
                response = self.client.repay(payload)
            except MarginTransportTimeout:
                self.ledger.mark(intent.intent_id, op_type, client_key, "unknown", None)
                if not self._bind_transfer("REPAY", intent.base_asset, amount, baseline):
                    return self._result(
                        intent.intent_id, "repay_unknown", "repay_unconfirmed", snapshot=snapshot
                    )
                self.ledger.mark(intent.intent_id, op_type, client_key, "confirmed", {"bound": True})
            except MarginExchangeError as exc:
                if exc.is_indeterminate:
                    self.ledger.mark(intent.intent_id, op_type, client_key, "unknown", None)
                    if not self._bind_transfer("REPAY", intent.base_asset, amount, baseline):
                        return self._result(
                            intent.intent_id, "repay_unknown", "repay_unconfirmed", snapshot=snapshot
                        )
                    self.ledger.mark(intent.intent_id, op_type, client_key, "confirmed", {"bound": True})
                elif exc.code == PENDING_TRANSACTION_CODE:
                    self.ledger.mark(
                        intent.intent_id,
                        op_type,
                        client_key,
                        "pending_lock",
                        {"code": exc.code},
                    )
                    return self._result(
                        intent.intent_id,
                        "repay_unknown",
                        "repay_pending_transaction_lock",
                        snapshot=snapshot,
                    )
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
            return self._result(
                intent.intent_id, "repay_rejected", "repay_rejected", snapshot=snapshot
            )
        elif str(existing["state"]) != "confirmed":
            op_meta = _extract_op_meta(existing)
            if not self._bind_transfer("REPAY", intent.base_asset, amount, op_meta):
                return self._result(
                    intent.intent_id, "repay_unknown", "repay_unconfirmed", snapshot=snapshot
                )
            self.ledger.mark(intent.intent_id, op_type, client_key, "confirmed", {"bound": True})
        view = self._view()
        if isinstance(view, TrackResult):
            return view
        assets, _level = view
        base_after = assets.get(intent.base_asset)
        if base_after is not None and op_type in {"partial_repay", "unwind_repay"}:
            self.ledger.mark(
                intent.intent_id,
                op_type,
                client_key,
                "confirmed",
                {"expected_borrowed_after": api_amount(base_after.borrowed)},
            )
        if liability(base_after) > 0 and op_type == "close_repay":
            return self._result(
                intent.intent_id,
                "repay_unconfirmed",
                "liability_remains_after_repay",
                snapshot=snapshot,
            )
        return None

    def _verify_attributed_interest(
        self,
        intent: Intent,
        expected_borrowed: Decimal,
        observed_interest: Decimal,
    ) -> str:
        if observed_interest <= 0:
            return ""
        borrow_row = self.ledger.peek(
            intent.intent_id, "borrow", make_client_id(intent.intent_id, "borrow")
        )
        if borrow_row is None or str(borrow_row["state"]) != "confirmed":
            return "unattributed_interest_liability_conflict"
        meta = _extract_op_meta(borrow_row)
        pre_bor_raw = meta.get("pre_borrowed")
        pre_int_raw = meta.get("pre_interest")
        if pre_bor_raw is None or pre_int_raw is None:
            return "preexisting_interest_liability_conflict"
        pre_bor, pb_err = parse_decimal(pre_bor_raw, "pre_borrowed")
        pre_int, pi_err = parse_decimal(pre_int_raw, "pre_interest")
        if pb_err or pi_err or pre_bor is None or pre_int is None or pre_bor > 0 or pre_int > 0:
            return "preexisting_interest_liability_conflict"
        if meta.get("ih_captured") is False:
            return "interest_attribution_query_failed"
        prior_int_ids = {str(x) for x in (meta.get("prior_interest_tx_ids") or [])}
        prior_tx_ids = {str(x) for x in (meta.get("prior_tx_ids") or [])}
        started_ms = meta.get("started_ms")
        start_query_ms = (
            max(0, int(started_ms) - 5000)
            if isinstance(started_ms, int) and started_ms > 0
            else None
        )
        if not hasattr(self.client, "query_interest_history"):
            return "interest_attribution_unsupported"
        try:
            br_hist = self.client.query_borrow_repay(
                build_borrow_query_payload(
                    intent.base_asset, "BORROW", start_time_ms=start_query_ms
                )
            )
        except (MarginExchangeError, MarginTransportTimeout):
            return "interest_attribution_query_failed"
        if isinstance(br_hist, Mapping) and isinstance(br_hist.get("rows"), list):
            non_prior_borrows = 0
            for br_item in br_hist["rows"]:
                if not isinstance(br_item, Mapping):
                    return "interest_attribution_query_failed"
                br_tx = str(br_item.get("txId") or "")
                if br_tx and br_tx in prior_tx_ids:
                    continue
                if str(br_item.get("asset") or "") != intent.base_asset:
                    continue
                if str(br_item.get("isolatedSymbol") or "").strip() != "":
                    return "same_asset_external_interest_conflict"
                br_amt, br_err = parse_decimal(br_item.get("amount"), "amount")
                if br_err or br_amt is None or br_amt != intent.quantity:
                    return "same_asset_external_interest_conflict"
                non_prior_borrows += 1
            if non_prior_borrows > 1:
                return "same_asset_external_interest_conflict"
        try:
            hist = self.client.query_interest_history(
                build_interest_history_payload(
                    intent.base_asset, start_time_ms=start_query_ms, size=100
                )
            )
        except (MarginExchangeError, MarginTransportTimeout):
            return "interest_attribution_query_failed"
        if not isinstance(hist, Mapping) or not isinstance(hist.get("rows"), list):
            return "interest_attribution_query_failed"
        rows_list = hist["rows"]
        total_raw = hist.get("total")
        if total_raw is not None:
            try:
                if int(total_raw) > len(rows_list):
                    return "interest_attribution_query_failed"
            except (TypeError, ValueError):
                return "interest_attribution_query_failed"
        part_row = self.ledger.peek(
            intent.intent_id, "partial_repay", make_client_id(intent.intent_id, "partial-repay")
        )
        part_confirmed = part_row is not None and str(part_row["state"]) == "confirmed"
        part_meta = _extract_op_meta(part_row) if part_confirmed else {}
        part_started_ms = part_meta.get("started_ms") if isinstance(part_meta.get("started_ms"), int) else None
        attributed_total = Decimal(0)
        seen_ih_ids: set[str] = set()
        on_borrow_count = 0
        for item in rows_list:
            if not isinstance(item, Mapping):
                return "interest_attribution_query_failed"
            tx_id = str(item.get("txId") or "").strip()
            if not tx_id:
                return "interest_attribution_query_failed"
            if tx_id in prior_int_ids:
                continue
            if tx_id in seen_ih_ids:
                return "interest_attribution_query_failed"
            seen_ih_ids.add(tx_id)
            if str(item.get("asset") or "") != intent.base_asset:
                continue
            if str(item.get("isolatedSymbol") or "").strip() != "":
                return "same_asset_external_interest_conflict"
            accrued_ms_raw = item.get("interestAccruedTime")
            if isinstance(started_ms, int) and started_ms > 0 and accrued_ms_raw is not None:
                try:
                    accrued_ms = int(accrued_ms_raw)
                except (TypeError, ValueError):
                    return "interest_attribution_query_failed"
                if accrued_ms < started_ms:
                    return "same_asset_external_interest_conflict"
            else:
                accrued_ms = None
            row_kind = str(item.get("type") or "").upper()
            if row_kind == "ON_BORROW":
                on_borrow_count += 1
                if on_borrow_count > 1:
                    return "same_asset_external_interest_conflict"
            prin, p_err = parse_decimal(item.get("principal"), "principal")
            accrued, i_err = parse_decimal(item.get("interest"), "interest")
            if p_err or i_err or prin is None or accrued is None or prin <= 0 or accrued < 0:
                return "interest_attribution_query_failed"
            if row_kind == "ON_BORROW":
                if prin != intent.quantity:
                    return "same_asset_external_interest_conflict"
            elif part_confirmed and part_started_ms is not None and accrued_ms is not None and accrued_ms > part_started_ms:
                if prin != expected_borrowed:
                    return "same_asset_external_interest_conflict"
            elif prin <= 0 or prin > intent.quantity or prin not in {intent.quantity, expected_borrowed}:
                return "same_asset_external_interest_conflict"
            attributed_total += accrued
        repaid_interest = Decimal(0)
        if part_confirmed:
            part_amt = _extract_op_amount(part_row) or Decimal(0)
            principal_delta = max(Decimal(0), intent.quantity - expected_borrowed)
            repaid_interest = max(Decimal(0), part_amt - principal_delta)
        expected_unpaid_interest = max(Decimal(0), attributed_total - repaid_interest)
        if expected_unpaid_interest != observed_interest:
            return "unattributed_interest_liability_conflict"
        return ""

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
        expected_borrowed = self._expected_open_borrowed(intent)
        if expected_borrowed is None or expected_borrowed <= 0:
            return None, "borrow_principal_unverified"
        if base.borrowed != expected_borrowed:
            return None, "same_asset_external_liability_conflict"
        if base.interest > 0:
            int_err = self._verify_attributed_interest(
                intent, expected_borrowed, base.interest
            )
            if int_err:
                return None, int_err
        rate = snapshot.hourly_interest_rate.get(contract.borrow_asset)
        if rate is None:
            return None, "asset_interest_rate_missing"
        return cover_buy_quantity(
            borrowed=expected_borrowed,
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
        except (MarginExchangeError, MarginTransportTimeout):
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
        except (MarginExchangeError, MarginTransportTimeout):
            return False
        if listed.get("listOrderStatus") != "EXECUTING":
            return False
        if listed.get("symbol") != intent.symbol:
            return False
        if str(listed.get("listClientOrderId") or "") != list_id:
            return False
        if not _is_cross(listed.get("isIsolated", False)):
            return False
        orders = listed.get("orders")
        if not isinstance(orders, list) or len(orders) != 2:
            return False
        expected_children = {
            make_client_id(intent.intent_id, "oco-limit"),
            make_client_id(intent.intent_id, "oco-stop"),
        }
        seen_children: set[str] = set()
        for item in orders:
            if not isinstance(item, Mapping):
                return False
            child_cid = str(item.get("clientOrderId") or "")
            if child_cid not in expected_children:
                return False
            seen_children.add(child_cid)
            try:
                order = self.client.query_order(
                    build_query_order_payload(intent.symbol, child_cid)
                )
            except (MarginExchangeError, MarginTransportTimeout):
                return False
            qty, problem = parse_decimal(order.get("origQty"), "origQty")
            if (
                problem
                or qty != quantity
                or order.get("symbol") != intent.symbol
                or str(order.get("clientOrderId") or "") != child_cid
                or order.get("side") != side
                or order.get("status") != "NEW"
                or not _is_cross(order.get("isIsolated"))
                or _unexpected_borrow(order)
            ):
                return False
        if seen_children != expected_children:
            return False
        return True

    def _net_base_from_order(
        self, symbol: str, base_asset: str, order: Mapping[str, Any], executed: Decimal
    ) -> Decimal | None:
        if self._trades_match(symbol, order, executed) is not True:
            return None
        try:
            trades = self.client.query_trades(
                build_query_trades_payload(symbol, order.get("orderId"))
            )
        except (MarginExchangeError, MarginTransportTimeout):
            return None
        base_fee = Decimal(0)
        for trade in trades:
            if str(trade.get("commissionAsset") or "") == base_asset:
                fee_val, fee_err = parse_decimal(trade.get("commission"), "commission")
                if fee_err or fee_val is None or fee_val < 0:
                    return None
                base_fee += fee_val
        return max(Decimal(0), executed - base_fee)

    def _filled_protection_leg(
        self, intent: Intent, snapshot: Snapshot, listed: Mapping[str, Any]
    ) -> TrackResult | Decimal:
        orders = listed.get("orders")
        if not isinstance(orders, list):
            return self._result(
                intent.intent_id, "exit_not_filled", "oco_orders_missing", snapshot=snapshot
            )
        filled = 0
        net_exit_base = Decimal(0)
        partial_exec = Decimal(0)
        expected_side = "BUY" if intent.action == "OPEN_SHORT" else "SELL"
        for item in orders:
            if not isinstance(item, Mapping):
                return self._result(
                    intent.intent_id, "exit_not_filled", "oco_orders_missing", snapshot=snapshot
                )
            try:
                order = self.client.query_order(
                    build_query_order_payload(intent.symbol, str(item.get("clientOrderId")))
                )
            except (MarginExchangeError, MarginTransportTimeout):
                return self._result(
                    intent.intent_id,
                    "fill_unconfirmed",
                    "protection_order_query_failed",
                    snapshot=snapshot,
                )
            expected_cid = str(item.get("clientOrderId") or "")
            if (
                order.get("symbol") != intent.symbol
                or str(order.get("clientOrderId") or "") != expected_cid
                or not _is_cross(order.get("isIsolated"))
                or _unexpected_borrow(order)
            ):
                return self._result(
                    intent.intent_id,
                    "fill_mismatch",
                    "protection_order_identity_mismatch",
                    snapshot=snapshot,
                )
            if order.get("side") != expected_side:
                return self._result(
                    intent.intent_id,
                    "fill_mismatch",
                    "protection_side_mismatch",
                    snapshot=snapshot,
                )
            exec_qty, _ = parse_decimal(order.get("executedQty", "0"), "executedQty")
            if order.get("status") == "FILLED":
                if exec_qty is None:
                    return self._result(
                        intent.intent_id,
                        "fill_unconfirmed",
                        "protection_trades",
                        snapshot=snapshot,
                    )
                leg_net = self._net_base_from_order(
                    intent.symbol, intent.base_asset, order, exec_qty
                )
                if leg_net is None:
                    return self._result(
                        intent.intent_id,
                        "fill_unconfirmed",
                        "protection_trades",
                        snapshot=snapshot,
                    )
                net_exit_base = leg_net
                filled += 1
            elif exec_qty is not None and exec_qty > 0:
                partial_exec += exec_qty
        if filled != 1:
            if filled == 0 and partial_exec > 0:
                return self._result(
                    intent.intent_id,
                    "exit_partially_filled_halted",
                    "protection_partial_fill_manual_review",
                    snapshot=snapshot,
                    detail={
                        "partial_exit_qty": api_amount(partial_exec),
                        "manual_action": "reconcile_partial_oco_and_repay",
                    },
                )
            return self._result(
                intent.intent_id,
                "fill_mismatch",
                "protection_leg_conflict",
                snapshot=snapshot,
            )
        return net_exit_base

    def _trades_match(
        self, symbol: str, order: Mapping[str, Any], executed: Decimal
    ) -> bool | None:
        try:
            trades = self.client.query_trades(
                build_query_trades_payload(symbol, order.get("orderId"))
            )
        except (MarginExchangeError, MarginTransportTimeout):
            return None
        if not trades:
            return None
        expected_oid = str(order.get("orderId") or "")
        total = Decimal(0)
        for trade in trades:
            if str(trade.get("symbol") or symbol) != symbol:
                return False
            if expected_oid and str(trade.get("orderId") or "") != expected_oid:
                return False
            if not _is_cross(trade.get("isIsolated", False)):
                return False
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

    def _bind_transfer(
        self,
        kind: str,
        asset: str,
        amount: Decimal,
        op_meta: Mapping[str, Any] | None = None,
    ) -> bool:
        meta = dict(op_meta or {})
        started_ms = meta.get("started_ms")
        start_query_ms = (
            max(0, int(started_ms) - 5000)
            if isinstance(started_ms, int) and started_ms > 0
            else None
        )
        prior_ids = {str(x) for x in (meta.get("prior_tx_ids") or [])}
        try:
            body = self.client.query_borrow_repay(
                build_borrow_query_payload(asset, kind, start_time_ms=start_query_ms)
            )
        except (MarginExchangeError, MarginTransportTimeout):
            return False
        matches = []
        for row in body.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            tx_id = str(row.get("txId") or "")
            if tx_id and tx_id in prior_ids:
                continue
            if str(row.get("isolatedSymbol") or "").strip() != "":
                continue
            if row.get("asset") != asset:
                continue
            # Real Binance returns type="MANUAL" (or "AUTO"); fixture returns "BORROW"/"REPAY".
            row_type = str(row.get("type") or "")
            if row_type not in {kind, "MANUAL"}:
                continue
            status = str(row.get("status") or "")
            if status == "PENDING":
                return False
            if status != "CONFIRMED":
                continue
            number, problem = parse_decimal(row.get("amount"), "amount")
            if problem or number != amount:
                continue
            matches.append(row)
        if len(matches) != 1:
            return False

        # Cross-verify live account liability delta against pre-operation baseline.
        view = self._view()
        if isinstance(view, TrackResult):
            return False
        assets, _level = view
        current = assets.get(asset)
        if current is None:
            return False
        pre_borrowed, pb_err = parse_decimal(meta.get("pre_borrowed"), "pre_borrowed")
        pre_interest, pi_err = parse_decimal(meta.get("pre_interest"), "pre_interest")
        if kind == "BORROW" and not pb_err and pre_borrowed is not None:
            if current.borrowed < pre_borrowed + amount:
                return False
        if kind == "REPAY" and not pb_err and not pi_err and pre_borrowed is not None and pre_interest is not None:
            pre_liab = pre_borrowed + pre_interest
            cur_liab = current.borrowed + current.interest
            if pre_liab > 0 and cur_liab >= pre_liab:
                return False
        return True

    def _new_risk_block(self, contract: Contract) -> str:
        view = self._view()
        if isinstance(view, TrackResult):
            return view.reason
        _assets, level = view
        try:
            account = self.client.query_account()
        except (MarginExchangeError, MarginTransportTimeout):
            return "account_read_failed"
        if account.get("tradeEnabled") is not True or account.get("borrowEnabled") is not True:
            return "account_flags_closed"
        if not str(account.get("accountType") or "").startswith("MARGIN"):
            return "account_type_not_cross_margin"
        if "ISOLATED" in str(account.get("accountType") or "").upper():
            return "isolated_account_rejected"
        status_txt = str(account.get("marginLevelStatus") or "EXCESSIVE").upper()
        if status_txt in EMERGENCY_MARGIN_LEVEL_STATUSES:
            return "margin_level_emergency_floor"
        if contract.parameters_approved and contract.emergency_margin_level is not None and level <= contract.emergency_margin_level:
            return "margin_level_emergency_floor"
        if level < contract.min_margin_level:
            return "margin_level_below_contract"
        return ""

    def _match_fresh_account(self, snapshot: Snapshot, contract: Contract, intent: Intent) -> str:
        if getattr(self.client, "account_uid", None) != snapshot.uid:
            return "uid_not_bound_on_client"
        if getattr(self.client, "margin_mode", None) != "CROSS":
            return "client_mode_not_cross"
        # Only a short borrows the base. Re-reading max borrow for a cash long
        # repeats the unused-asset -3045 and refuses the open before the order.
        borrows_base = intent.action == "OPEN_SHORT"
        try:
            account = self.client.query_account()
            borrow: Mapping[str, Any] | None = None
            rates: list[Mapping[str, Any]] | None = None
            if borrows_base:
                borrow = self.client.query_max_borrow(build_max_borrow_payload(contract.borrow_asset))
                rates = self.client.query_hourly_interest(
                    build_hourly_interest_payload(contract.borrow_asset)
                )
        except (MarginExchangeError, MarginTransportTimeout):
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
        for asset in intent_assets(snapshot):
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
        if not borrows_base:
            return ""
        amount, amount_problem = parse_decimal(
            None if borrow is None else borrow.get("amount"), "amount"
        )
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
        except (MarginExchangeError, MarginTransportTimeout):
            return self._result("", "locked", "account_read_failed")
        assets = _wire_assets(account)
        if isinstance(assets, str):
            return self._result("", "locked", assets)
        level, problem = parse_decimal(account.get("marginLevel"), "marginLevel")
        if problem or level is None:
            return self._result("", "locked", "margin_level_invalid")
        return assets, level

    def _stored_step(self, intent_id: str) -> Decimal | None:
        row = self.ledger.intent_row(intent_id)
        if row is None:
            return None
        try:
            stored = json.loads(str(row["payload_json"]))
            step, err = parse_decimal(
                str(stored.get("snapshot", {}).get("filters", {}).get("step_size")),
                "step_size",
            )
        except (TypeError, ValueError, KeyError):
            return None
        if err or step is None or step <= 0:
            return None
        return step

    def _stored_lot_residual(self, intent_id: str) -> Decimal | None:
        client_key = make_client_id(intent_id, "oco-list")
        row = self.ledger.peek(intent_id, "oco", client_key)
        if row is None:
            return None
        try:
            request = json.loads(str(row["request_json"]))
        except (TypeError, ValueError):
            return None
        raw = request.get("oco_lot_residual_qty")
        if raw in (None, ""):
            return Decimal(0)
        residual, err = parse_decimal(str(raw), "oco_lot_residual_qty")
        if err or residual is None or residual < 0:
            return None
        return residual

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
        if base is None:
            return False
        gap = base.free - baseline
        if gap < 0:
            return False
        if gap > 0:
            step = self._stored_step(intent.intent_id)
            # A remainder equal to one step is still a lot, not dust.
            if step is None or gap >= step:
                return False
        try:
            open_orders = self.client.query_open_orders(
                build_query_open_orders_payload(intent.symbol)
            )
        except (MarginExchangeError, MarginTransportTimeout):
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
        live_blocked = not (self._network_client and self._allow_live_network)
        return TrackResult(
            intent_id=intent_id,
            state=state,
            phase_ok=phase_ok,
            account_flat=account_flat,
            protection_confirmed=protection_confirmed,
            live_writes_blocked=live_blocked,
            reason=reason,
            borrowed=borrowed,
            interest=interest,
            base_free=base_free,
            detail={} if detail is None else detail,
        )


def _extract_op_amount(row: Any) -> Decimal | None:
    if row is None:
        return None
    try:
        parsed = json.loads(str(row["request_json"]))
    except (TypeError, ValueError, KeyError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    val, err = parse_decimal(parsed.get("amount"), "amount")
    return None if err or val is None or val < 0 else val


def _extract_op_post_borrowed(row: Any) -> Decimal | None:
    if row is None or not row["response_json"]:
        return None
    try:
        parsed = json.loads(str(row["response_json"]))
    except (TypeError, ValueError, KeyError):
        return None
    if not isinstance(parsed, Mapping) or "expected_borrowed_after" not in parsed:
        return None
    val, err = parse_decimal(parsed.get("expected_borrowed_after"), "expected_borrowed_after")
    return None if err or val is None or val < 0 else val


def _extract_op_meta(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        parsed = json.loads(str(row["request_json"]))
    except (TypeError, ValueError, KeyError):
        return {}
    if isinstance(parsed, Mapping) and isinstance(parsed.get("_meta"), Mapping):
        return dict(parsed["_meta"])
    return {}


def _stored_oco_intent(intent: Intent, row: Any) -> Intent:
    try:
        parsed = json.loads(str(row["request_json"]))
    except (TypeError, ValueError, KeyError):
        return intent
    if not isinstance(parsed, Mapping):
        return intent
    stored_tp, err = parse_decimal(parsed.get("price"), "price")
    if err or stored_tp is None or stored_tp <= 0:
        return intent
    return replace(intent, take_profit_price=stored_tp)


def _unknown_state(state: str) -> bool:
    return state.endswith("_unknown") or state in {"fill_unconfirmed"}


def _is_cross(value: object) -> bool:
    return value in (False, "FALSE", "false")


def _floor_lot(quantity: Decimal, step: Decimal) -> tuple[Decimal, Decimal]:
    """Floor to the lot step. The residual is what remains below one step."""
    if step <= 0:
        return quantity, Decimal(0)
    floored = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
    return floored, quantity - floored


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
