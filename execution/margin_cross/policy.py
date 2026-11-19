"""Ruled cross-margin risk limits, kill-switch checks, and exception policy.

Runtime unlock stays fail-closed by default:
- ``CROSS_MARGIN_PARAMETERS_APPROVED`` must be the literal string ``"1"``.
- ``CROSS_MARGIN_LIVE_ENABLED`` must be the literal string ``"1"`` for live I/O.
- ``LIVE_TRADING_DISABLED`` (env ``"1"`` or kill-switch file) blocks new live risk.

Never calls ``POST /sapi/v1/margin/max-leverage`` and never accepts MCP sub-account
uid ``1289917075`` or isolated / Cross-Pro (>3x) margin accounts.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import os
from pathlib import Path

PARAMETERS_APPROVED_ENV = "CROSS_MARGIN_PARAMETERS_APPROVED"
LIVE_ENABLED_ENV = "CROSS_MARGIN_LIVE_ENABLED"
BLOCK_EXIT_ON_KILL_ENV = "CROSS_MARGIN_BLOCK_EXIT_ON_KILL"
FILL_TP_FORMULA_B_ENV = "CROSS_MARGIN_FILL_TP_FORMULA_B"
ENABLE_STATE2_LONG_ENV = "CROSS_MARGIN_ENABLE_STATE2_LONG"
ENABLE_STATE4_LONG_ENV = "CROSS_MARGIN_ENABLE_STATE4_LONG"
KILL_SWITCH_ENV = "LIVE_KILL_SWITCH_PATH"
LIVE_DISABLED_FLAG_ENV = "LIVE_TRADING_DISABLED"
DEFAULT_KILL_SWITCH_NAME = "LIVE_TRADING_DISABLED"

APPROVED_CONTRACT_LABEL = "RULED_CROSS_MARGIN_V1"
APPROVED_DRY_RUN_MODE = "approved_dry_run"
LIVE_EXECUTION_MODE = "live"
MCP_FORBIDDEN_UID = "1289917075"

# Ruled leverage, borrow, and margin-level ceilings/floors
MAX_ACCOUNT_LEVERAGE = Decimal("3")
MAX_EFFECTIVE_LEVERAGE = Decimal("1.50")
MAX_TOTAL_LIABILITY_NET_RATIO = Decimal("0.50")
MAX_BORROWABLE_POOL_RATIO = Decimal("0.80")
MIN_ENTRY_MARGIN_LEVEL = Decimal("2.50")
WARN_MARGIN_LEVEL = Decimal("2.00")
EMERGENCY_MARGIN_LEVEL = Decimal("1.60")
MIN_ORDER_NOTIONAL_USDT = Decimal("20")

DEFAULT_MAX_SNAPSHOT_AGE_MS = 5000
MAX_ALLOWED_SNAPSHOT_AGE_MS = 15000
DEFAULT_INTEREST_HORIZON_HOURS = Decimal("4")
MAX_ALLOWED_INTEREST_HORIZON_HOURS = Decimal("24")
BORROW_REPAY_MIN_SPACING_SEC = Decimal("0.11")

ALLOWED_MARGIN_LEVEL_STATUSES = frozenset({"EXCESSIVE", "NORMAL"})
EMERGENCY_MARGIN_LEVEL_STATUSES = frozenset(
    {"MARGIN_CALL", "PRE_LIQUIDATION", "FORCE_LIQUIDATION"}
)

APPROVED_SHORT_STRATEGIES = frozenset(
    {
        "regime4_crowded_long_carry_short",
        "regime5_crisis_contagion_short",
        "regime5_unlock_cliff_short",
    }
)
APPROVED_LONG_STRATEGIES = frozenset(
    {
        "tech_boll_fade_v1",
        "regime1_positive_gamma_fade",
        "regime3_xs_leader_pullback",
        "regime2_short_squeeze_breakout",
        "regime4_defensive_deep_fade",
    }
)
APPROVED_CROSS_MARGIN_STRATEGIES = APPROVED_SHORT_STRATEGIES | APPROVED_LONG_STRATEGIES

TERMINAL_BORROW_REJECT_CODES = frozenset({-3003, -2015, -3006, -3012, -3045})
PENDING_TRANSACTION_CODE = -3007
REPAY_EXCEED_LIABILITY_CODE = -3015


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
    """Check ``LIVE_TRADING_DISABLED`` env flag and kill-switch file locations."""
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
) -> list[str]:
    """Return every reason live cross-margin execution is currently locked."""
    reasons: list[str] = []
    if not allow_live_network:
        reasons.append("live_network_not_authorized")
    if not parameters_approved_by_env(environ):
        reasons.append("parameters_not_approved")
    if not live_enabled_by_env(environ):
        reasons.append("cross_margin_live_disabled")
    kill_on, _kill_name = kill_switch_active(explicit_kill_path, environ)
    if kill_on:
        if not for_protective_or_repay or block_exit_on_kill_by_env(environ):
            reasons.append("kill_switch_active")
    return reasons
