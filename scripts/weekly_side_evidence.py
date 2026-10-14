#!/usr/bin/env python3
"""Weekly hard side-evidence and F-1 streak observation.

Read-only. Monday ``scripts/jev_validity.py`` calls this against the paper
ledger the execution layer already writes (``paper_trading.db`` positions).
It does not open a new database, does not change cooldown / liquidity /
strength defaults, and does not pause RiskManager or the position manager.

Hard side-evidence (P-3) can veto a Step-2 promote. F-1 only records
P(3rd loss | 2 consecutive losses). A fuse stays a later R2/R3 decision.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# Weekly check labels times in HKT. Daily open counts use the same calendar.
HK_TZ = timezone(timedelta(hours=8))
CALENDAR_TZ_NAME = "HKT (UTC+8)"

# P-3 hard side-evidence. Either flag blocks Step-2 promote.
STOP_LOSS_SHARE_MIN = 0.50
DAILY_OPENS_MIN = 20
DEFAULT_WINDOW_DAYS = 7

# Paper gateway executes this purpose. Sampling rows are not this cohort.
PAPER_PURPOSE = "live_candidate"

# Same definition as dashboard PaperClosedReport.stop_loss_share
# (2026-10-14: stop_loss ≈ 96% of a net-negative realized book).
STOP_LOSS_SHARE_FORMULA = (
    "stop_loss_pnl_share = min(1, |Σ pnl where exit_reason=stop_loss| / |Σ pnl|) "
    "when the window's net realized PnL is negative and at least one stop_loss "
    "close exists; otherwise null. This is dashboard stop_loss_share: the "
    "stop_loss bucket (net, any sign) over the net realized book, capped at 1. "
    "A net-positive book or a book with no stop_loss close does not flag."
)

_SOURCE_KEYS = ("signal_source_id", "signal_source", "source_id")


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


def calendar_day_hkt(value: Any) -> Optional[str]:
    dt = parse_ts(value)
    if dt is None:
        return None
    return dt.astimezone(HK_TZ).strftime("%Y-%m-%d")


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _decision_id(raw: Any) -> Optional[int]:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        return int(raw)
    text = str(raw).strip()
    if text.isdigit():
        return int(text)
    return None


@dataclass(frozen=True)
class PaperPosition:
    """One paper ``positions`` row. Open rows have no realized pnl."""

    opened_at: Optional[str]
    closed_at: Optional[str]
    status: Optional[str]
    exit_reason: Optional[str]
    pnl_usd: Optional[float]
    decision_log_id: Optional[int]
    strategy_id: Optional[str]


@dataclass(frozen=True)
class HardSide:
    available: bool
    formula: str
    window_days: int
    window_start: str
    window_end: str
    calendar_tz: str
    n_closed: int
    n_opens: int
    stop_loss_pnl_usd: Optional[float]
    total_pnl_usd: Optional[float]
    stop_loss_pnl_share: Optional[float]
    max_daily_opens: Optional[int]
    max_daily_opens_day: Optional[str]
    flag_stop_loss_share: bool
    flag_daily_opens: bool
    step2_blocked: bool
    note: str

    def flag_map(self) -> dict[str, bool]:
        return {
            "hard_旁证_stop_loss_share": self.flag_stop_loss_share,
            "hard_旁证_daily_opens": self.flag_daily_opens,
        }


@dataclass(frozen=True)
class SignalOutcome:
    """One strategy buy, in time order, with whatever loss label exists."""

    decision_id: int
    ts: str
    strategy_id: str
    purpose: Optional[str]
    signal_source_id: Optional[str]
    ret_1h: Optional[float]
    realized_pnl: Optional[float]
    paper_executed: bool

    def loss_flag(self) -> Optional[bool]:
        """True loss, False non-loss, None unresolved (breaks a streak).

        ``up_1h < 0`` (``ret_1h < 0``) wins when the forward return exists.
        Flat (0) is not a loss. Realized ``pnl < 0`` is the fallback when
        the 1h return was never joined. Missing both is unresolved.
        """
        if self.ret_1h is not None:
            return self.ret_1h < 0
        if self.realized_pnl is not None:
            return self.realized_pnl < 0
        return None


@dataclass(frozen=True)
class F1Group:
    strategy_id: str
    signal_source_id: Optional[str]
    cohort: str
    n_resolved: int
    n_unresolved: int
    max_streak: int
    trials_after_two: int
    third_losses: int
    p_third_given_two: Optional[float]


@dataclass(frozen=True)
class WeeklySide:
    hard: HardSide
    f1: tuple[F1Group, ...]
    f1_primary: Optional[F1Group]


def stop_loss_pnl_share(
    positions: Sequence[PaperPosition],
) -> tuple[Optional[float], Optional[float], float, int]:
    """Return (share, stop_loss_net_usd, total_pnl_usd, n_closed_with_pnl).

    Matches dashboard ``stop_loss_share``: null unless some close is
    ``exit_reason=stop_loss`` and net realized PnL is negative.
    """
    total = 0.0
    stop = 0.0
    n_pnl = 0
    saw_stop = False
    for pos in positions:
        pnl = pos.pnl_usd
        if pnl is None:
            continue
        n_pnl += 1
        total += pnl
        if (pos.exit_reason or "").strip() == "stop_loss":
            stop += pnl
            saw_stop = True
    stop_net = stop if saw_stop else None
    if (
        stop_net is None
        or not (total < 0 and abs(total) > 1e-9)
    ):
        return None, stop_net, total, n_pnl
    return min(1.0, abs(stop_net) / abs(total)), stop_net, total, n_pnl


def max_daily_opens(
    opened_ats: Sequence[Any],
) -> tuple[int, Optional[str]]:
    """Peak same-calendar-day open count. Calendar is HKT."""
    counts: dict[str, int] = {}
    for raw in opened_ats:
        day = calendar_day_hkt(raw)
        if day is None:
            continue
        counts[day] = counts.get(day, 0) + 1
    if not counts:
        return 0, None
    day = max(counts, key=lambda key: (counts[key], key))
    return counts[day], day


def hard_side_from_positions(
    positions: Sequence[PaperPosition],
    *,
    as_of: datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    available: bool = True,
    note: str = "",
) -> HardSide:
    """Windowed P-3 flags. ``as_of`` is inclusive; start is ``as_of - window``."""
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    end = _as_utc(as_of)
    start = end - timedelta(days=window_days)
    closed: list[PaperPosition] = []
    opens: list[Any] = []
    for pos in positions:
        opened = parse_ts(pos.opened_at)
        if opened is not None and start <= opened <= end:
            opens.append(pos.opened_at)
        if not _is_closed(pos):
            continue
        closed_at = parse_ts(pos.closed_at)
        if closed_at is not None and start <= closed_at <= end:
            closed.append(pos)
    share, stop_net, total_pnl, n_pnl = stop_loss_pnl_share(closed)
    peak, peak_day = max_daily_opens(opens)
    flag_stop = share is not None and share >= STOP_LOSS_SHARE_MIN
    flag_opens = peak >= DAILY_OPENS_MIN
    if not note:
        note = (
            "paper positions in the window"
            if available
            else "paper ledger unavailable"
        )
    return HardSide(
        available=available,
        formula=STOP_LOSS_SHARE_FORMULA,
        window_days=window_days,
        window_start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        calendar_tz=CALENDAR_TZ_NAME,
        n_closed=n_pnl,
        n_opens=len(opens),
        stop_loss_pnl_usd=stop_net if n_pnl else None,
        total_pnl_usd=total_pnl if n_pnl else None,
        stop_loss_pnl_share=share,
        max_daily_opens=peak,
        max_daily_opens_day=peak_day,
        flag_stop_loss_share=flag_stop,
        flag_daily_opens=flag_opens,
        step2_blocked=bool(flag_stop or flag_opens),
        note=note,
    )


def _is_closed(pos: PaperPosition) -> bool:
    status = (pos.status or "").strip().lower()
    if status == "open":
        return False
    if status == "closed":
        return True
    return bool((pos.closed_at or "").strip())


def unavailable_hard(
    *,
    as_of: datetime,
    window_days: int,
    note: str,
) -> HardSide:
    end = _as_utc(as_of)
    start = end - timedelta(days=max(window_days, 1))
    return HardSide(
        available=False,
        formula=STOP_LOSS_SHARE_FORMULA,
        window_days=window_days,
        window_start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        calendar_tz=CALENDAR_TZ_NAME,
        n_closed=0,
        n_opens=0,
        stop_loss_pnl_usd=None,
        total_pnl_usd=None,
        stop_loss_pnl_share=None,
        max_daily_opens=None,
        max_daily_opens_day=None,
        flag_stop_loss_share=False,
        flag_daily_opens=False,
        step2_blocked=False,
        note=note,
    )


def streak_stats(flags: Sequence[Optional[bool]]) -> tuple[int, int, int, Optional[float]]:
    """Max loss streak, trials of (2 losses then a 3rd), 3rd-loss count, P.

    ``None`` is unresolved: it breaks the streak and is not a win or a loss.
    A trial is every index i whose previous two flags are losses and whose
    own flag is resolved. Windows overlap. P is null when there is no trial.
    """
    max_streak = 0
    current = 0
    trials = 0
    third_losses = 0
    for i, flag in enumerate(flags):
        if flag is None:
            current = 0
            continue
        if flag:
            current += 1
            if current > max_streak:
                max_streak = current
        else:
            current = 0
        if i >= 2 and flags[i - 2] is True and flags[i - 1] is True:
            trials += 1
            if flag:
                third_losses += 1
    p = (third_losses / trials) if trials else None
    return max_streak, trials, third_losses, p


def select_cohort(
    rows: Sequence[SignalOutcome],
) -> tuple[list[SignalOutcome], str]:
    """Prefer buys paper actually opened, else live_candidate, else all."""
    executed = [row for row in rows if row.paper_executed]
    if executed:
        return executed, "paper_executed"
    live = [row for row in rows if (row.purpose or "") == PAPER_PURPOSE]
    if live:
        return live, "purpose=live_candidate"
    return list(rows), "strategy_buys"


def observe_streaks(
    rows: Sequence[SignalOutcome],
    *,
    strategy_id: Optional[str] = None,
) -> tuple[F1Group, ...]:
    """Per strategy_id, plus per signal source when one is present.

    The strategy row (``signal_source_id is None``) is the weekly headline.
    Source rows do not replace it. Order is time, then decision id.
    """
    scoped = [row for row in rows if strategy_id is None or row.strategy_id == strategy_id]
    strategies = sorted({row.strategy_id for row in scoped})
    groups: list[F1Group] = []
    for sid in strategies:
        bucket = [row for row in scoped if row.strategy_id == sid]
        groups.append(_one_group(bucket, sid, None))
        sources = sorted({row.signal_source_id for row in bucket if row.signal_source_id})
        for source in sources:
            sub = [row for row in bucket if row.signal_source_id == source]
            groups.append(_one_group(sub, sid, source))
    return tuple(groups)


def _one_group(
    rows: Sequence[SignalOutcome],
    strategy_id: str,
    signal_source_id: Optional[str],
) -> F1Group:
    ordered = sorted(rows, key=lambda row: (row.ts or "", row.decision_id))
    cohort_rows, cohort = select_cohort(ordered)
    flags = [row.loss_flag() for row in cohort_rows]
    max_streak, trials, third_losses, p = streak_stats(flags)
    return F1Group(
        strategy_id=strategy_id,
        signal_source_id=signal_source_id,
        cohort=cohort,
        n_resolved=sum(1 for flag in flags if flag is not None),
        n_unresolved=sum(1 for flag in flags if flag is None),
        max_streak=max_streak,
        trials_after_two=trials,
        third_losses=third_losses,
        p_third_given_two=p,
    )


def primary_f1(groups: Sequence[F1Group], strategy_id: str) -> Optional[F1Group]:
    for group in groups:
        if group.strategy_id == strategy_id and group.signal_source_id is None:
            return group
    for group in groups:
        if group.signal_source_id is None:
            return group
    return groups[0] if groups else None


def resolve_paper_db(explicit: Optional[Path], repo: Path) -> Optional[Path]:
    """Paper ledger path. Does not create the file."""
    if explicit is not None and str(explicit).strip():
        path = Path(str(explicit)).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    for env_name in ("EXECUTION_PAPER_DB", "PAPER_DB_PATH", "DASHBOARD_PAPER_DB"):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (Path.cwd() / path).resolve()
    for cand in (repo / "paper_trading.db", repo / "crawler" / "paper_trading.db"):
        if cand.is_file():
            return cand.resolve()
    return (repo / "paper_trading.db").resolve()


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


def load_paper_positions(path: Path) -> list[PaperPosition]:
    """Read-only positions. Missing file or table raises FileNotFoundError / sqlite3.Error."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        have = _table_columns(conn, "positions")
        if not have:
            raise sqlite3.OperationalError("positions table missing")
        wanted = (
            "opened_at",
            "closed_at",
            "status",
            "exit_reason",
            "pnl_usd",
            "meta_json",
        )
        selected = [name for name in wanted if name in have]
        if "opened_at" not in selected and "closed_at" not in selected:
            raise sqlite3.OperationalError("positions has no opened_at/closed_at")
        rows = conn.execute(
            f"SELECT {', '.join(selected)} FROM positions"
        ).fetchall()
    finally:
        conn.close()
    out: list[PaperPosition] = []
    for row in rows:
        meta = _parse_meta(row["meta_json"] if "meta_json" in row.keys() else None)
        out.append(
            PaperPosition(
                opened_at=_cell(row, "opened_at"),
                closed_at=_cell(row, "closed_at"),
                status=_cell(row, "status"),
                exit_reason=_cell(row, "exit_reason"),
                pnl_usd=_finite(row["pnl_usd"]) if "pnl_usd" in row.keys() else None,
                decision_log_id=_decision_id(meta.get("decision_log_id")),
                strategy_id=(
                    str(meta.get("strategy_id")).strip()
                    if meta.get("strategy_id") not in (None, "")
                    else None
                ),
            )
        )
    return out


