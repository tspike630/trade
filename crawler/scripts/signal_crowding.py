#!/usr/bin/env python3
"""Same-window signal crowding for one strategy. Observe only.

INTEL_LOOP item 6 (同类策略): how bunched ``jev_strength_v1`` buys are
inside the weekly check window. ``purpose=live_candidate`` and
``purpose=sample`` are labeled separately and never pooled.

The window matches ``weekly_side_evidence``: inclusive
``[as_of - window_days, as_of]``, with HKT calendar days and clock hours.
This module does not read the paper ledger, does not call RiskManager,
does not fuse, does not demote a strategy mid-session, and does not veto
Step 2. Monday 2026-10-19 may cite the numbers as 旁证. Turning them into
a gate is an R2/R3 decision.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

HK_TZ = timezone(timedelta(hours=8))
CALENDAR_TZ_NAME = "HKT (UTC+8)"
DEFAULT_WINDOW_DAYS = 7

LABEL_LIVE = "live_candidate"
LABEL_SAMPLE = "sample"
LABEL_MISSING = "purpose_missing"

# Always present in the report, even when that producer wrote zero buys.
FIXED_LABELS = (LABEL_LIVE, LABEL_SAMPLE)

# Trend CSV cells. One weekly row, so only the two labeled producers.
TREND_KEYS = (
    "crowd_lc_n",
    "crowd_lc_buys_per_hkt_day",
    "crowd_lc_unique_per_buy",
    "crowd_lc_peak_hour_density",
    "crowd_lc_peak_day_buys",
    "crowd_sample_n",
    "crowd_sample_buys_per_hkt_day",
    "crowd_sample_unique_per_buy",
    "crowd_sample_peak_hour_density",
    "crowd_sample_peak_day_buys",
)


def parse_ts(value: Any) -> Optional[datetime]:
    """ISO timestamp to aware UTC. Trailing Z is accepted. Naive means UTC."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def window_bounds(as_of: datetime, window_days: int) -> tuple[datetime, datetime]:
    """Inclusive UTC bounds. Same formula as weekly hard side-evidence."""
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    end = _as_utc(as_of)
    start = end - timedelta(days=window_days)
    return start, end


def purpose_label(purpose: Any) -> str:
    """Strip only. Do not case-fold. Blank is ``purpose_missing``.

    ``live_candidate`` and ``sample`` stay themselves. Any other non-blank
    purpose is its own label so it is not merged into those two.
    """
    if purpose is None:
        return LABEL_MISSING
    text = str(purpose).strip()
    if not text:
        return LABEL_MISSING
    return text


def hkt_day(value: Any) -> Optional[str]:
    dt = parse_ts(value)
    if dt is None:
        return None
    return dt.astimezone(HK_TZ).strftime("%Y-%m-%d")


def hkt_hour(value: Any) -> Optional[str]:
    """Clock hour in HKT, ``YYYY-MM-DD HH`` (00–23)."""
    dt = parse_ts(value)
    if dt is None:
        return None
    return dt.astimezone(HK_TZ).strftime("%Y-%m-%d %H")


@dataclass(frozen=True)
class CrowdBuy:
    """One strategy buy. ``purpose`` is already stripped, or None."""

    ts: str
    purpose: Optional[str]
    chain_id: str
    token_address: str


@dataclass(frozen=True)
class PurposeCrowd:
    label: str
    n_buys: int
    window_days: int
    buys_per_hkt_day: Optional[float]
    unique_tokens: int
    unique_per_buy: Optional[float]
    peak_hour: Optional[str]
    peak_hour_buys: int
    peak_hour_density: Optional[float]
    peak_day: Optional[str]
    peak_day_buys: int
    day_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CrowdingReport:
    available: bool
    note: str
    strategy_id: str
    window_days: int
    window_start: str
    window_end: str
    calendar_tz: str
    n_bad_ts: int
    cohorts: tuple[PurposeCrowd, ...]

    def cohort(self, label: str) -> Optional[PurposeCrowd]:
        for row in self.cohorts:
            if row.label == label:
                return row
        return None


