"""C exit grid constants and offline path-replay helpers.

Eight cells: original 4 (SL∈{4,5}×hold∈{60,90}, TP10) + c_sl3_h60, c_sl3_h30,
c_tp5half_sl5_h60 (+5% sell half, rest TP10/SL5), c_trail3_h60 (3% trailing, SL5 floor).
Does not touch main paper_trading.db or .env.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional


TP_PCT = 10.0

# Legacy tuple view (tag, sl, hold) of the original four TP10 cells.
GRID_CELLS: tuple[tuple[str, float, float], ...] = (
    ("c_sl4_h60", 4.0, 60.0),
    ("c_sl5_h60", 5.0, 60.0),  # main-path mirror
    ("c_sl4_h90", 4.0, 90.0),
    ("c_sl5_h90", 5.0, 90.0),
)

MIN_CLOSED_PER_CELL = 30
DAILY_LOSS_CAP_USD = 200.0
ROLLBACK_SINGLE_USD = -500.0
ROLLBACK_CUMULATIVE_USD = -400.0


@dataclass(frozen=True)
class GridCell:
    tag: str
    tp_pct: float | None
    sl_pct: float
    max_hold_min: float
    # Partial take-profit: when price >= entry*(1+partial_tp_pct%), sell
    # partial_frac of the original qty once; remainder keeps tp/sl/hold.
    partial_tp_pct: float | None = None
    partial_frac: float = 0.0
    # Trailing stop: exit when price <= peak*(1-trail_pct%). sl_pct stays as
    # the hard floor from entry; tp_pct None means no fixed TP.
    trail_pct: float | None = None


CELLS: tuple[GridCell, ...] = (
    GridCell("c_sl4_h60", TP_PCT, 4.0, 60.0),
    GridCell("c_sl5_h60", TP_PCT, 5.0, 60.0),  # main-path mirror
    GridCell("c_sl4_h90", TP_PCT, 4.0, 90.0),
    GridCell("c_sl5_h90", TP_PCT, 5.0, 90.0),
    GridCell("c_sl3_h60", TP_PCT, 3.0, 60.0),
    GridCell("c_sl3_h30", TP_PCT, 3.0, 30.0),
    GridCell("c_tp5half_sl5_h60", TP_PCT, 5.0, 60.0, partial_tp_pct=5.0, partial_frac=0.5),
    GridCell("c_trail3_h60", None, 5.0, 60.0, trail_pct=3.0),
)


def cells() -> list[GridCell]:
    return list(CELLS)


def cell_by_tag(tag: str) -> GridCell | None:
    for c in CELLS:
        if c.tag == tag:
            return c
    return None


def cell_exit_action(
    pos: dict[str, Any],
    price: float,
    now: datetime,
    *,
    peak_price: float | None = None,
    partial_done: bool = False,
) -> tuple[str, str] | None:
    """Return (kind, reason) where kind in {"partial","close"}; None = hold.

    Order: SL floor -> trailing -> partial TP -> full TP -> max_hold.
    Uses the position row's own tp/sl/hold + optional partial/trail fields.
    """
    entry = float(pos.get("entry_price") or 0.0)
    if entry <= 0 or price <= 0:
        return None
    sl = pos.get("sl_pct")
    if sl is not None and price <= entry * (1 - float(sl) / 100.0):
        return ("close", "stop_loss")
    trail = pos.get("trail_pct")
    if trail is not None:
        peak = max(float(peak_price or entry), entry)
        if peak > entry and price <= peak * (1 - float(trail) / 100.0):
            return ("close", "trailing_stop")
    ptp = pos.get("partial_tp_pct")
    tp = pos.get("tp_pct")
    if tp is not None and price >= entry * (1 + float(tp) / 100.0):
        return ("close", "take_profit")
    if ptp is not None and not partial_done and price >= entry * (1 + float(ptp) / 100.0):
        return ("partial", "partial_take_profit")
    opened = _parse_ts(pos.get("opened_at"))
    hold = pos.get("max_hold_min")
    if opened and hold is not None and now >= opened + timedelta(minutes=float(hold)):
        return ("close", "max_hold")
    return None


def _parse_ts(s: Any) -> Optional[datetime]:
    if not s:
        return None
    text = str(s).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def replay_path_exit(
    *,
    entry: float,
    size_usd: float,
    opened_at: Any,
    bars: list[tuple[Any, float]],
    tp_pct: float,
    sl_pct: float,
    max_hold_min: float,
) -> dict[str, Any] | None:
    """Coarse path replay on pair_history bars.

    Same-bar order: SL before TP (conservative, matches M2).
    PnL model: size_usd * (exit_mid / entry - 1) — M2 notional style.
    Returns None if no usable bar in the hold window.
    """
    if entry <= 0 or size_usd <= 0:
        return None
    opened = _parse_ts(opened_at)
    if opened is None:
        return None
    deadline = opened + timedelta(minutes=float(max_hold_min))
    tp_px = entry * (1.0 + float(tp_pct) / 100.0)
    sl_px = entry * (1.0 - float(sl_pct) / 100.0)

    last_bar: tuple[datetime, float] | None = None
    for raw_ts, raw_px in bars:
        ts = _parse_ts(raw_ts)
        try:
            px = float(raw_px)
        except (TypeError, ValueError):
            continue
        if ts is None or px <= 0:
            continue
        if ts < opened:
            continue
        if ts > deadline:
            break
        last_bar = (ts, px)
        # SL first, then TP
        if px <= sl_px:
            exit_px = px
            reason = "stop_loss"
            pnl = size_usd * (exit_px / entry - 1.0)
            return {
                "exit_price": exit_px,
                "exit_reason": reason,
                "closed_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "pnl_usd": pnl,
                "hold_min": (ts - opened).total_seconds() / 60.0,
            }
        if px >= tp_px:
            exit_px = px
            reason = "take_profit"
            pnl = size_usd * (exit_px / entry - 1.0)
            return {
                "exit_price": exit_px,
                "exit_reason": reason,
                "closed_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "pnl_usd": pnl,
                "hold_min": (ts - opened).total_seconds() / 60.0,
            }

    if last_bar is None:
        return None
    ts, px = last_bar
    # If last bar is before deadline but we have no post-deadline bar,
    # still close at max_hold using last observed mid (M2-style miss if empty).
    # Prefer exact max_hold close when we ran out of bars after deadline break.
    exit_px = px
    reason = "max_hold"
    # If we broke because ts > deadline, use last in-window bar already set.
    pnl = size_usd * (exit_px / entry - 1.0)
    hold = min((ts - opened).total_seconds() / 60.0, float(max_hold_min))
    return {
        "exit_price": exit_px,
        "exit_reason": reason,
        "closed_at": (opened + timedelta(minutes=hold)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pnl_usd": pnl,
        "hold_min": hold,
    }


def summarize_cell(
    tag: str,
    results: Iterable[dict[str, Any]],
    *,
    baseline_pnl: float | None = None,
    actual_pnl: float | None = None,
) -> dict[str, Any]:
    rows = [r for r in results if r is not None]
    n = len(rows)
    miss = sum(1 for r in results if r is None) if False else 0  # caller tracks miss
    pnls = [float(r["pnl_usd"]) for r in rows]
    total = sum(pnls) if pnls else 0.0
    reasons = {"take_profit": 0, "stop_loss": 0, "max_hold": 0}
    for r in rows:
        reasons[str(r.get("exit_reason") or "max_hold")] = (
            reasons.get(str(r.get("exit_reason") or "max_hold"), 0) + 1
        )
    neg = [p for p in pnls if p < 0]
    neg_sum = sum(neg) if neg else 0.0
    sl_neg = sum(
        float(r["pnl_usd"])
        for r in rows
        if r.get("exit_reason") == "stop_loss" and float(r["pnl_usd"]) < 0
    )
    sl_share_of_neg = (sl_neg / neg_sum) if neg_sum < 0 else None
    wins = sum(1 for p in pnls if p > 0)
    sl_peak = min(
        (float(r["pnl_usd"]) for r in rows if r.get("exit_reason") == "stop_loss"),
        default=None,
    )
    out: dict[str, Any] = {
        "tag": tag,
        "n": n,
        "total_pnl": total,
        "avg_pnl": (total / n) if n else None,
        "n_tp": reasons.get("take_profit", 0),
        "n_sl": reasons.get("stop_loss", 0),
        "n_mh": reasons.get("max_hold", 0),
        "win_rate": (wins / n) if n else None,
        "sl_neg_usd": sl_neg,
        "neg_sum_usd": neg_sum,
        "sl_share_of_neg": sl_share_of_neg,
        "sl_peak_usd": sl_peak,
    }
    if baseline_pnl is not None:
        out["delta_vs_baseline"] = total - baseline_pnl
    if actual_pnl is not None:
        out["delta_vs_actual"] = total - actual_pnl
    return out
