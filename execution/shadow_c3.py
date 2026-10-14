"""C3 / F-3 shadow: peak mark and giveback on close. Does not change exits.

``peak_price`` is the highest mid observed while the position is open. It is
stored on ``positions.meta_json`` and only rises. Take-profit, stop-loss, and
max-hold in ``execution.exits`` are not consulted here.

On a real close the shadow row records:

- ``peak_pnl_pct`` = (peak / entry - 1) * 100
- ``giveback_from_peak_pct`` = (peak - exit_fill) / entry * 100

Giveback is percentage points of the entry fill, from the peak mid down to the
exit fill. It is the profit given back, not a shorter hold.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable


def parse_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def raise_peak(meta: dict[str, Any], price: float) -> tuple[dict[str, Any], bool]:
    """Return ``(meta, changed)``. A non-positive price is ignored."""
    try:
        observed = float(price)
    except (TypeError, ValueError):
        return meta, False
    if observed <= 0:
        return meta, False
    current = meta.get("peak_price")
    try:
        cur = float(current) if current is not None else None
    except (TypeError, ValueError):
        cur = None
    if cur is None or observed > cur:
        updated = dict(meta)
        updated["peak_price"] = observed
        return updated, True
    return meta, False


def refresh_open_peaks(ledger: Any, price_lookup: Callable[[dict[str, Any]], Any]) -> int:
    """Raise ``peak_price`` on each open position. Returns how many rows changed.

    Missing ``merge_open_meta`` is a no-op (tests can pass a partial ledger).
    """
    merge = getattr(ledger, "merge_open_meta", None)
    if not callable(merge):
        return 0
    list_open = getattr(ledger, "list_open", None)
    if not callable(list_open):
        return 0
    changed = 0
    for pos in list_open():
        if not isinstance(pos, dict):
            pos = dict(pos)
        raw_px = price_lookup(pos)
        if raw_px is None:
            continue
        meta = parse_meta(pos.get("meta_json"))
        updated, did = raise_peak(meta, raw_px)
        if not did:
            continue
        merge(str(pos["id"]), {"peak_price": updated["peak_price"]})
        changed += 1
    return changed


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hold_minutes(opened_at: Any, closed_at: Any) -> float | None:
    opened = parse_ts(opened_at)
    closed = parse_ts(closed_at)
    if opened is None or closed is None:
        return None
    return (closed - opened).total_seconds() / 60.0


def resolve_peak(
    meta: dict[str, Any],
    entry: float,
    mid_price: float | None,
) -> float:
    """Peak used at close. Stored peak wins; a higher close mid can still raise it."""
    raw = meta.get("peak_price")
    try:
        peak = float(raw) if raw is not None else float(entry)
    except (TypeError, ValueError):
        peak = float(entry)
    if peak <= 0:
        peak = float(entry)
    if mid_price is not None:
        try:
            mid = float(mid_price)
        except (TypeError, ValueError):
            mid = 0.0
        if mid > peak:
            peak = mid
    return peak


def shadow_metrics(
    *,
    entry: float,
    peak: float,
    exit_price: float,
) -> tuple[float, float]:
    """``(peak_pnl_pct, giveback_from_peak_pct)``. Entry ≤ 0 yields zeros."""
    if entry <= 0:
        return 0.0, 0.0
    peak_pnl_pct = (peak / entry - 1.0) * 100.0
    giveback_from_peak_pct = (peak - exit_price) / entry * 100.0
    return peak_pnl_pct, giveback_from_peak_pct


def summarize_giveback(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mean giveback and mean realized pnl, grouped by ``actual_reason``."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        reason = str(row.get("actual_reason") or "unknown")
        buckets.setdefault(reason, []).append(row)
    out: list[dict[str, Any]] = []
    for reason, group in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(group)
        give = [_f(r.get("giveback_from_peak_pct")) for r in group]
        pnls = [_f(r.get("actual_pnl_usd")) for r in group]
        peaks = [_f(r.get("peak_pnl_pct")) for r in group]
        out.append(
            {
                "actual_reason": reason,
                "n": n,
                "mean_giveback_from_peak_pct": sum(give) / n if n else None,
                "mean_peak_pnl_pct": sum(peaks) / n if n else None,
                "mean_actual_pnl_usd": sum(pnls) / n if n else None,
            }
        )
    return out


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