def _cell(row: sqlite3.Row, name: str) -> Optional[str]:
    if name not in row.keys() or row[name] is None:
        return None
    return str(row[name])


def load_decision_buys(
    conn: sqlite3.Connection,
    strategy_id: str,
) -> list[dict[str, Any]]:
    """Buy rows for one strategy. Purpose column is optional on old DBs."""
    have = _table_columns(conn, "decision_log")
    if "id" not in have or "strategy_id" not in have:
        return []
    cols = ["id", "ts", "strategy_id", "action"]
    for optional in ("purpose", "raw_json", "reason"):
        if optional in have:
            cols.append(optional)
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM decision_log "
        "WHERE action = 'buy' AND strategy_id = ? ORDER BY ts, id",
        (strategy_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        raw: dict[str, Any] = {}
        if "raw_json" in row.keys() and row["raw_json"]:
            raw = _parse_meta(row["raw_json"])
        purpose = None
        if "purpose" in row.keys() and row["purpose"] not in (None, ""):
            purpose = str(row["purpose"]).strip()
        if not purpose:
            top = raw.get("purpose")
            if isinstance(top, str) and top.strip():
                purpose = top.strip()
        out.append(
            {
                "id": int(row["id"]),
                "ts": str(row["ts"] or ""),
                "strategy_id": str(row["strategy_id"] or strategy_id),
                "purpose": purpose,
                "signal_source_id": _signal_source_id(raw),
            }
        )
    return out


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _signal_source_id(raw: Mapping[str, Any]) -> Optional[str]:
    extras = raw.get("extras")
    bags: list[Mapping[str, Any]] = [raw]
    if isinstance(extras, dict):
        bags.append(extras)
    for key in _SOURCE_KEYS:
        for bag in bags:
            value = bag.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def join_outcomes(
    buys: Sequence[Mapping[str, Any]],
    positions: Sequence[PaperPosition],
    ret_1h_by_id: Mapping[int, Optional[float]],
) -> list[SignalOutcome]:
    """Attach forward 1h (when the weekly join has it) and paper realized pnl."""
    by_decision: dict[int, PaperPosition] = {}
    for pos in positions:
        did = pos.decision_log_id
        if did is None:
            continue
        prev = by_decision.get(did)
        if prev is None or _prefer_position(pos, prev):
            by_decision[did] = pos
    out: list[SignalOutcome] = []
    for buy in buys:
        did = int(buy["id"])
        pos = by_decision.get(did)
        realized = None
        executed = pos is not None
        if pos is not None and _is_closed(pos):
            realized = pos.pnl_usd
        ret = ret_1h_by_id.get(did)
        out.append(
            SignalOutcome(
                decision_id=did,
                ts=str(buy.get("ts") or ""),
                strategy_id=str(buy.get("strategy_id") or ""),
                purpose=_opt_str(buy.get("purpose")),
                signal_source_id=_opt_str(buy.get("signal_source_id")),
                ret_1h=_finite(ret) if ret is not None else None,
                realized_pnl=realized,
                paper_executed=executed,
            )
        )
    return out


def _prefer_position(new: PaperPosition, old: PaperPosition) -> bool:
    """Keep a closed row with pnl over an open stub for the same decision."""
    new_closed = _is_closed(new) and new.pnl_usd is not None
    old_closed = _is_closed(old) and old.pnl_usd is not None
    if new_closed and not old_closed:
        return True
    return False


def compute_for_weekly(
    *,
    decision_conn: Optional[sqlite3.Connection],
    ret_1h_by_id: Mapping[int, Optional[float]],
    strategy_id: str,
    paper_db: Optional[Path],
    window_days: int = DEFAULT_WINDOW_DAYS,
    as_of: Optional[datetime] = None,
) -> WeeklySide:
    """One Monday pass. Missing paper DB does not veto and does not raise."""
    moment = _as_utc(as_of or datetime.now(timezone.utc))
    positions: list[PaperPosition] = []
    if paper_db is None:
        hard = unavailable_hard(
            as_of=moment,
            window_days=window_days,
            note="paper db path unresolved",
        )
    elif not paper_db.is_file():
        hard = unavailable_hard(
            as_of=moment,
            window_days=window_days,
            note=f"paper db missing ({paper_db.name})",
        )
    else:
        try:
            positions = load_paper_positions(paper_db)
        except (OSError, sqlite3.Error) as exc:
            hard = unavailable_hard(
                as_of=moment,
                window_days=window_days,
                note=f"paper read failed ({type(exc).__name__})",
            )
        else:
            hard = hard_side_from_positions(
                positions,
                as_of=moment,
                window_days=window_days,
            )
    buys: list[dict[str, Any]] = []
    if decision_conn is not None:
        try:
            buys = load_decision_buys(decision_conn, strategy_id)
        except sqlite3.Error:
            buys = []
    outcomes = join_outcomes(buys, positions, ret_1h_by_id)
    groups = observe_streaks(outcomes, strategy_id=strategy_id)
    return WeeklySide(
        hard=hard,
        f1=groups,
        f1_primary=primary_f1(groups, strategy_id),
    )


def _fmt_share(share: Optional[float]) -> str:
    if share is None:
        return "—"
    return f"{share * 100:.1f}%"


def _fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.4f}"


