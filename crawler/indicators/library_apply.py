"""Pure write rules for pandas-ta columns. No pandas import.

P1-08: preserving a previous RSI/MACD/BOLL value is not a fresh compute.
P2-13: MACD is one set — all three values, the previous set, or all NULL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from indicators.base import parse_iso

MACD_FIELDS: tuple[str, ...] = ("macd", "macd_signal", "macd_hist")
BOLL_FIELDS: tuple[str, ...] = ("boll_upper", "boll_mid", "boll_lower")


def apply_library_fields(
    prev: dict[str, Any] | None,
    lib: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Return RSI/MACD/BOLL columns and whether this pass recomputed them.

    ``fresh`` is False when any written value was copied from ``prev`` because
    this pass did not produce it. Honest NULLs (nothing computed and nothing
    stored) are fresh: the clock may advance. A kept-old value is not.
    """
    prev = prev or {}
    lib = lib or {}
    out: dict[str, Any] = {}
    preserved = False

    macd_now = [lib.get(key) for key in MACD_FIELDS]
    if all(value is not None for value in macd_now):
        for key, value in zip(MACD_FIELDS, macd_now):
            out[key] = value
    elif any(prev.get(key) is not None for key in MACD_FIELDS):
        for key in MACD_FIELDS:
            out[key] = prev.get(key)
        preserved = True
    else:
        for key in MACD_FIELDS:
            out[key] = None

    rsi = lib.get("rsi_14")
    if rsi is not None:
        out["rsi_14"] = rsi
    elif prev.get("rsi_14") is not None:
        out["rsi_14"] = prev.get("rsi_14")
        preserved = True
    else:
        out["rsi_14"] = None

    for key in BOLL_FIELDS:
        value = lib.get(key)
        if value is not None:
            out[key] = value
        elif prev.get(key) is not None:
            out[key] = prev.get(key)
            preserved = True
        else:
            out[key] = None

    return out, not preserved


def technical_refresh_due(
    tech_updated_at: str | None,
    *,
    tier: str,
    now: datetime,
    hot_interval_sec: int,
    warm_interval_sec: int,
    cold_interval_sec: int,
) -> bool:
    """Whether technical should run. NULL tech_updated_at is always due.

    The shared token_indicators.updated_at column is intentionally not read.
    Funding and sentiment refresh that column every pass.
    """
    last_dt = parse_iso(tech_updated_at)
    if last_dt is None:
        return True
    age = (now - last_dt).total_seconds()
    if tier == "hot":
        need = hot_interval_sec
    elif tier == "cold":
        need = cold_interval_sec
    else:
        need = warm_interval_sec
    return age >= need
