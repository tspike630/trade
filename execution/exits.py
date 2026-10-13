"""Unified exit rules shared by paper tick, backtest engine and future live executor.

Copied verbatim from paper_trade.tick() L254-275 semantics (same defaults, same
reason strings), so switching call sites to this module is behavior-identical.

  reason = exit_reason(position, price, now)
  # -> "take_profit" | "stop_loss" | "max_hold" | None
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

DEFAULT_TP_PCT = 10.0
DEFAULT_SL_PCT = 5.0
DEFAULT_MAX_HOLD_MIN = 60.0


def _parse_ts(s: Any) -> Optional[datetime]:
    """Parse an ISO timestamp. Trailing Z is UTC (Python ≤3.10 fromisoformat rejects Z)."""
    if not s:
        return None
    text = str(s).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def exit_reason(
    position: dict[str, Any],
    price: float,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """One position, one mark price -> exit reason (or None)."""
    entry = float(position.get("entry_price") or 0.0)
    if entry <= 0:
        return None
    tp = position.get("tp_pct")
    sl = position.get("sl_pct")
    if tp is not None and price >= entry * (1 + float(tp) / 100.0):
        return "take_profit"
    if sl is not None and price <= entry * (1 - float(sl) / 100.0):
        return "stop_loss"
    opened = _parse_ts(position.get("opened_at"))
    hold = position.get("max_hold_min")
    if opened and hold is not None:
        now = now or datetime.now(timezone.utc)
        if now >= opened + timedelta(minutes=float(hold)):
            return "max_hold"
    return None


def scan_exits(
    open_positions: list[dict[str, Any]],
    price_lookup,
    now: Optional[datetime] = None,
) -> list[tuple[dict[str, Any], str]]:
    """扫全部持仓：返回 [(position, reason)] 待平仓清单（调用方负责 close）。"""
    now = now or datetime.now(timezone.utc)
    out: list[tuple[dict[str, Any], str]] = []
    for p in open_positions:
        px = price_lookup(p)
        if px is None:
            continue
        reason = exit_reason(p, float(px), now)
        if reason is not None:
            out.append((p, reason))
    return out