def _peak(counts: Mapping[str, int]) -> tuple[Optional[str], int]:
    """Highest count. Ties take the later label (ISO day / hour sorts)."""
    if not counts:
        return None, 0
    key = max(counts, key=lambda name: (counts[name], name))
    return key, counts[key]


def cohort_metrics(
    label: str,
    rows: Sequence[CrowdBuy],
    *,
    window_days: int,
) -> PurposeCrowd:
    """Three crowding readings for one labeled purpose.

    ``buys_per_hkt_day`` divides by the window length, so a quiet day is 0.
    ``unique_per_buy`` is distinct ``(chain_id, token_address)`` / buys.
    ``peak_hour_density`` is the busiest HKT clock hour's share of buys.
    Empty input leaves the ratios null. ``buys_per_hkt_day`` is 0.
    """
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    n = len(rows)
    tokens = {(row.chain_id, row.token_address) for row in rows}
    hours: dict[str, int] = {}
    days: dict[str, int] = {}
    for row in rows:
        hour = hkt_hour(row.ts)
        day = hkt_day(row.ts)
        if hour is not None:
            hours[hour] = hours.get(hour, 0) + 1
        if day is not None:
            days[day] = days.get(day, 0) + 1
    peak_hour, peak_hour_buys = _peak(hours)
    peak_day, peak_day_buys = _peak(days)
    day_counts = tuple(sorted(days.items()))
    return PurposeCrowd(
        label=label,
        n_buys=n,
        window_days=window_days,
        buys_per_hkt_day=(n / window_days),
        unique_tokens=len(tokens),
        unique_per_buy=(len(tokens) / n) if n else None,
        peak_hour=peak_hour,
        peak_hour_buys=peak_hour_buys,
        peak_hour_density=(peak_hour_buys / n) if n else None,
        peak_day=peak_day,
        peak_day_buys=peak_day_buys,
        day_counts=day_counts,
    )


def observe(
    buys: Sequence[CrowdBuy],
    *,
    as_of: datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    strategy_id: str,
    available: bool = True,
    note: str = "",
) -> CrowdingReport:
    """Bucket in-window buys by purpose label. Out-of-window rows drop."""
    start, end = window_bounds(as_of, window_days)
    grouped: dict[str, list[CrowdBuy]] = {}
    n_bad = 0
    for buy in buys:
        dt = parse_ts(buy.ts)
        if dt is None:
            n_bad += 1
            continue
        if dt < start or dt > end:
            continue
        label = purpose_label(buy.purpose)
        grouped.setdefault(label, []).append(buy)
    labels = list(FIXED_LABELS)
    for extra in sorted(grouped):
        if extra not in FIXED_LABELS and grouped[extra]:
            labels.append(extra)
    cohorts = tuple(
        cohort_metrics(label, grouped.get(label, ()), window_days=window_days)
        for label in labels
    )
    if not note:
        note = "decision_log buys in the weekly window" if available else "unavailable"
    return CrowdingReport(
        available=available,
        note=note,
        strategy_id=strategy_id,
        window_days=window_days,
        window_start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        calendar_tz=CALENDAR_TZ_NAME,
        n_bad_ts=n_bad,
        cohorts=cohorts,
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _parse_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text_cell(row: sqlite3.Row, name: str) -> Optional[str]:
    if name not in row.keys() or row[name] is None:
        return None
    return str(row[name])


def load_strategy_buys(conn: sqlite3.Connection, strategy_id: str) -> list[CrowdBuy]:
    """Buy rows for one strategy. Purpose column is optional on old DBs.

    Column ``purpose`` wins over ``raw_json``. Chain and token come from
    their columns when the schema has them, else from ``raw_json``.
    """
    have = _table_columns(conn, "decision_log")
    if "ts" not in have or "strategy_id" not in have or "action" not in have:
        return []
    cols = ["ts", "strategy_id", "action"]
    for optional in ("purpose", "raw_json", "chain_id", "token_address"):
        if optional in have:
            cols.append(optional)
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM decision_log "
        "WHERE action = 'buy' AND strategy_id = ? ORDER BY ts",
        (strategy_id,),
    ).fetchall()
    out: list[CrowdBuy] = []
    for row in rows:
        raw = _parse_meta(row["raw_json"]) if "raw_json" in row.keys() else {}
        purpose = _text_cell(row, "purpose")
        if purpose is not None:
            purpose = purpose.strip() or None
        if purpose is None:
            top = raw.get("purpose")
            if isinstance(top, str) and top.strip():
                purpose = top.strip()
        if "chain_id" in row.keys():
            chain = str(row["chain_id"] or "")
        else:
            chain = str(raw.get("chain_id") or "")
        if "token_address" in row.keys():
            token = str(row["token_address"] or "")
        else:
            token = str(raw.get("token_address") or "")
        out.append(
            CrowdBuy(
                ts=str(row["ts"] or ""),
                purpose=purpose,
                chain_id=chain,
                token_address=token,
            )
        )
    return out