def _fmt_p(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.4f}"


def render_markdown(side: WeeklySide) -> list[str]:
    """Sections pasted into the weekly validity report."""
    lines: list[str] = []
    a = lines.append
    hard = side.hard
    flags = hard.flag_map()
    a("## P-3 硬旁证（可否决 Step2）")
    a("")
    a(
        "这不是 WARNING 段落。`unique_token≥10`、`n≥15`、连续两周 1h≥55% 仍是样本门槛；"
        "下面任一硬旁证为真，Step2 推荐 **不可启动 Step2**，即使那些门槛已过。"
        "临时 `live_candidate` @ 0.15 **不算有效性**。不改 cooldown / min_liq / strength / 出场。"
    )
    a("")
    a(f"**公式：** {hard.formula}")
    a("")
    a(
        f"窗口 `{hard.window_start}` → `{hard.window_end}`（{hard.window_days} 天，两端含）。"
        f"日开仓日历是 **{hard.calendar_tz}**。"
        "冷却键是 `(strategy_id, chain_id, token_address)`，所以 3600s **不**限制跨票日开仓；"
        "G-1 用这里的开仓计数。账本是 paper `positions`（执行 tick 把 `exit_reason` 写在这里，不写 `execution_log`）。"
    )
    a("")
    if not hard.available:
        a(f"**硬旁证未形成：** {hard.note}。不据此否决，也不把缺数当成通过。")
        a("")
    a("| 指标 | 值 |")
    a("|------|----|")
    a(f"| 已平仓计入亏损簿 n | {hard.n_closed} |")
    a(f"| Σ pnl（exit_reason=stop_loss，净额） | {_fmt_usd(hard.stop_loss_pnl_usd)} |")
    a(f"| Σ pnl（窗口已平仓净额） | {_fmt_usd(hard.total_pnl_usd)} |")
    a(f"| stop_loss_pnl_share | {_fmt_share(hard.stop_loss_pnl_share)} |")
    a(
        f"| `hard_旁证_stop_loss_share`（≥{STOP_LOSS_SHARE_MIN:.0%}） | "
        f"{'true' if flags['hard_旁证_stop_loss_share'] else 'false'} |"
    )
    a(f"| 窗口内开仓 n | {hard.n_opens} |")
    peak = "—" if hard.max_daily_opens is None else str(hard.max_daily_opens)
    peak_day = hard.max_daily_opens_day or "—"
    a(f"| max_daily_opens | {peak}（{peak_day}） |")
    a(
        f"| `hard_旁证_daily_opens`（≥{DAILY_OPENS_MIN}） | "
        f"{'true' if flags['hard_旁证_daily_opens'] else 'false'} |"
    )
    a("")
    if hard.step2_blocked:
        a("**Step2：不可启动 Step2。** 硬旁证否决。")
    elif hard.available:
        a("**Step2 硬旁证：** 未触发。样本门槛仍按决策表，本段不额外放行。")
    else:
        a("**Step2 硬旁证：** 未计算，不否决。")
    a("")
    a("## F-1 信号连亏（只记不熔断）")
    a("")
    a(
        "同一信号源连续亏损只观察。亏损定义：`up_1h < 0`（`ret_1h < 0`）；"
        "没有 1h 收益时，用已平仓 `pnl_usd < 0`。未结算（两者都缺）打断连亏，不算赢也不算亏。"
        "队列优先 paper 实际开过的 buy（`positions.meta_json.decision_log_id`），"
        f"否则 `purpose={PAPER_PURPOSE}`，否则该策略全部 buy。"
        "P(第3笔亏 | 连续2笔亏) 用重叠窗口：每个「前两笔都亏、第3笔已结算」计一次试验。"
    )
    a("")
    a(
        "**纸面阶段只记不熔断。** 不调用 RiskManager，不暂停 PM。"
        "熔断留 R2/R3。F-1 用该策略全部已读 buy 的时间序，不裁成上面的 7 天窗口，避免把跨周连亏拆开。"
    )
    a("")
    if not side.f1:
        a("_无 buy 可观察。_")
        a("")
        return lines
    a("| strategy | source | cohort | n_resolved | unresolved | max_streak | trials | third_losses | p_third |")
    a("|----------|--------|--------|------------|------------|------------|--------|--------------|---------|")
    for group in side.f1:
        source = group.signal_source_id or "—"
        a(
            f"| `{group.strategy_id}` | {source} | {group.cohort} | "
            f"{group.n_resolved} | {group.n_unresolved} | {group.max_streak} | "
            f"{group.trials_after_two} | {group.third_losses} | "
            f"{_fmt_p(group.p_third_given_two)} |"
        )
    a("")
    return lines


