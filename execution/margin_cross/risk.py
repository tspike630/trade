"""Explicit cross-margin risk contract.

Missing, non-finite, stale, or mismatched inputs fail closed.
By default ``CROSS_MARGIN_PARAMETERS_APPROVED`` is off, so ``parameters_approved``
must be false and only offline fixture verification can run. When operator
approval is explicitly enabled via ``CROSS_MARGIN_PARAMETERS_APPROVED=1``,
``parameters_approved=True`` is validated against the ruled risk ceilings in
``execution.margin_cross.policy``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from execution.margin_cross.money import on_grid, parse_decimal
from execution.margin_cross.policy import (
    ALLOWED_MARGIN_LEVEL_STATUSES,
    APPROVED_CONTRACT_LABEL,
    APPROVED_CROSS_MARGIN_STRATEGIES,
    APPROVED_DRY_RUN_MODE,
    EMERGENCY_MARGIN_LEVEL,
    EMERGENCY_MARGIN_LEVEL_STATUSES,
    LIVE_EXECUTION_MODE,
    MAX_ACCOUNT_LEVERAGE,
    MAX_ALLOWED_INTEREST_HORIZON_HOURS,
    MAX_ALLOWED_SNAPSHOT_AGE_MS,
    MAX_BORROWABLE_POOL_RATIO,
    MAX_EFFECTIVE_LEVERAGE,
    MAX_TOTAL_LIABILITY_NET_RATIO,
    MCP_FORBIDDEN_UID,
    MIN_ENTRY_MARGIN_LEVEL,
    MIN_ORDER_NOTIONAL_USDT,
    WARN_MARGIN_LEVEL,
    live_enabled_by_env,
    parameters_approved_by_env,
)

PURPOSE_CROSS_MARGIN_TRACK = "cross_margin_track_v1"
OFFLINE_FIXTURE_STRATEGY_ID = "FIXTURE_UNAPPROVED_NOT_A_STRATEGY"
REQUIRED_UNAPPROVED_LABEL = "NOT_APPROVED_FIXTURE"
OFFLINE_EXECUTION_MODE = "offline_fixture"

_ALL_MARGIN_STATUSES = ALLOWED_MARGIN_LEVEL_STATUSES | EMERGENCY_MARGIN_LEVEL_STATUSES


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
    margin_level_status: str = "EXCESSIVE"


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
    max_effective_leverage: Decimal = MAX_EFFECTIVE_LEVERAGE
    warn_margin_level: Decimal = WARN_MARGIN_LEVEL
    emergency_margin_level: Decimal = EMERGENCY_MARGIN_LEVEL
    formula_b_enabled: bool = False


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


def _opt_dec(
    raw: Mapping[str, object],
    field: str,
    default: Decimal,
    reasons: list[str],
    *,
    positive: bool = True,
) -> Decimal:
    if field not in raw or raw.get(field) is None:
        return default
    return _dec(raw, field, reasons, positive=positive)


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
    status_raw = raw.get("margin_level_status", "EXCESSIVE")
    if not isinstance(status_raw, str) or status_raw.strip().upper() not in _ALL_MARGIN_STATUSES:
        reasons.append("margin_level_status_invalid")
        margin_status = "EXCESSIVE"
    else:
        margin_status = status_raw.strip().upper()
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
        margin_level_status=margin_status,
    )
    if snapshot.fee_rate < 0 or snapshot.fee_rate >= 1:
        reasons.append("fee_rate_invalid")
    return (None, reasons) if reasons else (snapshot, [])


def parse_contract(raw: Mapping[str, object]) -> tuple[Contract | None, list[str]]:
    reasons: list[str] = []
    approved_raw = raw.get("parameters_approved")
    if not isinstance(approved_raw, bool):
        reasons.append("parameters_approved_must_be_false")
        approved_flag = False
    elif approved_raw is True and not parameters_approved_by_env():
        reasons.append("parameters_approved_must_be_false")
        approved_flag = False
    else:
        approved_flag = approved_raw

    formula_b_raw = raw.get("formula_b_enabled", False)
    if not isinstance(formula_b_raw, bool):
        reasons.append("formula_b_enabled_invalid")
        formula_b = False
    else:
        formula_b = formula_b_raw

    contract = Contract(
        account_uid=_text(raw, "account_uid", reasons),
        mode=_text(raw, "mode", reasons),
        symbol=_text(raw, "symbol", reasons),
        base_asset=_text(raw, "base_asset", reasons),
        quote_asset=_text(raw, "quote_asset", reasons),
        borrow_asset=_text(raw, "borrow_asset", reasons),
        parameters_approved=approved_flag,
        unapproved_label=_text(raw, "unapproved_label", reasons),
        max_leverage=_dec(raw, "max_leverage", reasons, positive=True),
        min_margin_level=_dec(raw, "min_margin_level", reasons, positive=True),
        max_borrow_quantity=_dec(raw, "max_borrow_quantity", reasons, positive=True),
        max_snapshot_age_ms=_whole_ms(raw, "max_snapshot_age_ms", reasons),
        interest_horizon_hours=_dec(raw, "interest_horizon_hours", reasons),
        max_base_over_liability=_dec(raw, "max_base_over_liability", reasons),
        strategy_id=_text(raw, "strategy_id", reasons),
        borrow_step=_dec(raw, "borrow_step", reasons, positive=True),
        max_effective_leverage=_opt_dec(
            raw, "max_effective_leverage", MAX_EFFECTIVE_LEVERAGE, reasons
        ),
        warn_margin_level=_opt_dec(raw, "warn_margin_level", WARN_MARGIN_LEVEL, reasons),
        emergency_margin_level=_opt_dec(
            raw, "emergency_margin_level", EMERGENCY_MARGIN_LEVEL, reasons
        ),
        formula_b_enabled=formula_b,
    )
    if contract.interest_horizon_hours < 0:
        reasons.append("interest_horizon_hours_invalid")
    if contract.max_base_over_liability < 0:
        reasons.append("max_base_over_liability_invalid")
    if contract.max_snapshot_age_ms <= 0:
        reasons.append("max_snapshot_age_ms_invalid")
    if approved_flag:
        if contract.max_leverage > MAX_ACCOUNT_LEVERAGE:
            reasons.append("contract_max_leverage_exceeds_policy")
        if contract.max_effective_leverage > MAX_EFFECTIVE_LEVERAGE:
            reasons.append("contract_effective_leverage_exceeds_policy")
        if contract.min_margin_level < MIN_ENTRY_MARGIN_LEVEL:
            reasons.append("contract_min_margin_level_below_policy")
        if not (
            EMERGENCY_MARGIN_LEVEL
            <= contract.emergency_margin_level
            < contract.warn_margin_level
            <= contract.min_margin_level
        ):
            reasons.append("contract_margin_ladder_invalid")
        if contract.max_snapshot_age_ms > MAX_ALLOWED_SNAPSHOT_AGE_MS:
            reasons.append("contract_snapshot_age_exceeds_policy")
        if contract.interest_horizon_hours > MAX_ALLOWED_INTEREST_HORIZON_HOURS:
            reasons.append("contract_interest_horizon_exceeds_policy")
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


def _projected_effective_leverage(
    intent: Intent, snapshot: Snapshot
) -> tuple[Decimal | None, Decimal | None]:
    quote_row = snapshot.assets.get(intent.quote_asset)
    base_row = snapshot.assets.get(intent.base_asset)
    if quote_row is None or base_row is None:
        return None, None
    total_asset = (quote_row.free + quote_row.locked) + (
        (base_row.free + base_row.locked) * snapshot.reference_price
    )
    total_liab = (quote_row.borrowed + quote_row.interest) + (
        (base_row.borrowed + base_row.interest) * snapshot.reference_price
    )
    net_asset = total_asset - total_liab
    if net_asset <= 0:
        return None, None
    added_borrow = (
        intent.quantity * intent.limit_price if intent.action == "OPEN_SHORT" else Decimal(0)
    )
    projected_lev = (total_asset + added_borrow) / net_asset
    projected_liab_ratio = (total_liab + added_borrow) / net_asset
    return projected_lev, projected_liab_ratio


def contract_identity_reasons(
    intent: Intent, contract: Contract, snapshot: Snapshot, gate: Gate
) -> list[str]:
    reasons: list[str] = []
    if intent.purpose != PURPOSE_CROSS_MARGIN_TRACK:
        reasons.append("purpose_not_cross_margin_track")

    if not contract.parameters_approved:
        if gate.execution_mode != OFFLINE_EXECUTION_MODE:
            reasons.append("live_network_disabled")
        if (
            intent.strategy_id != OFFLINE_FIXTURE_STRATEGY_ID
            or contract.strategy_id != OFFLINE_FIXTURE_STRATEGY_ID
        ):
            reasons.append("strategy_not_in_offline_fixture_allowlist")
        if contract.unapproved_label != REQUIRED_UNAPPROVED_LABEL:
            reasons.append("unapproved_label_required")
    else:
        if not parameters_approved_by_env():
            reasons.append("parameters_approved_must_be_false")
        if gate.execution_mode not in {APPROVED_DRY_RUN_MODE, LIVE_EXECUTION_MODE}:
            reasons.append("execution_mode_invalid_for_approved_contract")
        if gate.execution_mode == LIVE_EXECUTION_MODE and not live_enabled_by_env():
            reasons.append("live_network_disabled")
        if contract.unapproved_label != APPROVED_CONTRACT_LABEL:
            reasons.append("approved_label_mismatch")
        if (
            intent.strategy_id != contract.strategy_id
            or intent.strategy_id not in APPROVED_CROSS_MARGIN_STRATEGIES
        ):
            reasons.append("strategy_not_in_approved_allowlist")
        if snapshot.account_type != "MARGIN_1":
            reasons.append("account_type_not_classic_cross_margin")

    uids = {intent.account_uid, contract.account_uid, snapshot.uid}
    if len(uids) != 1:
        reasons.append("uid_mismatch")
    if MCP_FORBIDDEN_UID in uids:
        reasons.append("mcp_account_forbidden")
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
    if snapshot.margin_level_status not in ALLOWED_MARGIN_LEVEL_STATUSES:
        reasons.append("margin_level_status_unsafe")
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
    notional = intent.quantity * intent.limit_price
    if notional < filters.min_notional:
        reasons.append("entry_notional_below_min")
    if contract.parameters_approved and notional < MIN_ORDER_NOTIONAL_USDT:
        reasons.append("notional_below_20_usdt_floor")
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
        elif contract.parameters_approved and intent.quantity > borrowable * MAX_BORROWABLE_POOL_RATIO:
            reasons.append("asset_borrow_exceeds_pool_safety_cap")
        tier_limit = snapshot.borrow_limit.get(contract.borrow_asset)
        if contract.parameters_approved and tier_limit is not None and tier_limit > 0:
            if intent.quantity > tier_limit:
                reasons.append("asset_borrow_exceeds_tier_limit")
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
    elif contract.parameters_approved:
        proj_lev, proj_liab = _projected_effective_leverage(intent, snapshot)
        if proj_lev is None or proj_liab is None:
            reasons.append("net_asset_not_positive")
        else:
            if proj_lev > contract.max_effective_leverage:
                reasons.append("effective_leverage_exceeded")
            if proj_liab > MAX_TOTAL_LIABILITY_NET_RATIO:
                reasons.append("total_liability_exceeds_cap")
    if snapshot.fee_asset not in {intent.base_asset, intent.quote_asset}:
        reasons.append("fee_asset_unmodeled")
    return reasons
