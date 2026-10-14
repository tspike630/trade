"""10-19 weekly evidence from closed paper trades and execution exports.

Observation only. Does not change JEV gates, min_liq, cooldown, weights,
or position-manager / circuit-breaker wiring.

Stop-loss loss share is the fraction of *negative* realized PnL that came
from ``exit_reason=stop_loss``. F-1 matches the dashboard streak definition
(``pnl_usd > 0`` is a win; anything else is a loss) when no ``streak_observe``
export is present. Peak prices and ruin counts are reported only when the
fields are actually stored.
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

from review.data import DbPaths, connect_ro, parse_ts, safe_float

# Observation thresholds for the Step2 veto line. Not JEV validity gates.
STOP_LOSS_SHARE_VETO = 0.50
DAILY_OPEN_PEAK_VETO = 20

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
) -> dict[str, Any]:
    """Compute the 10-19 evidence block for one report window.

    ``period_end`` is exclusive. Timestamps are interpreted as UTC and
    bucketed onto the Asia/Hong_Kong calendar when a zone is available.
    """
    start = parse_ts(period_start)
    end = parse_ts(period_end)
    closes, opens, schema = _load_paper(
        paths.paper, start, end, strategy_id=strategy_id
    )
    exports = _load_exports(paths.execution, export_roots or [])
    stop = _stop_loss_share(closes)
    daily = _daily_open_peak(
        paths,
        opens,
        schema,
        start,
        end,
        strategy_id=strategy_id,
    )
    streak = _f1_streak(closes, exports, strategy_id=strategy_id)
    shadow = _c3_shadow(closes, schema)
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
    negative = [row for row in closes if row.pnl_usd is not None and row.pnl_usd < 0]
    neg_sum = sum(row.pnl_usd or 0.0 for row in negative)
    sl_neg = [row for row in negative if row.exit_reason == "stop_loss"]
    sl_sum = sum(row.pnl_usd or 0.0 for row in sl_neg)
    share = (sl_sum / neg_sum) if neg_sum < 0 else None
    stops = [row for row in closes if row.exit_reason == "stop_loss" and row.pnl_usd is not None]
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
        note = "no negative realized pnl"
    return {
        "n_closed": len(closes),
        "n_negative": len(negative),
        "negative_pnl_usd": neg_sum if negative else None,
        "stop_loss_negative_pnl_usd": sl_sum if sl_neg else (0.0 if negative else None),
        "loss_share": share,
        "worst_stop": worst,
        "definition": (
            "abs(sum of pnl<0 on exit_reason=stop_loss) / abs(sum of all pnl<0)"
        ),
        "note": note,
    }


def _daily_open_peak(
    paths: DbPaths,
    opens: list[tuple[str, str]],
    schema: dict[str, bool],
    start: datetime,
    end: datetime,
    *,
    strategy_id: Optional[str],
) -> dict[str, Any]:
    if schema.get("paper_ok") and schema.get("has_opened_at"):
        return _peak_from_days(opens, source="paper.positions.opened_at")
    executed = _executed_entries(paths.execution, start, end, strategy_id=strategy_id)
    if executed is None:
        return {
            "source": "data_gap",
            "peak": None,
            "peak_day": None,
            "tz": HK_TZ_NAME,
            "n_opens": None,
            "by_day": {},
            "note": "paper opened_at missing and execution executed entries unavailable",
        }
    payload = _peak_from_days(executed, source="execution_log.executed")
    if not schema.get("paper_ok"):
        payload["note"] = "paper db unread; counted executed entries"
    else:
        payload["note"] = "opened_at column missing; counted executed entries"
    return payload


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
    peak = max(by_day.values())
    peak_day = min(day for day, count in by_day.items() if count == peak)
    return {
        "source": source,
        "peak": peak,
        "peak_day": peak_day,
        "tz": HK_TZ_NAME,
        "n_opens": sum(by_day.values()),
        "by_day": dict(sorted(by_day.items())),
        "note": None,
    }


def _executed_entries(
    path: str,
    start: datetime,
    end: datetime,
    *,
    strategy_id: Optional[str],
) -> Optional[list[tuple[str, str]]]:
    try:
        conn = connect_ro(path)
    except (FileNotFoundError, sqlite3.Error):
        return None
    try:
        if "execution_log" not in _table_names(conn):
            return None
        cols = _columns(conn, "execution_log")
        if "ts" not in cols or "executed" not in cols:
            return None
        selected = ["ts", "executed"]
        if "strategy_id" in cols:
            selected.append("strategy_id")
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM execution_log WHERE executed = 1"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    out: list[tuple[str, str]] = []
    for row in rows:
        ts = str(row["ts"] or "")
        if not _in_window(ts, start, end):
            continue
        sid = "(unknown)"
        if "strategy_id" in row.keys() and row["strategy_id"]:
            sid = str(row["strategy_id"])
        if strategy_id and sid != strategy_id:
            continue
        day = _hk_day(ts)
        if day:
            out.append((day, sid))
    return out


def _is_loss(pnl: Optional[float]) -> bool:
    """Dashboard F-1: a win is pnl_usd > 0. Zero and missing count as losses."""
    return not (pnl is not None and pnl > 0)


def _streak_from_pnls(pnls: list[Optional[float]]) -> dict[str, Any]:
    bits = [0 if _is_loss(pnl) else 1 for pnl in pnls]
    after: list[int] = []
    for index in range(len(bits) - 2):
        if bits[index] == 0 and bits[index + 1] == 0:
            after.append(bits[index + 2])
    n_third = sum(1 for bit in after if bit == 0)
    n_given = len(after)
    return {
        "n_closed": len(pnls),
        "n_given_two_loss": n_given,
        "n_third_loss": n_third,
        "p_third_loss_given_two_loss": (n_third / n_given) if n_given else None,
    }


def _close_sort_key(row: _Close) -> tuple[datetime, str]:
    try:
        closed = parse_ts(row.closed_at)
    except (TypeError, ValueError):
        closed = datetime.min.replace(tzinfo=timezone.utc)
    return closed, row.position_id


def _f1_from_closes(closes: list[_Close]) -> dict[str, Any]:
    grouped: dict[str, list[_Close]] = {}
    for row in closes:
        grouped.setdefault(row.strategy_id, []).append(row)
    by_strategy: dict[str, Any] = {}
    for sid, rows in sorted(grouped.items()):
        ordered = sorted(rows, key=_close_sort_key)
        by_strategy[sid] = _streak_from_pnls([row.pnl_usd for row in ordered])
    return {
        "source": "closed_trades",
        "note": "no streak_observe export; pnl>0 is a win, else a loss (dashboard F-1)",
        "by_strategy": by_strategy,
    }


def _f1_streak(
    closes: list[_Close],
    exports: list[dict[str, Any]],
    *,
    strategy_id: Optional[str],
) -> dict[str, Any]:
    stats_payloads: list[tuple[str, dict[str, Any]]] = []
    event_rows: list[dict[str, Any]] = []
    for item in exports:
        for payload in _streak_payloads(item["obj"]):
            kind, body = _classify_streak(payload)
            if kind == "stats" and isinstance(body, dict):
                stats_payloads.append((str(item.get("sort") or ""), body))
            elif kind == "events" and isinstance(body, list):
                event_rows.extend(body)
    if stats_payloads:
        _sort_key, stats = max(stats_payloads, key=lambda pair: pair[0])
        if strategy_id:
            stats = {sid: row for sid, row in stats.items() if sid == strategy_id}
        return {
            "source": "streak_observe",
            "note": "read from execution/dashboard export or order event",
            "by_strategy": stats,
        }
    if event_rows:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in event_rows:
            sid = str(row.get("strategy_id") or "(unknown)")
            if strategy_id and sid != strategy_id:
                continue
            grouped.setdefault(sid, []).append(row)
        by_strategy = {}
        for sid, rows in sorted(grouped.items()):
            ordered = sorted(rows, key=lambda row: (str(row.get("closed_at") or row.get("ts") or ""), str(row.get("position_id") or "")))
            pnls = [_event_pnl(row) for row in ordered]
            by_strategy[sid] = _streak_from_pnls(pnls)
        return {
            "source": "streak_observe",
            "note": "computed from streak_observe trade rows",
            "by_strategy": by_strategy,
        }
    computed = _f1_from_closes(closes)
    if strategy_id:
        computed["by_strategy"] = {
            sid: row
            for sid, row in computed["by_strategy"].items()
            if sid == strategy_id
        }
    return computed


def _event_pnl(row: dict[str, Any]) -> Optional[float]:
    if "is_loss" in row and "pnl_usd" not in row:
        return -1.0 if bool(row.get("is_loss")) else 1.0
    return safe_float(row.get("pnl_usd"))


def _classify_streak(payload: Any) -> tuple[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("by_strategy"), dict):
        payload = payload["by_strategy"]
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) for item in payload):
            if any("pnl_usd" in item or "is_loss" in item for item in payload):
                return "events", payload
            stats: dict[str, Any] = {}
            for item in payload:
                sid = str(item.get("strategy_id") or "").strip()
                if sid:
                    stats[sid] = _normalize_streak_stat(item)
            if stats:
                return "stats", stats
        return "none", None
    if isinstance(payload, dict):
        stats = {}
        for sid, item in payload.items():
            if sid in {"source", "note", "as_of", "ts"}:
                continue
            if isinstance(item, dict):
                stats[str(sid)] = _normalize_streak_stat(item)
        if stats:
            return "stats", stats
    return "none", None


def _normalize_streak_stat(item: dict[str, Any]) -> dict[str, Any]:
    n_given = _first_num(item, ("n_given_two_loss", "cond_n", "n_opportunities", "n"))
    n_third = _first_num(item, ("n_third_loss", "third_losses"))
    prob = _first_num(
        item,
        ("p_third_loss_given_two_loss", "cond_third_loss_rate", "p", "probability"),
    )
    n_closed = _first_num(item, ("n_closed", "closed"))
    n_given_i = int(n_given) if n_given is not None else None
    n_third_i = int(n_third) if n_third is not None else None
    if prob is None and n_given_i and n_third_i is not None and n_given_i > 0:
        prob = n_third_i / n_given_i
    return {
        "n_closed": int(n_closed) if n_closed is not None else None,
        "n_given_two_loss": n_given_i,
        "n_third_loss": n_third_i,
        "p_third_loss_given_two_loss": prob,
    }


def _first_num(item: dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    for key in keys:
        if key in item:
            return safe_float(item.get(key))
    return None


def _streak_payloads(obj: Any) -> list[Any]:
    found: list[Any] = []
    for blob in _iter_dicts(obj):
        if "streak_observe" in blob:
            found.append(blob["streak_observe"])
    return found


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


def _peak_mfe_usd(row: _Close) -> Optional[float]:
    if row.peak_price is None or row.entry_price is None or row.entry_price <= 0:
        return None
    if row.size_usd is not None:
        return (row.peak_price - row.entry_price) / row.entry_price * row.size_usd
    if row.qty is not None:
        return (row.peak_price - row.entry_price) * row.qty
    return None


def _giveback_usd(row: _Close, mfe: Optional[float]) -> Optional[float]:
    if row.giveback_usd is not None:
        return row.giveback_usd
    if mfe is not None and row.pnl_usd is not None:
        return mfe - row.pnl_usd
    return None


def _cohort(rows: list[_Close]) -> dict[str, Any]:
    mfes: list[float] = []
    pnls: list[float] = []
    givebacks: list[float] = []
    for row in rows:
        mfe = _peak_mfe_usd(row)
        if mfe is not None:
            mfes.append(mfe)
        if row.pnl_usd is not None:
            pnls.append(row.pnl_usd)
        giveback = _giveback_usd(row, mfe)
        if giveback is not None and (row.giveback_usd is not None or mfe is not None):
            givebacks.append(giveback)
    return {
        "n": len(rows),
        "n_with_peak_mfe": len(mfes),
        "n_with_giveback": len(givebacks),
        "peak_mfe_usd_sum": sum(mfes) if mfes else None,
        "exit_pnl_usd_sum": sum(pnls) if pnls else None,
        "giveback_usd_sum": sum(givebacks) if givebacks else None,
    }


def _c3_shadow(closes: list[_Close], schema: dict[str, bool]) -> dict[str, Any]:
    usable = [
        row
        for row in closes
        if _peak_mfe_usd(row) is not None or row.giveback_usd is not None
    ]
    present = bool(schema.get("peak_field_present") or schema.get("giveback_field_present"))
    if not usable:
        if present:
            note = "peak_price / shadow giveback present but empty; peaks not invented"
        else:
            note = "peak_price / shadow giveback fields missing on closes; peaks not invented"
        return {
            "status": "data_gap",
            "note": note,
            "n_closed": len(closes),
            "all": None,
            "max_hold": None,
        }
    max_hold = [row for row in usable if row.exit_reason == "max_hold"]
    basis = "shadow_field" if any(row.giveback_usd is not None for row in usable) else "peak_mfe_minus_exit_pnl"
    if any(row.giveback_usd is not None for row in usable) and any(
        row.peak_price is not None and row.giveback_usd is None for row in usable
    ):
        basis = "mixed"
    return {
        "status": "ok",
        "note": None,
        "giveback_basis": basis,
        "n_closed": len(closes),
        "n_with_shadow": len(usable),
        "all": _cohort(usable),
        "max_hold": _cohort(max_hold),
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
    if not found:
        return {
            "status": "data_gap",
            "ruin_count": None,
            "source": None,
            "n_observations": 0,
            "note": "execution export floor(cash/per_trade_risk) not present",
            "line": "G-3 ruin count: data gap — execution 未导出 floor(cash/per_trade_risk)",
        }
    _sort_key, ruin, source = max(found, key=lambda item: (item[0], item[2]))
    return {
        "status": "ok",
        "ruin_count": ruin,
        "source": source,
        "n_observations": len(found),
        "formula": "floor(cash/per_trade_risk)",
        "note": None,
        "line": f"G-3 ruin count: floor(cash/per_trade_risk)={ruin}（{source}）",
    }


def _ruin_from_dict(blob: dict[str, Any]) -> Optional[int]:
    for key in ("floor_cash_per_trade_risk", "g3_ruin_count"):
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
                        "OR detail_json LIKE '%g3_ruin_count%'"
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
                        "OR receipt_json LIKE '%g3_ruin_count%'"
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
