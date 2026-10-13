"""Execution-layer switches.

Risk skeleton (plan 05) defaults off. Paper / PM gates stay in their own env
flags and are not changed here.
"""

from __future__ import annotations

import os


def _env_bool(name: str, default: bool = False) -> bool:
    """Same truthy set as execution.gateway._env_bool: 1/true/yes/on."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float_or_none(name: str) -> float | None:
    """Unset, blank, or ≤0 → None (rule off)."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


# 执行层风控骨架（方案5；默认关）。只认 _env_bool，默认仍为关。
EXECUTION_RISK_ENABLED = _env_bool("EXECUTION_RISK_ENABLED", False)
EXECUTION_MAX_OPEN = int(os.getenv("EXECUTION_MAX_OPEN", "5"))
# 当日已实现亏损熔断（USD）。未设置 → 不启用。不改变 EXECUTION_RISK_ENABLED 默认关。
EXECUTION_DAILY_LOSS_USD = _env_float_or_none("EXECUTION_DAILY_LOSS_USD")

# 订单事件流（方案5-2a；默认开——纯审计增强、零行为副作用，可关）
EXECUTION_EVENTS_ENABLED = _env_bool("EXECUTION_EVENTS_ENABLED", True)