def trend_fields(side: Optional[WeeklySide]) -> dict[str, str]:
    """CSV cells. Flags are 0/1. Missing share stays blank, not zero."""
    blank = {
        "stop_loss_pnl_share": "",
        "max_daily_opens": "",
        "hard_旁证_stop_loss_share": "0",
        "hard_旁证_daily_opens": "0",
        "step2_blocked": "0",
        "f1_max_streak": "",
        "f1_trials_after_two": "",
        "f1_p_third_given_two": "",
        "side_evidence": "skipped",
    }
    if side is None:
        return blank
    hard = side.hard
    if not hard.available:
        blank["side_evidence"] = "unavailable"
        return blank
    share = "" if hard.stop_loss_pnl_share is None else f"{hard.stop_loss_pnl_share:.6f}"
    opens = "" if hard.max_daily_opens is None else str(hard.max_daily_opens)
    primary = side.f1_primary
    p_text = ""
    streak = ""
    trials = ""
    if primary is not None:
        streak = str(primary.max_streak)
        trials = str(primary.trials_after_two)
        if primary.p_third_given_two is not None:
            p_text = f"{primary.p_third_given_two:.6f}"
    return {
        "stop_loss_pnl_share": share,
        "max_daily_opens": opens,
        "hard_旁证_stop_loss_share": "1" if hard.flag_stop_loss_share else "0",
        "hard_旁证_daily_opens": "1" if hard.flag_daily_opens else "0",
        "step2_blocked": "1" if hard.step2_blocked else "0",
        "f1_max_streak": streak,
        "f1_trials_after_two": trials,
        "f1_p_third_given_two": p_text,
        "side_evidence": "ok",
    }


