"""Cross-margin structural safety invariants, zero-default operator config loader, and gates.

No leverage, borrow-ratio, margin-level, snapshot-age, interest-horizon, or
position-sizing threshold has a built-in live default here. Missing or
unapproved thresholds fail closed:
- ``CROSS_MARGIN_PARAMETERS_APPROVED`` must equal literal ``"1"``.
- ``CROSS_MARGIN_APPROVED_CONFIG_JSON`` must point to a readable JSON file
  explicitly specifying every required threshold with zero defaults.
- ``CROSS_MARGIN_LIVE_ENABLED`` must equal literal ``"1"`` for live network I/O.
- Active kill-switch flag/file blocks new live risk.

Never calls ``POST /sapi/v1/margin/max-leverage`` and never accepts MCP sub-account
uid ``1289917075`` or isolated / Cross-Pro (>3x) margin accounts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any

from execution.margin_cross.money import parse_decimal

PARAMETERS_APPROVED_ENV = "CROSS_MARGIN_PARAMETERS_APPROVED"
APPROVED_CONFIG_PATH_ENV = "CROSS_MARGIN_APPROVED_CONFIG_JSON"
LIVE_ENABLED_ENV = "CROSS_MARGIN_LIVE_ENABLED"
BLOCK_EXIT_ON_KILL_ENV = "CROSS_MARGIN_BLOCK_EXIT_ON_KILL"
FILL_TP_FORMULA_B_ENV = "CROSS_MARGIN_FILL_TP_FORMULA_B"
ENABLE_STATE2_LONG_ENV = "CROSS_MARGIN_ENABLE_STATE2_LONG"
ENABLE_STATE4_LONG_ENV = "CROSS_MARGIN_ENABLE_STATE4_LONG"
KILL_SWITCH_ENV = "LIVE_KILL_SWITCH_PATH"
LIVE_DISABLED_FLAG_ENV = "LIVE_TRADING_DISABLED"
DEFAULT_KILL_SWITCH_NAME = "LIVE_TRADING_DISABLED"

APPROVED_CONTRACT_LABEL = "OPERATOR_APPROVED_CROSS_MARGIN_V1"
APPROVED_DRY_RUN_MODE = "approved_dry_run"
LIVE_EXECUTION_MODE = "live"
MCP_FORBIDDEN_UID = "1289917075"

# Exchange-protocol structural ceiling for Classic Cross Margin (MARGIN_1 <= 3x).
# This is an upper protocol bound, NOT a default contract parameter.
HARD_CLASSIC_CROSS_LEVERAGE_CEILING = Decimal("3")
BORROW_REPAY_MIN_SPACING_SEC = Decimal("0.11")

ALLOWED_MARGIN_LEVEL_STATUSES = frozenset({"EXCESSIVE", "NORMAL"})
EMERGENCY_MARGIN_LEVEL_STATUSES = frozenset(
    {"MARGIN_CALL", "PRE_LIQUIDATION", "FORCE_LIQUIDATION"}
)

# Router capability set only; NOT an active allowlist until explicitly listed in
# operator-supplied ``ApprovedRiskConfig.approved_strategies``.
SUPPORTED_SHORT_STRATEGIES = frozenset(
    {
        "regime4_crowded_long_carry_short",
        "regime5_crisis_contagion_short",
        "regime5_unlock_cliff_short",
    }
)
SUPPORTED_LONG_STRATEGIES = frozenset(
    {
        "tech_boll_fade_v1",
        "regime1_positive_gamma_fade",
        "regime3_xs_leader_pullback",
        "regime2_short_squeeze_breakout",
        "regime4_defensive_deep_fade",
    }
)
SUPPORTED_REGIME_STRATEGIES = SUPPORTED_SHORT_STRATEGIES | SUPPORTED_LONG_STRATEGIES

TERMINAL_BORROW_REJECT_CODES = frozenset({-3003, -2015, -3006, -3012, -3045})
PENDING_TRANSACTION_CODE = -3007
REPAY_EXCEED_LIABILITY_CODE = -3015


@dataclass(frozen=True)
class ApprovedRiskConfig:
    """Explicit zero-default operator-approved risk & sizing configuration."""

    max_leverage: Decimal
    max_effective_leverage: Decimal
    max_total_liability_net_ratio: Decimal
    max_single_borrow_net_ratio: Decimal
    max_borrowable_pool_ratio: Decimal
    min_entry_margin_level: Decimal
    warn_margin_level: Decimal
    emergency_margin_level: Decimal
    min_order_notional_usdt: Decimal
    max_snapshot_age_ms: int
    interest_horizon_hours: Decimal
    base_position_pct: Decimal
    approved_strategies: frozenset[str]


def _req_dec(
    raw: Mapping[str, Any],
    field: str,
    reasons: list[str],
    *,
    positive: bool = True,
) -> Decimal:
    if field not in raw or raw.get(field) is None:
        reasons.append(f"approved_param_{field}_missing")
        return Decimal(0)
    val, err = parse_decimal(raw.get(field), field)
    if err or val is None:
        reasons.append(f"approved_param_{field}_invalid")
        return Decimal(0)
    if positive and val <= 0:
        reasons.append(f"approved_param_{field}_not_positive")
    elif not positive and val < 0:
        reasons.append(f"approved_param_{field}_negative")
    return val


def _req_int_ms(raw: Mapping[str, Any], field: str, reasons: list[str]) -> int:
    if field not in raw or raw.get(field) is None:
        reasons.append(f"approved_param_{field}_missing")
        return 0
    val, err = parse_decimal(raw.get(field), field)
    if err or val is None or val != val.to_integral_value() or val <= 0:
        reasons.append(f"approved_param_{field}_invalid")
        return 0
    return int(val)


def parse_approved_risk_config(
    raw: Mapping[str, Any] | None,
) -> tuple[ApprovedRiskConfig | None, list[str]]:
    """Parse explicit operator risk config with ZERO fallback defaults."""
    if not isinstance(raw, Mapping):
        return None, ["approved_config_missing"]
    reasons: list[str] = []
    max_lev = _req_dec(raw, "max_leverage", reasons)
    max_eff_lev = _req_dec(raw, "max_effective_leverage", reasons)
    max_tot_liab = _req_dec(raw, "max_total_liability_net_ratio", reasons)
    max_single_borrow = _req_dec(raw, "max_single_borrow_net_ratio", reasons)
    max_pool_ratio = _req_dec(raw, "max_borrowable_pool_ratio", reasons)
    min_entry_ml = _req_dec(raw, "min_entry_margin_level", reasons)
    warn_ml = _req_dec(raw, "warn_margin_level", reasons)
    emerg_ml = _req_dec(raw, "emergency_margin_level", reasons)
    min_notional = _req_dec(raw, "min_order_notional_usdt", reasons)
    max_snap_ms = _req_int_ms(raw, "max_snapshot_age_ms", reasons)
    horizon_h = _req_dec(raw, "interest_horizon_hours", reasons, positive=False)
    base_pct = _req_dec(raw, "base_position_pct", reasons)

    strat_raw = raw.get("approved_strategies")
    strategies: set[str] = set()
    if not isinstance(strat_raw, (list, tuple, set, frozenset)) or not strat_raw:
        reasons.append("approved_param_approved_strategies_missing")
    else:
        for item in strat_raw:
            if not isinstance(item, str) or not item.strip():
                reasons.append("approved_param_approved_strategies_invalid")
                break
            name = item.strip()
            if name not in SUPPORTED_REGIME_STRATEGIES:
                reasons.append(f"unsupported_strategy_in_approved_config:{name}")
            else:
                strategies.add(name)

    if not reasons:
        if max_lev > HARD_CLASSIC_CROSS_LEVERAGE_CEILING:
            reasons.append("approved_max_leverage_exceeds_classic_cross_ceiling")
        if max_eff_lev > max_lev:
            reasons.append("approved_effective_leverage_exceeds_max_leverage")
        if max_tot_liab > Decimal(1):
            reasons.append("approved_total_liability_ratio_above_one")
        if max_single_borrow > max_tot_liab:
            reasons.append("approved_single_borrow_exceeds_total_liability_ratio")
        if max_pool_ratio > Decimal(1):
            reasons.append("approved_borrowable_pool_ratio_above_one")
        if not (Decimal(1) < emerg_ml < warn_ml < min_entry_ml):
            reasons.append("approved_margin_level_ladder_invalid")
        if base_pct > Decimal(1):
            reasons.append("approved_base_position_pct_above_one")

    if reasons:
        return None, reasons
    return (
        ApprovedRiskConfig(
            max_leverage=max_lev,
            max_effective_leverage=max_eff_lev,
            max_total_liability_net_ratio=max_tot_liab,
            max_single_borrow_net_ratio=max_single_borrow,
            max_borrowable_pool_ratio=max_pool_ratio,
            min_entry_margin_level=min_entry_ml,
            warn_margin_level=warn_ml,
            emergency_margin_level=emerg_ml,
            min_order_notional_usdt=min_notional,
            max_snapshot_age_ms=max_snap_ms,
            interest_horizon_hours=horizon_h,
            base_position_pct=base_pct,
            approved_strategies=frozenset(strategies),
        ),
        [],
    )


def load_approved_risk_config(
    environ: Mapping[str, str] | None = None,
) -> tuple[ApprovedRiskConfig | None, list[str]]:
    """Load explicit zero-default operator config from ``CROSS_MARGIN_APPROVED_CONFIG_JSON``."""
    env = os.environ if environ is None else environ
    raw_path = str(env.get(APPROVED_CONFIG_PATH_ENV) or "").strip()
    if not raw_path:
        return None, ["approved_config_missing"]
    path = Path(raw_path).expanduser()
    if not path.is_file():
        return None, ["approved_config_file_not_found"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, ["approved_config_unreadable_or_invalid_json"]
    return parse_approved_risk_config(doc)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def env_flag_literal_one(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """True strictly when the environment variable equals literal ``'1'``."""
    env = os.environ if environ is None else environ
    raw = env.get(name)
    return isinstance(raw, str) and raw == "1"


def parameters_approved_by_env(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag_literal_one(PARAMETERS_APPROVED_ENV, environ)


def live_enabled_by_env(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag_literal_one(LIVE_ENABLED_ENV, environ)


def block_exit_on_kill_by_env(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag_literal_one(BLOCK_EXIT_ON_KILL_ENV, environ)


def fill_tp_formula_b_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag_literal_one(FILL_TP_FORMULA_B_ENV, environ)


def state2_long_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag_literal_one(ENABLE_STATE2_LONG_ENV, environ)


def state4_long_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag_literal_one(ENABLE_STATE4_LONG_ENV, environ)


def kill_switch_active(
    explicit_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    """Check kill-switch env flag and kill-switch file locations."""
    env = os.environ if environ is None else environ
    env_flag = str(env.get(LIVE_DISABLED_FLAG_ENV, "")).strip().lower()
    if env_flag in {"1", "true", "yes", "on"}:
        return True, LIVE_DISABLED_FLAG_ENV
    if explicit_path is not None and str(explicit_path).strip():
        candidate = Path(explicit_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.is_file(), candidate.name
    configured = str(env.get(KILL_SWITCH_ENV, "")).strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.is_file(), candidate.name
    root = _repo_root()
    for path in (
        Path.cwd() / DEFAULT_KILL_SWITCH_NAME,
        root / DEFAULT_KILL_SWITCH_NAME,
        root / "crawler" / DEFAULT_KILL_SWITCH_NAME,
    ):
        if path.is_file():
            return True, path.name
    return False, DEFAULT_KILL_SWITCH_NAME


def live_gate_reasons(
    *,
    allow_live_network: bool,
    for_protective_or_repay: bool = False,
    explicit_kill_path: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    approved_config: ApprovedRiskConfig | None = None,
) -> list[str]:
    """Return every reason live cross-margin execution is currently locked."""
    reasons: list[str] = []
    if not allow_live_network:
        reasons.append("live_network_not_authorized")
    if not parameters_approved_by_env(environ):
        reasons.append("parameters_not_approved")
    cfg = approved_config
    if cfg is None:
        cfg, cfg_reasons = load_approved_risk_config(environ)
        if cfg is None:
            reasons.extend(cfg_reasons)
    if not live_enabled_by_env(environ):
        reasons.append("cross_margin_live_disabled")
    kill_on, _kill_name = kill_switch_active(explicit_kill_path, environ)
    if kill_on:
        if not for_protective_or_repay or block_exit_on_kill_by_env(environ):
            reasons.append("kill_switch_active")
    return reasons
