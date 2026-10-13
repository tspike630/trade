"""Limit checkers: max open per strategy + daily loss circuit (skeleton)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from execution.risk import register


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _meta_dict(pos: dict[str, Any]) -> dict[str, Any]:
    """PaperLedger stores meta_json as TEXT; callers may also pass a dict."""
    meta = pos.get("meta_json")
    if isinstance(meta, dict):
        return meta
    if not meta or not isinstance(meta, str):
        return {}
    try:
        parsed = json.loads(meta)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def check_max_open(
    ledger,
    sig: dict[str, Any],
    *,
    max_open_per_strategy: int = 5,
    **_: Any,
) -> dict[str, Any]:
    """单策略最多同时持仓数（token_dedup 之外的第二道闸）。"""
    sid = sig.get("strategy_id") or "(unknown)"
    n_open = len(
        [
            p
            for p in ledger.list_open()
            if _meta_dict(p).get("strategy_id") == sid
        ]
    )
    if n_open >= int(max_open_per_strategy):
        return {
            "ok": False,
            "reason": f"max_open:{sid}:{n_open}>={max_open_per_strategy}",
        }
    return {"ok": True}


def _closed_rows(ledger) -> list[dict[str, Any]]:
    """Closed positions. PaperLedger.list_closed(limit=0) is empty (SQL LIMIT 0).

    Real columns are closed_at + pnl_usd (positions has no created_at).
    """
    fn = getattr(ledger, "list_closed", None)
    if fn is None:
        return []
    try:
        rows = fn(limit=100_000)
    except TypeError:
        rows = fn()
    out: list[dict[str, Any]] = []
    for row in rows or []:
        out.append(row if isinstance(row, dict) else dict(row))
    return out


def check_daily_loss(
    ledger,
    sig: dict[str, Any],
    *,
    daily_loss_limit_usd: Optional[float] = None,
    **_: Any,
) -> dict[str, Any]:
    """当日已实现亏损熔断（USD；None=关）。"""
    if daily_loss_limit_usd is None:
        return {"ok": True}
    today = _utc_now().date().isoformat()
    pnl = 0.0
    for row in _closed_rows(ledger):
        closed_at = str(row.get("closed_at") or "")
        if not closed_at.startswith(today):
            continue
        try:
            pnl += float(row.get("pnl_usd") or 0.0)
        except (TypeError, ValueError):
            continue
    limit = float(daily_loss_limit_usd)
    if pnl < -limit:
        return {"ok": False, "reason": f"daily_loss:{today}:{pnl}<{-limit}"}
    return {"ok": True, "note": "daily_loss skeleton (off unless limit set)"}


register("max_open_per_strategy", check_max_open)
register("daily_loss_limit", check_daily_loss)