def unavailable_report(
    *,
    as_of: datetime,
    window_days: int,
    strategy_id: str,
    note: str,
) -> CrowdingReport:
    safe_days = window_days if window_days >= 1 else 1
    report = observe(
        (),
        as_of=as_of,
        window_days=safe_days,
        strategy_id=strategy_id,
        available=False,
        note=note,
    )
    return report


def compute_for_weekly(
    *,
    decision_conn: Optional[sqlite3.Connection],
    strategy_id: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
    as_of: Optional[datetime] = None,
) -> CrowdingReport:
    """One Monday pass. A read error does not raise and does not veto."""
    moment = _as_utc(as_of or datetime.now(timezone.utc))
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    if decision_conn is None:
        return unavailable_report(
            as_of=moment,
            window_days=window_days,
            strategy_id=strategy_id,
            note="decision db not open",
        )
    try:
        buys = load_strategy_buys(decision_conn, strategy_id)
    except sqlite3.Error as exc:
        return unavailable_report(
            as_of=moment,
            window_days=window_days,
            strategy_id=strategy_id,
            note=f"decision_log read failed ({type(exc).__name__})",
        )
    return observe(
        buys,
        as_of=moment,
        window_days=window_days,
        strategy_id=strategy_id,
    )


def _fmt_rate(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.4f}"


def _fmt_csv(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.6f}"


