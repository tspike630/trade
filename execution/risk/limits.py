"""Limit checkers: max open per strategy + daily loss circuit (skeleton)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from execution.risk import register

# Hong Kong has no DST. Paper closed_at stays UTC ISO; the loss day is this zone.
_HKT = ZoneInfo("Asia/Hong_Kong")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hkt_today() -> str:
    """Current calendar date in Asia/Hong_Kong (YYYY-MM-DD)."""
    now = _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_HKT).date().isoformat()


def _closed_at_hkt_day(closed_at: Any) -> Optional[str]:
    """Asia/Hong_Kong calendar date of a closed_at stamp, or None if unusable.

    Paper ledger writes UTC ISO (``...Z``). Naive stamps are UTC. A value that
    does not parse is ignored so it cannot move the day bucket.
    """
    raw = str(closed_at or "").strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_HKT).date().isoformat()


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
    """单策略最多同时持仓数（token_dedup 之外的第二道闸）。

    SELL 不跑这条：达上限时必须仍能平仓。
    """
    action = str(sig.get("action") or "").strip().lower()
    if action == "sell":
        return {"ok": True, "note": "sell_skips_max_open"}
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
    """当日已实现亏损熔断（USD；None=关）。

    「当日」是 Asia/Hong_Kong 日历日。``closed_at`` 仍是 UTC ISO，按该时刻的
    HKT 日期归桶，不用 UTC 日期前缀。00:00–08:00 HKT 不再把上一 HKT 日的亏损
    算进今天。

    拒绝时 reason 固定为 ``daily_loss``。``today`` / ``pnl`` / ``limit`` 在
    ``detail``，不拼进 reason，便于回执与 ``order_events`` 稳定分成
    ``risk_rejected:daily_loss``。阈值仍是 ``pnl < -limit`` 才拒。
    ``EXECUTION_DAILY_LOSS_USD`` 未设置仍是关；限额数值语义不变。
    """
    if daily_loss_limit_usd is None:
        return {"ok": True}
    today = _hkt_today()
    pnl = 0.0
    for row in _closed_rows(ledger):
        if _closed_at_hkt_day(row.get("closed_at")) != today:
            continue
        try:
            pnl += float(row.get("pnl_usd") or 0.0)
        except (TypeError, ValueError):
            continue
    limit = float(daily_loss_limit_usd)
    if pnl < -limit:
        return {
            "ok": False,
            "reason": "daily_loss",
            "detail": {"today": today, "pnl": pnl, "limit": limit},
        }
    return {"ok": True, "note": "daily_loss skeleton (off unless limit set)"}


register("max_open_per_strategy", check_max_open)
register("daily_loss_limit", check_daily_loss)
