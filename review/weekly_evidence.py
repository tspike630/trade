"""10-19 weekly evidence from paper closes and execution exports.

Observation only. Does not change JEV gates, min_liq, cooldown, weights,
exit defaults, or circuit-breaker / PM wiring.

Stop-loss share matches dashboard / ``scripts.weekly_side_evidence``:
``min(1, |Σ stop_loss pnl| / |net pnl|)`` when the book is net-negative.
Daily opens use the Asia/Hong_Kong calendar on ``opened_at`` only.
F-1 is two lines that are never combined: decision ``up_1h`` and the
realized-PnL ledger from ``python -m execution.f1_streak_report``.
C3 prefers ``order_events`` ``shadow_c3``, then the ``shadow_c3`` table.
G-3 prefers a stored ruin count, then ``python -m execution.risk.bankruptcy``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from execution.f1_streak_report import load_ledger_groups
from execution.risk.bankruptcy import bankruptcy_report
from execution.streak import summarize_groups
from review.data import DbPaths, connect_ro, parse_ts, safe_float

# Observation thresholds for the Step2 veto line. Not JEV validity gates.
# Same cutoffs as scripts.weekly_side_evidence (PR #71). Do not import that
# module's constants into review.gates.
STOP_LOSS_SHARE_VETO = 0.50
DAILY_OPEN_PEAK_VETO = 20
STOP_LOSS_SHARE_FORMULA = (
    "min(1, |Σ pnl where exit_reason=stop_loss| / |Σ pnl|) when net realized "
    "PnL is negative and at least one stop_loss close exists; otherwise null. "
    "Dashboard stop_loss_share. A net-positive book does not flag."
)

LABEL = "不算有效性 / R2旁证"

_PEAK_KEYS = ("peak_price", "peak_px")
_GIVEBACK_KEYS = (
    "shadow_giveback",
    "shadow_giveback_usd",
    "giveback_usd",
    "giveback",
    "shadow_pnl",
)
_SKIP_EXPORT_NAMES = frozenset({"grok_context.json", "grok_report_state.json"})
_MAX_EXPORT_BYTES = 1_500_000
_MAX_EXPORT_FILES = 200

try:
    HK_TZ = ZoneInfo("Asia/Hong_Kong")
    HK_TZ_NAME = "Asia/Hong_Kong"
except ZoneInfoNotFoundError:  # pragma: no cover - tzdata missing
    HK_TZ = timezone(timedelta(hours=8))
    HK_TZ_NAME = "UTC+8"


@dataclass
class _Close:
    position_id: str
    strategy_id: str
    pnl_usd: Optional[float]
    exit_reason: str
    closed_at: str
    opened_at: Optional[str]
    entry_price: Optional[float]
    qty: Optional[float]
    size_usd: Optional[float]
    peak_price: Optional[float]
    giveback_usd: Optional[float]
    peak_field: Optional[str]
    giveback_field: Optional[str]


def compute_weekly_evidence(
    paths: DbPaths,
    *,
    period_start: str,
    period_end: str,
    strategy_id: Optional[str] = None,
    export_roots: Optional[list[Path]] = None,
    up_1h_rows: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Compute the 10-19 evidence block for one report window.

    ``period_end`` is exclusive. Open timestamps are bucketed on the
    Asia/Hong_Kong calendar. ``up_1h_rows`` are decision/signal rows
    (``fwd_1h``); they are not mixed into the realized-PnL streak.
    """
    start = parse_ts(period_start)
    end = parse_ts(period_end)
    closes, opens, schema = _load_paper(
        paths.paper, start, end, strategy_id=strategy_id
    )
    exports = _load_exports(paths.execution, export_roots or [])
    stop = _stop_loss_share(closes)
    daily = _daily_open_peak(opens, schema)
    streak = _f1_lines(
        up_1h_rows or [],
        paths.execution,
        strategy_id=strategy_id,
    )
    shadow = _c3_shadow(paths.execution)
    ruin = _g3_ruin(exports)
    veto = _step2_veto(stop, daily)
    return {
        "label": LABEL,
        "counts_as_validity": False,
        "period_start": period_start,
        "period_end": period_end,
        "tz": HK_TZ_NAME,
        "stop_loss": stop,
        "daily_open_peak": daily,
        "f1_streak": streak,
        "c3_shadow": shadow,
        "g3_ruin": ruin,
        "step2_veto": veto,
    }