def render_markdown(report: CrowdingReport) -> list[str]:
    """Section pasted into the weekly validity report. Does not veto."""
    lines: list[str] = []
    a = lines.append
    a("## 信号拥挤度（只观察）")
    a("")
    a(
        "INTEL_LOOP ⑥ 同类策略。同一窗口里 "
        f"`{report.strategy_id}` 的 `action=buy`（`decision_log`）。"
        "`purpose=live_candidate` 与 `purpose=sample` **分开标注，不合并**。"
        "其它 purpose 各自成行，不并进这两列。"
        "不套用流动性 / 覆盖率 / 价格过滤，也不按 strength 再切：拥挤看的是已经写入的 buy。"
    )
    a("")
    a(
        f"窗口 `{report.window_start}` → `{report.window_end}`"
        f"（{report.window_days} 天，两端含，与硬旁证同一时钟）。"
        f"日与小时按 **{report.calendar_tz}**。"
    )
    a("")
    a(
        "- **buys / HKT day** = n_buys / 窗口天数。没有 buy 的日子算 0，"
        "所以分母不是「有信号的天数」。"
    )
    a(
        "- **unique tokens / buys** = distinct `(chain_id, token_address)` / n_buys。"
        "1 表示每笔不同币；越低越挤在少数币上。"
    )
    a(
        "- **peak hour density** = 最密的一个 HKT 时钟小时里的 buy 数 / n_buys。"
        "1 表示全部落在同一小时。并列时取较晚的小时。"
    )
    a("")
    a(
        "**10-19 只作旁证，不算有效性。** 不改 RiskManager，不自动熔断，"
        "不在盘中降级，**不否决 Step2**。Step2 仍只由硬旁证（#71：止损占亏≥50% "
        "或单日开仓≥20）否决。激活留 R2/R3。候选 ≠ 自动激活。"
    )
    a("")
    if not report.available:
        a(f"**拥挤度未形成：** {report.note}。不把缺数写成 0，也不据此否决。")
        a("")
        return lines
    if report.n_bad_ts:
        a(f"无法解析时间戳、未计入窗口的 buy：{report.n_bad_ts}。")
        a("")
    a(
        "| purpose | n | buys/HKT day | unique | unique/buys | "
        "peak hour (HKT) | peak hour n | density | peak day | peak day n |"
    )
    a(
        "|---------|---|--------------|--------|-------------|"
        "-----------------|-------------|---------|----------|------------|"
    )
    for row in report.cohorts:
        hour = row.peak_hour or "—"
        day = row.peak_day or "—"
        a(
            f"| `{row.label}` | {row.n_buys} | {_fmt_rate(row.buys_per_hkt_day)} | "
            f"{row.unique_tokens} | {_fmt_rate(row.unique_per_buy)} | {hour} | "
            f"{row.peak_hour_buys} | {_fmt_rate(row.peak_hour_density)} | "
            f"{day} | {row.peak_day_buys} |"
        )
    a("")
    day_rows = [
        (row.label, day, count)
        for row in report.cohorts
        for day, count in row.day_counts
    ]
    if day_rows:
        a("HKT 日计数（只列有 buy 的日子）：")
        a("")
        a("| purpose | HKT day | buys |")
        a("|---------|---------|------|")
        for label, day, count in day_rows:
            a(f"| `{label}` | {day} | {count} |")
        a("")
    return lines


def _cohort_cells(row: Optional[PurposeCrowd], prefix: str) -> dict[str, str]:
    if row is None:
        return {
            f"{prefix}_n": "",
            f"{prefix}_buys_per_hkt_day": "",
            f"{prefix}_unique_per_buy": "",
            f"{prefix}_peak_hour_density": "",
            f"{prefix}_peak_day_buys": "",
        }
    return {
        f"{prefix}_n": str(row.n_buys),
        f"{prefix}_buys_per_hkt_day": _fmt_csv(row.buys_per_hkt_day),
        f"{prefix}_unique_per_buy": _fmt_csv(row.unique_per_buy),
        f"{prefix}_peak_hour_density": _fmt_csv(row.peak_hour_density),
        f"{prefix}_peak_day_buys": str(row.peak_day_buys),
    }


def trend_fields(report: Optional[CrowdingReport]) -> dict[str, str]:
    """CSV cells. Unavailable stays blank, not zero. No veto flag."""
    blank = {key: "" for key in TREND_KEYS}
    if report is None or not report.available:
        return blank
    cells = _cohort_cells(report.cohort(LABEL_LIVE), "crowd_lc")
    cells.update(_cohort_cells(report.cohort(LABEL_SAMPLE), "crowd_sample"))
    return cells


def format_stdout(report: CrowdingReport) -> list[str]:
    if not report.available:
        return [f"拥挤: 未形成（{report.note}）只观察，不否决 Step2"]
    bits: list[str] = []
    for row in report.cohorts:
        bits.append(
            f"{row.label} n={row.n_buys} "
            f"buys/day={_fmt_rate(row.buys_per_hkt_day)} "
            f"uniq/buy={_fmt_rate(row.unique_per_buy)} "
            f"peak_hour_density={_fmt_rate(row.peak_hour_density)}"
        )
    return ["拥挤: " + " | ".join(bits) + " （只观察，不否决 Step2）"]