def format_stdout(side: WeeklySide) -> list[str]:
    hard = side.hard
    flags = hard.flag_map()
    share = "—" if hard.stop_loss_pnl_share is None else f"{hard.stop_loss_pnl_share:.4f}"
    opens = "—" if hard.max_daily_opens is None else str(hard.max_daily_opens)
    lines = [
        (
            "硬旁证: "
            f"stop_loss_pnl_share={share} "
            f"hard_旁证_stop_loss_share={int(flags['hard_旁证_stop_loss_share'])} "
            f"max_daily_opens={opens} "
            f"hard_旁证_daily_opens={int(flags['hard_旁证_daily_opens'])} "
            f"available={int(hard.available)}"
        )
    ]
    if hard.step2_blocked:
        lines.append("Step2: 不可启动 Step2（硬旁证否决）")
    elif hard.available:
        lines.append("Step2: 硬旁证未触发")
    else:
        lines.append(f"Step2: 硬旁证未形成（{hard.note}）")
    primary = side.f1_primary
    if primary is None:
        lines.append("F-1: 无样本（只记不熔断）")
    else:
        p = "—" if primary.p_third_given_two is None else f"{primary.p_third_given_two:.4f}"
        lines.append(
            "F-1: "
            f"cohort={primary.cohort} max_streak={primary.max_streak} "
            f"trials={primary.trials_after_two} P(3|2)={p} （只记不熔断）"
        )
    return lines