def _parse_meta(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    if not text:
        return {}
    try:
        obj = json.loads(str(text))
    except (json.JSONDecodeError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _in_window(ts: Optional[str], start: datetime, end: datetime) -> bool:
    if not ts:
        return False
    try:
        dt = parse_ts(ts)
    except (TypeError, ValueError):
        return False
    return start <= dt < end


def _hk_day(ts: str) -> Optional[str]:
    try:
        dt = parse_ts(ts)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(HK_TZ).date().isoformat()


def _strategy_of(meta: dict[str, Any]) -> str:
    sid = meta.get("strategy_id")
    if sid is None:
        return "(unknown)"
    text = str(sid).strip()
    return text or "(unknown)"


def _first_key(meta: dict[str, Any], keys: tuple[str, ...]) -> tuple[Optional[str], Any]:
    for key in keys:
        if key in meta:
            return key, meta.get(key)
    extras = meta.get("extras")
    if isinstance(extras, dict):
        for key in keys:
            if key in extras:
                return f"extras.{key}", extras.get(key)
    return None, None


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _load_paper(
    path: str,
    start: datetime,
    end: datetime,
    *,
    strategy_id: Optional[str],
) -> tuple[list[_Close], list[tuple[str, str]], dict[str, bool]]:
    """Return (closes in window, opens as (day, strategy), schema flags)."""
    schema = {
        "paper_ok": False,
        "has_opened_at": False,
        "peak_field_present": False,
        "giveback_field_present": False,
    }
    try:
        conn = connect_ro(path)
    except (FileNotFoundError, sqlite3.Error):
        return [], [], schema
    try:
        if "positions" not in _table_names(conn):
            return [], [], schema
        schema["paper_ok"] = True
        cols = _columns(conn, "positions")
        schema["has_opened_at"] = "opened_at" in cols
        schema["peak_field_present"] = any(key in cols for key in _PEAK_KEYS)
        schema["giveback_field_present"] = any(key in cols for key in _GIVEBACK_KEYS)
        wanted = [
            name
            for name in (
                "id",
                "status",
                "pnl_usd",
                "exit_reason",
                "closed_at",
                "opened_at",
                "entry_price",
                "qty",
                "size_usd",
                "meta_json",
                *_PEAK_KEYS,
                *_GIVEBACK_KEYS,
            )
            if name in cols
        ]
        if "id" not in wanted:
            return [], [], schema
        rows = conn.execute(
            f"SELECT {', '.join(wanted)} FROM positions"
        ).fetchall()
    except sqlite3.Error:
        return [], [], schema
    finally:
        conn.close()

    closes: list[_Close] = []
    opens: list[tuple[str, str]] = []
    for row in rows:
        meta = _parse_meta(row["meta_json"] if "meta_json" in row.keys() else None)
        if not schema["peak_field_present"] or not schema["giveback_field_present"]:
            peak_name, _peak_raw = _first_key(meta, _PEAK_KEYS)
            give_name, _give_raw = _first_key(meta, _GIVEBACK_KEYS)
            if peak_name:
                schema["peak_field_present"] = True
            if give_name:
                schema["giveback_field_present"] = True
        sid = _strategy_of(meta)
        if strategy_id and sid != strategy_id:
            continue
        opened = str(row["opened_at"]) if "opened_at" in row.keys() and row["opened_at"] else None
        if opened and _in_window(opened, start, end):
            day = _hk_day(opened)
            if day:
                opens.append((day, sid))
        status = str(row["status"]).strip().lower() if "status" in row.keys() and row["status"] else ""
        closed_at = str(row["closed_at"]) if "closed_at" in row.keys() and row["closed_at"] else ""
        is_closed = status == "closed" or (not status and bool(closed_at))
        if not is_closed or not _in_window(closed_at, start, end):
            continue
        peak_field, peak_raw = _field_from_row(row, meta, _PEAK_KEYS)
        give_field, give_raw = _field_from_row(row, meta, _GIVEBACK_KEYS)
        closes.append(
            _Close(
                position_id=str(row["id"]),
                strategy_id=sid,
                pnl_usd=safe_float(row["pnl_usd"]) if "pnl_usd" in row.keys() else None,
                exit_reason=(
                    str(row["exit_reason"]).strip()
                    if "exit_reason" in row.keys() and row["exit_reason"] is not None
                    else ""
                ),
                closed_at=closed_at,
                opened_at=opened,
                entry_price=safe_float(row["entry_price"]) if "entry_price" in row.keys() else None,
                qty=safe_float(row["qty"]) if "qty" in row.keys() else None,
                size_usd=safe_float(row["size_usd"]) if "size_usd" in row.keys() else None,
                peak_price=safe_float(peak_raw),
                giveback_usd=safe_float(give_raw),
                peak_field=peak_field,
                giveback_field=give_field,
            )
        )
    return closes, opens, schema


def _field_from_row(
    row: sqlite3.Row,
    meta: dict[str, Any],
    keys: tuple[str, ...],
) -> tuple[Optional[str], Any]:
    names = set(row.keys())
    for key in keys:
        if key in names and row[key] is not None and str(row[key]).strip() != "":
            return key, row[key]
    return _first_key(meta, keys)


def _stop_loss_share(closes: list[_Close]) -> dict[str, Any]:
    """Dashboard / decision PR #71 share. Null unless the book is net-negative."""
    total = 0.0
    stop_net = 0.0
    n_pnl = 0
    saw_stop = False
    stops: list[_Close] = []
    for row in closes:
        if row.pnl_usd is None:
            continue
        n_pnl += 1
        total += row.pnl_usd
        if row.exit_reason == "stop_loss":
            stop_net += row.pnl_usd
            saw_stop = True
            stops.append(row)
    share: Optional[float] = None
    if saw_stop and total < 0 and abs(total) > 1e-9:
        share = min(1.0, abs(stop_net) / abs(total))
    worst: Optional[dict[str, Any]] = None
    if stops:
        worst_row = min(stops, key=lambda row: (row.pnl_usd or 0.0, row.closed_at, row.position_id))
        worst = {
            "position_id": worst_row.position_id,
            "strategy_id": worst_row.strategy_id,
            "pnl_usd": worst_row.pnl_usd,
            "closed_at": worst_row.closed_at,
            "exit_reason": worst_row.exit_reason,
        }
    note = None
    if not closes:
        note = "no closed paper trades in window"
    elif share is None:
        note = "net realized pnl is not negative, or no stop_loss close"
    return {
        "n_closed": n_pnl,
        "total_pnl_usd": total if n_pnl else None,
        "stop_loss_pnl_usd": stop_net if saw_stop else None,
        "loss_share": share,
        "worst_stop": worst,
        "definition": STOP_LOSS_SHARE_FORMULA,
        "note": note,
    }


def _daily_open_peak(
    opens: list[tuple[str, str]],
    schema: dict[str, bool],
) -> dict[str, Any]:
    """Peak opens on one Asia/Hong_Kong date. Source is ``opened_at`` only."""
    if not (schema.get("paper_ok") and schema.get("has_opened_at")):
        return {
            "source": "data_gap",
            "peak": None,
            "peak_day": None,
            "tz": HK_TZ_NAME,
            "n_opens": None,
            "by_day": {},
            "note": "opened_at missing; daily opens are not counted from a UTC date prefix",
        }
    return _peak_from_days(opens, source="paper.positions.opened_at")


def _peak_from_days(pairs: list[tuple[str, str]], *, source: str) -> dict[str, Any]:
    by_day: dict[str, int] = {}
    for day, _sid in pairs:
        by_day[day] = by_day.get(day, 0) + 1
    if not by_day:
        return {
            "source": source,
            "peak": 0,
            "peak_day": None,
            "tz": HK_TZ_NAME,
            "n_opens": 0,
            "by_day": {},
            "note": None,
        }
    # Same tie-break as scripts.weekly_side_evidence.max_daily_opens: later day wins.
    peak_day = max(by_day, key=lambda day: (by_day[day], day))
    return {
        "source": source,
        "peak": by_day[peak_day],
        "peak_day": peak_day,
        "tz": HK_TZ_NAME,
        "n_opens": sum(by_day.values()),
        "by_day": dict(sorted(by_day.items())),
        "note": None,
    }


def _f1_lines(
    up_1h_rows: list[dict[str, Any]],
    execution_path: str,
    *,
    strategy_id: Optional[str],
) -> dict[str, Any]:
    """Two F-1 lines. No combined P(3|2)."""
    return {
        "up_1h": _f1_up_1h(up_1h_rows, strategy_id=strategy_id),
        "realized_pnl": _f1_realized(execution_path, strategy_id=strategy_id),
    }


def _sid_text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "(unknown)"


def _up_1h_flag(row: dict[str, Any]) -> Optional[bool]:
    """Decision loss on ``fwd_1h`` only. Flat 0 is not a loss. Missing breaks the streak."""
    if "fwd_1h" not in row or row.get("fwd_1h") is None:
        return None
    value = safe_float(row.get("fwd_1h"))
    if value is None:
        return None
    return value < 0


def _decision_streak(flags: list[Optional[bool]]) -> dict[str, Any]:
    """Match ``scripts.weekly_side_evidence.streak_stats`` without mixing in realized pnl."""
    max_streak = 0
    current = 0
    trials = 0
    third_losses = 0
    for index, flag in enumerate(flags):
        if flag is None:
            current = 0
            continue
        if flag:
            current += 1
            if current > max_streak:
                max_streak = current
        else:
            current = 0
        if index >= 2 and flags[index - 2] is True and flags[index - 1] is True:
            trials += 1
            if flag:
                third_losses += 1
    return {
        "n_closed": len(flags),
        "n_resolved": sum(1 for flag in flags if flag is not None),
        "n_given_two_loss": trials,
        "n_third_loss": third_losses,
        "p_third_loss_given_two_loss": (third_losses / trials) if trials else None,
        "max_streak": max_streak,
    }


def _f1_up_1h(
    rows: list[dict[str, Any]],
    *,
    strategy_id: Optional[str],
) -> dict[str, Any]:
    scoped = [
        row
        for row in rows
        if isinstance(row, dict) and (strategy_id is None or _sid_text(row.get("strategy_id")) == strategy_id)
    ]
    resolved = any(_up_1h_flag(row) is not None for row in scoped)
    if not resolved:
        return {
            "status": "data_gap",
            "source": "decision_up_1h",
            "note": "up_1h unavailable",
            "by_strategy": {},
        }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in scoped:
        grouped.setdefault(_sid_text(row.get("strategy_id")), []).append(row)
    by_strategy: dict[str, Any] = {}
    for sid in sorted(grouped):
        ordered = sorted(
            grouped[sid],
            key=lambda row: (
                str(row.get("ts") or ""),
                row.get("decision_id") if isinstance(row.get("decision_id"), int) else 0,
                str(row.get("decision_id") or ""),
            ),
        )
        by_strategy[sid] = _decision_streak([_up_1h_flag(row) for row in ordered])
    return {
        "status": "ok",
        "source": "decision_up_1h",
        "note": (
            "up_1h<0 is a loss; flat 0 is not a loss; missing fwd_1h breaks the streak. "
            "Realized pnl is not mixed into this line."
        ),
        "by_strategy": by_strategy,
    }


def _f1_realized(execution_path: str, *, strategy_id: Optional[str]) -> dict[str, Any]:
    """``streak_events`` via ``load_ledger_groups``. Empty ledger is a data gap, not a paper replay."""
    gap = {
        "status": "data_gap",
        "source": "execution.f1_streak_report",
        "note": "streak_events missing or empty; realized PnL is not replayed from paper closes or up_1h",
        "by_strategy": {},
    }
    try:
        groups = load_ledger_groups(Path(execution_path))
    except sqlite3.Error:
        return gap
    if not groups:
        return gap
    if strategy_id:
        groups = {sid: pnls for sid, pnls in groups.items() if sid == strategy_id}
        if not groups:
            gap["note"] = f"no streak_events for strategy_id={strategy_id}"
            return gap
    summary = summarize_groups(groups)
    by_strategy: dict[str, Any] = {}
    for row in summary["by_strategy"]:
        by_strategy[str(row["strategy_id"])] = {
            "n_closed": row["n"],
            "n_resolved": row["n"],
            "n_given_two_loss": row["cond_n"],
            "n_third_loss": row["cond_hits"],
            "p_third_loss_given_two_loss": row["p_third_loss_given_streak_ge2"],
            "max_streak": row["max_streak"],
        }
    return {
        "status": "ok",
        "source": "execution.f1_streak_report",
        "note": (
            "streak_events in id order, same as python -m execution.f1_streak_report. "
            "pnl<0 is a loss; pnl==0 neither extends nor clears the streak and is not a third-loss hit. "
            "Per strategy only; the pooled micro-average is not reported."
        ),
        "by_strategy": by_strategy,
    }


def _iter_dicts(obj: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts(value, depth + 1)
        return
    if isinstance(obj, list):
        for item in obj[:400]:
            yield from _iter_dicts(item, depth + 1)


def _c3_shadow(execution_path: str) -> dict[str, Any]:
    """Prefer ``order_events`` shadow_c3, else the ``shadow_c3`` table. Never invent peaks."""
    event_rows = _c3_from_order_events(execution_path)
    if event_rows:
        return _c3_payload(event_rows, source="order_events.shadow_c3")
    table_rows = _c3_from_table(execution_path)
    if table_rows:
        return _c3_payload(
            table_rows,
            source="shadow_c3",
            note="order_events has no shadow_c3; read the shadow_c3 table",
        )
    return {
        "status": "data_gap",
        "source": None,
        "note": (
            "order_events shadow_c3 and the shadow_c3 table are empty; "
            "peaks not invented from paper closes"
        ),
        "n_rows": 0,
        "all": None,
        "max_hold": None,
    }


def _looks_like_shadow(item: dict[str, Any]) -> bool:
    return any(key in item for key in ("peak_pnl_pct", "giveback_from_peak_pct", "actual_pnl_usd"))


def _shadow_rows_from_obj(obj: Any, *, event: str) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        found: list[dict[str, Any]] = []
        for item in obj:
            found.extend(_shadow_rows_from_obj(item, event=event))
        return found
    if not isinstance(obj, dict):
        return []
    nested = obj.get("shadow_c3")
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict) and _looks_like_shadow(item)]
    if isinstance(nested, dict) and _looks_like_shadow(nested):
        return [nested]
    if event == "shadow_c3" and _looks_like_shadow(obj):
        return [obj]
    return []


def _c3_from_order_events(path: str) -> list[dict[str, Any]]:
    try:
        conn = connect_ro(path)
    except (FileNotFoundError, sqlite3.Error):
        return []
    try:
        if "order_events" not in _table_names(conn):
            return []
        cols = _columns(conn, "order_events")
        if "detail_json" not in cols:
            return []
        has_event = "event" in cols
        selected = "event, detail_json" if has_event else "detail_json"
        where = (
            "detail_json LIKE '%shadow_c3%' "
            "OR detail_json LIKE '%giveback_from_peak_pct%' "
            "OR detail_json LIKE '%peak_pnl_pct%'"
        )
        if has_event:
            where = f"event = 'shadow_c3' OR ({where})"
        try:
            rows = conn.execute(f"SELECT {selected} FROM order_events WHERE {where}").fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()
    found: list[dict[str, Any]] = []
    for row in rows:
        event = str(row["event"]) if has_event and row["event"] is not None else ""
        found.extend(_shadow_rows_from_obj(_maybe_json(row["detail_json"]), event=event))
    return found


def _c3_from_table(path: str) -> list[dict[str, Any]]:
    try:
        conn = connect_ro(path)
    except (FileNotFoundError, sqlite3.Error):
        return []
    try:
        if "shadow_c3" not in _table_names(conn):
            return []
        try:
            rows = conn.execute(
                """
                SELECT strategy_id, peak_pnl_pct, giveback_from_peak_pct,
                       actual_pnl_usd, actual_reason
                FROM shadow_c3
                ORDER BY id ASC
                """
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _mean_key(rows: list[dict[str, Any]], key: str) -> Optional[float]:
    values = [value for value in (safe_float(row.get(key)) for row in rows) if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _c3_cohort(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows),
        "mean_peak_pnl_pct": _mean_key(rows, "peak_pnl_pct"),
        "mean_giveback_from_peak_pct": _mean_key(rows, "giveback_from_peak_pct"),
        "mean_actual_pnl_usd": _mean_key(rows, "actual_pnl_usd"),
    }


def _c3_payload(
    rows: list[dict[str, Any]],
    *,
    source: str,
    note: Optional[str] = None,
) -> dict[str, Any]:
    hold = [row for row in rows if str(row.get("actual_reason") or "") == "max_hold"]
    return {
        "status": "ok",
        "source": source,
        "note": note,
        "n_rows": len(rows),
        "all": _c3_cohort(rows),
        "max_hold": _c3_cohort(hold),
    }


def _g3_ruin(exports: list[dict[str, Any]]) -> dict[str, Any]:
    found: list[tuple[str, int, str]] = []
    for item in exports:
        for blob in _iter_dicts(item["obj"]):
            ruin = _ruin_from_dict(blob)
            if ruin is None:
                continue
            found.append((str(item.get("sort") or ""), ruin, str(item.get("source") or "")))
            break
    if found:
        _sort_key, ruin, source = max(found, key=lambda item: (item[0], item[2]))
        return {
            "status": "ok",
            "kind": "export",
            "ruin_count": ruin,
            "source": source,
            "n_observations": len(found),
            "formula": "floor(cash/per_trade_risk)",
            "note": None,
            "line": f"G-3 ruin count: floor(cash/per_trade_risk)={ruin}（{source}）",
        }
    return _g3_cli()


def _g3_cli() -> dict[str, Any]:
    """Theoretical CLI defaults. Not an observed book count, and not a changed risk gate."""
    try:
        row = bankruptcy_report()
    except (TypeError, ValueError):
        return {
            "status": "data_gap",
            "kind": "data_gap",
            "ruin_count": None,
            "source": None,
            "n_observations": 0,
            "note": "execution.risk.bankruptcy report failed",
            "line": "G-3 ruin count: data gap — bankruptcy CLI failed",
        }
    ruin = int(row["max_sequential_losses"])
    return {
        "status": "ok",
        "kind": "cli_defaults",
        "ruin_count": ruin,
        "source": "execution.risk.bankruptcy",
        "n_observations": 0,
        "formula": "floor(capital/per_trade_risk); per_trade_risk=size_usd*(sl_pct/100)",
        "capital": row["capital"],
        "size_usd": row["size_usd"],
        "sl_pct": row["sl_pct"],
        "per_trade_risk": row["per_trade_risk"],
        "note": "theoretical CLI defaults, not an observed book count",
        "line": (
            f"G-3 ruin count: floor(capital/per_trade_risk)={ruin} "
            f"（execution.risk.bankruptcy CLI defaults; not an observed book）"
        ),
    }


def _ruin_from_dict(blob: dict[str, Any]) -> Optional[int]:
    for key in ("max_sequential_losses", "floor_cash_per_trade_risk", "g3_ruin_count"):
        if key in blob:
            value = safe_float(blob.get(key))
            if value is not None:
                return int(math.floor(value))
    risk = safe_float(blob.get("per_trade_risk"))
    cash = safe_float(blob.get("cash_usd"))
    if cash is None:
        cash = safe_float(blob.get("cash"))
    if risk is not None and risk > 0 and cash is not None:
        return int(math.floor(cash / risk))
    return None


def _fmt_share(share: Optional[float]) -> str:
    if share is None:
        return "—"
    return f"{share * 100:.1f}%"


def _step2_veto(stop: dict[str, Any], daily: dict[str, Any]) -> dict[str, Any]:
    share = stop.get("loss_share")
    peak = daily.get("peak")
    reasons: list[str] = []
    share_hit = isinstance(share, (int, float)) and not isinstance(share, bool) and float(share) >= STOP_LOSS_SHARE_VETO
    peak_hit = isinstance(peak, (int, float)) and not isinstance(peak, bool) and float(peak) >= DAILY_OPEN_PEAK_VETO
    if share_hit:
        reasons.append("stop_loss_share")
    if peak_hit:
        reasons.append("daily_opens")
    share_s = _fmt_share(float(share) if isinstance(share, (int, float)) and not isinstance(share, bool) else None)
    peak_s = "—" if peak is None else str(int(peak))
    if share_hit and peak_hit:
        line = (
            f"Step2 veto 旁证：止损亏损占比 {share_s} ≥50%，且单日开仓峰值 {peak_s} ≥20，"
            f"即使 1h≥55% 也不启动 Step2。{LABEL}。"
        )
    elif share_hit:
        line = (
            f"Step2 veto 旁证：止损亏损占比 {share_s} ≥50%，"
            f"即使 1h≥55% 也不启动 Step2。{LABEL}。"
        )
    elif peak_hit:
        line = (
            f"Step2 veto 旁证：单日开仓峰值 {peak_s} ≥20，"
            f"即使 1h≥55% 也不启动 Step2。{LABEL}。"
        )
    else:
        line = (
            f"Step2 veto 旁证：未触发（止损亏损占比 {share_s}，单日开仓峰值 {peak_s}）。"
            f"{LABEL}。"
        )
    return {
        "triggered": bool(reasons),
        "reasons": reasons,
        "label": LABEL,
        "thresholds": {
            "stop_loss_loss_share": STOP_LOSS_SHARE_VETO,
            "daily_open_peak": DAILY_OPEN_PEAK_VETO,
        },
        "line": line,
        "note": "observation only; does not change JEV gates, min_liq, cooldown, or weights",
    }


def _load_exports(execution_path: str, roots: list[Path]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    found.extend(_load_execution_blobs(execution_path))
    found.extend(_load_export_files(roots))
    return found


def _maybe_json(text: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if not text:
        return None
    try:
        return json.loads(str(text))
    except (json.JSONDecodeError, TypeError):
        return None


def _load_execution_blobs(path: str) -> list[dict[str, Any]]:
    try:
        conn = connect_ro(path)
    except (FileNotFoundError, sqlite3.Error):
        return []
    found: list[dict[str, Any]] = []
    try:
        tables = _table_names(conn)
        if "order_events" in tables:
            cols = _columns(conn, "order_events")
            if "detail_json" in cols:
                ts_col = "event_ts" if "event_ts" in cols else None
                id_col = "id" if "id" in cols else "rowid"
                selected = f"{id_col} AS id, detail_json"
                if ts_col:
                    selected += f", {ts_col} AS event_ts"
                try:
                    rows = conn.execute(
                        f"SELECT {selected} FROM order_events "
                        "WHERE detail_json LIKE '%streak_observe%' "
                        "OR detail_json LIKE '%per_trade_risk%' "
                        "OR detail_json LIKE '%floor_cash_per_trade_risk%' "
                        "OR detail_json LIKE '%g3_ruin_count%' "
                        "OR detail_json LIKE '%max_sequential_losses%'"
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
                for row in rows:
                    obj = _maybe_json(row["detail_json"])
                    if obj is None:
                        continue
                    sort = str(row["event_ts"]) if ts_col and row["event_ts"] else ""
                    found.append(
                        {
                            "source": f"order_events:{row['id']}",
                            "sort": sort,
                            "obj": obj,
                        }
                    )
        if "execution_log" in tables:
            cols = _columns(conn, "execution_log")
            if "receipt_json" in cols:
                id_col = "id" if "id" in cols else "rowid"
                ts_sel = ", ts" if "ts" in cols else ""
                try:
                    rows = conn.execute(
                        f"SELECT {id_col} AS id, receipt_json{ts_sel} FROM execution_log "
                        "WHERE receipt_json LIKE '%streak_observe%' "
                        "OR receipt_json LIKE '%per_trade_risk%' "
                        "OR receipt_json LIKE '%floor_cash_per_trade_risk%' "
                        "OR receipt_json LIKE '%g3_ruin_count%' "
                        "OR receipt_json LIKE '%max_sequential_losses%'"
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
                for row in rows:
                    obj = _maybe_json(row["receipt_json"])
                    if obj is None:
                        continue
                    sort = str(row["ts"]) if "ts" in row.keys() and row["ts"] else ""
                    found.append(
                        {
                            "source": f"execution_log.receipt:{row['id']}",
                            "sort": sort,
                            "obj": obj,
                        }
                    )
    finally:
        conn.close()
    return found


def _load_export_files(roots: list[Path]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            seen += 1
            if seen > _MAX_EXPORT_FILES:
                return found
            if path.name in _SKIP_EXPORT_NAMES:
                continue
            try:
                if path.stat().st_size > _MAX_EXPORT_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not any(
                mark in text
                for mark in (
                    "streak_observe",
                    "per_trade_risk",
                    "floor_cash_per_trade_risk",
                    "g3_ruin_count",
                    "max_sequential_losses",
                )
            ):
                continue
            obj = _maybe_json(text)
            if obj is None:
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            found.append({"source": str(path), "sort": mtime, "obj": obj})
    return found
