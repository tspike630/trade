"""Explicit cross-margin risk contract.

Missing, non-finite, stale, or mismatched inputs fail closed. No field has an
approved default. ``parameters_approved`` must be false; a true flag is rejected
rather than treated as permission to trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from execution.margin_cross.money import on_grid, parse_decimal

PURPOSE_CROSS_MARGIN_TRACK = "cross_margin_track_v1"
OFFLINE_FIXTURE_STRATEGY_ID = "FIXTURE_UNAPPROVED_NOT_A_STRATEGY"
REQUIRED_UNAPPROVED_LABEL = "NOT_APPROVED_FIXTURE"
OFFLINE_EXECUTION_MODE = "offline_fixture"


@dataclass(frozen=True)
class Filters:
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class AssetRow:
    asset: str
    free: Decimal
    locked: Decimal
    borrowed: Decimal
    interest: Decimal


@dataclass(frozen=True)
class Snapshot:
    uid: str
    margin_mode: str
    account_type: str
    created: bool
    trade_enabled: bool
    borrow_enabled: bool
    margin_level: Decimal
    account_max_leverage: Decimal
    observed_at_ms: int
    assets: dict[str, AssetRow]
    max_borrowable: dict[str, Decimal]
    borrow_limit: dict[str, Decimal]
    hourly_interest_rate: dict[str, Decimal]
    bid_qty: Decimal
    ask_qty: Decimal
    reference_price: Decimal
    filters: Filters
    fee_rate: Decimal
    fee_asset: str


@dataclass(frozen=True)
class Contract:
    account_uid: str
    mode: str
    symbol: str
    base_asset: str
    quote_asset: str
    borrow_asset: str
    parameters_approved: bool
    unapproved_label: str
    max_leverage: Decimal
    min_margin_level: Decimal
    max_borrow_quantity: Decimal
    max_snapshot_age_ms: int
    interest_horizon_hours: Decimal
    max_base_over_liability: Decimal
    strategy_id: str
    borrow_step: Decimal


@dataclass(frozen=True)
class Intent:
    intent_id: str
    purpose: str
    strategy_id: str
    account_uid: str
    mode: str
    action: str
    symbol: str
    base_asset: str
    quote_asset: str
    quantity: Decimal
    limit_price: Decimal
    take_profit_price: Decimal
    stop_price: Decimal
    stop_limit_price: Decimal

    def canonical(self) -> str:
        parts = [
            self.intent_id,
            self.purpose,
            self.strategy_id,
            self.account_uid,
            self.mode,
            self.action,
            self.symbol,
            self.base_asset,
            self.quote_asset,
            format(self.quantity, "f"),
            format(self.limit_price, "f"),
            format(self.take_profit_price, "f"),
            format(self.stop_price, "f"),
            format(self.stop_limit_price, "f"),
        ]
        return "|".join(parts)


@dataclass(frozen=True)
class Gate:
    emergency_stop: bool
    now_ms: int
    execution_mode: str


def _text(raw: Mapping[str, object], field: str, reasons: list[str]) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or value.strip() == "":
        reasons.append(f"{field}_invalid")
        return ""
    return value.strip()


def _bool(raw: Mapping[str, object], field: str, reasons: list[str]) -> bool:
    value = raw.get(field)
    if not isinstance(value, bool):
        reasons.append(f"{field}_invalid")
        return False
    return value


def _whole_ms(raw: Mapping[str, object], field: str, reasons: list[str]) -> int:
    number, problem = parse_decimal(raw.get(field), field)
    if problem or number is None or number != number.to_integral_value() or number < 0:
        reasons.append(f"{field}_invalid")
        return 0
    return int(number)


def _dec(
    raw: Mapping[str, object], field: str, reasons: list[str], *, positive: bool = False
) -> Decimal:
    number, problem = parse_decimal(raw.get(field), field)
    if problem or number is None:
        reasons.append(problem or f"{field}_invalid")
        return Decimal(0)
    if positive and number <= 0:
        reasons.append(f"{field}_not_positive")
    return number


def _dec_map(
    raw: Mapping[str, object], field: str, reasons: list[str]
) -> dict[str, Decimal]:
    value = raw.get(field)
    parsed: dict[str, Decimal] = {}
    if not isinstance(value, Mapping) or not value:
        reasons.append(f"{field}_invalid")
        return parsed
    for key, item in value.items():
        if not isinstance(key, str) or key.strip() == "":
            reasons.append(f"{field}_invalid")
            continue
        number, problem = parse_decimal(item, f"{field}.{key}")
        if problem or number is None or number < 0:
            reasons.append(problem or f"{field}_invalid")
            continue
        parsed[key] = number
    return parsed


def parse_filters(raw: Mapping[str, object], reasons: list[str]) -> Filters:
    return Filters(
        tick_size=_dec(raw, "tick_size", reasons, positive=True),
        step_size=_dec(raw, "step_size", reasons, positive=True),
        min_qty=_dec(raw, "min_qty", reasons, positive=True),
        min_notional=_dec(raw, "min_notional", reasons, positive=True),
    )


def _assets(raw: Mapping[str, object], reasons: list[str]) -> dict[str, AssetRow]:
    value = raw.get("assets")
    parsed: dict[str, AssetRow] = {}
    if not isinstance(value, list) or not value:
        reasons.append("assets_invalid")
        return parsed
    for item in value:
        if not isinstance(item, Mapping):
            reasons.append("assets_invalid")
            continue
        asset = item.get("asset")
        if not isinstance(asset, str) or asset.strip() == "":
            reasons.append("assets_invalid")
            continue
        free, free_problem = parse_decimal(item.get("free"), "free")
        locked, locked_problem = parse_decimal(item.get("locked"), "locked")
        borrowed, borrowed_problem = parse_decimal(item.get("borrowed"), "borrowed")
        interest, interest_problem = parse_decimal(item.get("interest"), "interest")
        if (
            free_problem
            or locked_problem
            or borrowed_problem
            or interest_problem
            or free is None
            or locked is None
            or borrowed is None
            or interest is None
            or min(free, locked, borrowed, interest) < 0
        ):
            reasons.append(f"asset_{asset}_invalid")
            continue
        parsed[asset] = AssetRow(asset, free, locked, borrowed, interest)
    return parsed


def parse_snapshot(raw: Mapping[str, object]) -> tuple[Snapshot | None, list[str]]:
    reasons: list[str] = []
    filters_raw = raw.get("filters")
    filters = parse_filters(filters_raw if isinstance(filters_raw, Mapping) else {}, reasons)
    snapshot = Snapshot(
        uid=_text(raw, "uid", reasons),
        margin_mode=_text(raw, "margin_mode", reasons),
        account_type=_text(raw, "account_type", reasons),
        created=_bool(raw, "created", reasons),
        trade_enabled=_bool(raw, "trade_enabled", reasons),
        borrow_enabled=_bool(raw, "borrow_enabled", reasons),
        margin_level=_dec(raw, "margin_level", reasons, positive=True),
        account_max_leverage=_dec(raw, "account_max_leverage", reasons, positive=True),
        observed_at_ms=_whole_ms(raw, "observed_at_ms", reasons),
        assets=_assets(raw, reasons),
        max_borrowable=_dec_map(raw, "max_borrowable", reasons),
        borrow_limit=_dec_map(raw, "borrow_limit", reasons),
        hourly_interest_rate=_dec_map(raw, "hourly_interest_rate", reasons),
        bid_qty=_dec(raw, "bid_qty", reasons, positive=True),
        ask_qty=_dec(raw, "ask_qty", reasons, positive=True),
        reference_price=_dec(raw, "reference_price", reasons, positive=True),
        filters=filters,
        fee_rate=_dec(raw, "fee_rate", reasons),
        fee_asset=_text(raw, "fee_asset", reasons),
    )
    if snapshot.fee_rate < 0 or snapshot.fee_rate >= 1:
        reasons.append("fee_rate_invalid")
    return (None, reasons) if reasons else (snapshot, [])


def parse_contract(raw: Mapping[str, object]) -> tuple[Contract | None, list[str]]:
    reasons: list[str] = []
    approved = raw.get("parameters_approved")
    if approved is not False:
        reasons.append("parameters_approved_must_be_false")
    contract = Contract(
        account_uid=_text(raw, "account_uid", reasons),
        mode=_text(raw, "mode", reasons),
        symbol=_text(raw, "symbol", reasons),
        base_asset=_text(raw, "base_asset", reasons),
        quote_asset=_text(raw, "quote_asset", reasons),
        borrow_asset=_text(raw, "borrow_asset", reasons),
        parameters_approved=False,
        unapproved_label=_text(raw, "unapproved_label", reasons),
        max_leverage=_dec(raw, "max_leverage", reasons, positive=True),
        min_margin_level=_dec(raw, "min_margin_level", reasons, positive=True),
        max_borrow_quantity=_dec(raw, "max_borrow_quantity", reasons, positive=True),
        max_snapshot_age_ms=_whole_ms(raw, "max_snapshot_age_ms", reasons),
        interest_horizon_hours=_dec(raw, "interest_horizon_hours", reasons),
        max_base_over_liability=_dec(raw, "max_base_over_liability", reasons),
        strategy_id=_text(raw, "strategy_id", reasons),
        borrow_step=_dec(raw, "borrow_step", reasons, positive=True),
    )
    if contract.interest_horizon_hours < 0:
        reasons.append("interest_horizon_hours_invalid")
    if contract.max_base_over_liability < 0:
        reasons.append("max_base_over_liability_invalid")
    if contract.max_snapshot_age_ms <= 0:
        reasons.append("max_snapshot_age_ms_invalid")
    return (None, reasons) if reasons else (contract, [])


def parse_intent(raw: Mapping[str, object]) -> tuple[Intent | None, list[str]]:
    reasons: list[str] = []
    intent = Intent(
        intent_id=_text(raw, "intent_id", reasons),
        purpose=_text(raw, "purpose", reasons),
        strategy_id=_text(raw, "strategy_id", reasons),
        account_uid=_text(raw, "account_uid", reasons),
        mode=_text(raw, "mode", reasons),
        action=_text(raw, "action", reasons),
        symbol=_text(raw, "symbol", reasons),
        base_asset=_text(raw, "base_asset", reasons),
        quote_asset=_text(raw, "quote_asset", reasons),
        quantity=_dec(raw, "quantity", reasons, positive=True),
        limit_price=_dec(raw, "limit_price", reasons, positive=True),
        take_profit_price=_dec(raw, "take_profit_price", reasons, positive=True),
        stop_price=_dec(raw, "stop_price", reasons, positive=True),
        stop_limit_price=_dec(raw, "stop_limit_price", reasons, positive=True),
    )
    if intent.action not in {"OPEN_SHORT", "OPEN_LONG"}:
        reasons.append("action_invalid")
    return (None, reasons) if reasons else (intent, [])


def parse_gate(raw: Mapping[str, object]) -> tuple[Gate | None, list[str]]:
    reasons: list[str] = []
    if not isinstance(raw.get("emergency_stop"), bool):
        reasons.append("emergency_stop_invalid")
    gate = Gate(
        emergency_stop=raw.get("emergency_stop") is True,
        now_ms=_whole_ms(raw, "now_ms", reasons),
        execution_mode=_text(raw, "execution_mode", reasons),
    )
    return (None, reasons) if reasons else (gate, [])


def contract_identity_reasons(
    intent: Intent, contract: Contract, snapshot: Snapshot, gate: Gate
) -> list[str]:
    reasons: list[str] = []
    if gate.execution_mode != OFFLINE_EXECUTION_MODE:
        reasons.append("live_network_disabled")
    if intent.purpose != PURPOSE_CROSS_MARGIN_TRACK:
        reasons.append("purpose_not_cross_margin_track")
    if (
        intent.strategy_id != OFFLINE_FIXTURE_STRATEGY_ID
        or contract.strategy_id != OFFLINE_FIXTURE_STRATEGY_ID
    ):
        reasons.append("strategy_not_in_offline_fixture_allowlist")
    if contract.unapproved_label != REQUIRED_UNAPPROVED_LABEL:
        reasons.append("unapproved_label_required")
    if contract.parameters_approved is not False:
        reasons.append("parameters_approved_must_be_false")
    uids = {intent.account_uid, contract.account_uid, snapshot.uid}
    if len(uids) != 1:
        reasons.append("uid_mismatch")
    if intent.mode != "CROSS" or contract.mode != "CROSS" or snapshot.margin_mode != "CROSS":
        reasons.append("mode_not_cross")
    if "ISOLATED" in snapshot.account_type.upper():
        reasons.append("isolated_account_rejected")
    if not snapshot.account_type.startswith("MARGIN"):
        reasons.append("account_type_not_cross_margin")
    symbols = {intent.symbol, contract.symbol}
    if len(symbols) != 1:
        reasons.append("symbol_mismatch")
    if intent.base_asset != contract.base_asset or intent.quote_asset != contract.quote_asset:
        reasons.append("asset_mismatch")
    if intent.action == "OPEN_SHORT" and contract.borrow_asset != intent.base_asset:
        reasons.append("short_borrow_asset_must_be_base")
    if not (snapshot.created and snapshot.trade_enabled and snapshot.borrow_enabled):
        reasons.append("account_flags_closed")
    if snapshot.account_max_leverage > contract.max_leverage:
        reasons.append("account_leverage_above_contract")
    age = gate.now_ms - snapshot.observed_at_ms
    if age < 0 or age > contract.max_snapshot_age_ms:
        reasons.append("snapshot_stale_or_future")
    filters = snapshot.filters
    if not on_grid(intent.quantity, filters.step_size):
        reasons.append("quantity_off_step")
    if intent.quantity < filters.min_qty:
        reasons.append("quantity_below_min")
    for name, price in (
        ("limit_price", intent.limit_price),
        ("take_profit_price", intent.take_profit_price),
        ("stop_price", intent.stop_price),
        ("stop_limit_price", intent.stop_limit_price),
        ("reference_price", snapshot.reference_price),
    ):
        if not on_grid(price, filters.tick_size):
            reasons.append(f"{name}_off_tick")
    if intent.quantity * intent.limit_price < filters.min_notional:
        reasons.append("entry_notional_below_min")
    reference = snapshot.reference_price
    if intent.action == "OPEN_SHORT":
        if not (
            intent.take_profit_price < reference < intent.stop_price
            and intent.stop_limit_price >= intent.stop_price
        ):
            reasons.append("short_protection_price_geometry")
        if intent.quantity > snapshot.bid_qty:
            reasons.append("sell_liquidity_insufficient")
        borrowable = snapshot.max_borrowable.get(contract.borrow_asset)
        if borrowable is None:
            reasons.append("asset_max_borrowable_missing")
        elif intent.quantity > borrowable:
            reasons.append("asset_borrow_insufficient")
        if intent.quantity > contract.max_borrow_quantity:
            reasons.append("contract_borrow_quantity_exceeded")
        if not on_grid(intent.quantity, contract.borrow_step):
            reasons.append("borrow_off_step")
        if contract.borrow_asset not in snapshot.hourly_interest_rate:
            reasons.append("asset_interest_rate_missing")
    elif intent.action == "OPEN_LONG":
        if not (
            intent.take_profit_price > reference > intent.stop_price
            and intent.stop_limit_price <= intent.stop_price
        ):
            reasons.append("long_protection_price_geometry")
        if intent.quantity > snapshot.ask_qty:
            reasons.append("buy_liquidity_insufficient")
    if snapshot.margin_level < contract.min_margin_level:
        reasons.append("margin_level_below_contract")
    if contract.borrow_asset not in snapshot.assets or intent.quote_asset not in snapshot.assets:
        reasons.append("snapshot_asset_row_missing")
    if snapshot.fee_asset not in {intent.base_asset, intent.quote_asset}:
        reasons.append("fee_asset_unmodeled")
    return reasons
