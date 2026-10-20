"""Sole SQLite access for the dashboard.

Every connection uses ``file:...?mode=ro`` plus ``PRAGMA query_only=ON``.
Write statements are rejected before they reach SQLite. Missing files and
tables return :class:`DataResult` instead of raising.
"""

from __future__ import annotations

import csv
import importlib.util
import logging
import re
import sqlite3
import statistics
import json
import sys
import time
import types
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Iterator, Literal, assert_never
from urllib.parse import quote

from execution.slippage_report import build_report as build_slippage_report

from dashboard.parsing import (
    clip,
    extract_coverage,
    extract_jev_strength,
    extract_liquidity_usd,
    extract_pair,
    extract_symbol,
    liq_bucket_label,
    parse_json,
    parse_ts,
    receipt_summary,
    rejection_key,
    safe_float,
    validation_error_text,
)
from dashboard.settings import (
    EXECUTION_DB_ENV,
    _ReceiptFlags,
    _as_bool,
    _parse_utc,
    db_root,
    gateway_stale_seconds,
    resolve_db_file,
    stale_seconds,
)
from dashboard.format_util import Liveness

log = logging.getLogger("dashboard.data")

_TABLES = frozenset(
    {
        "decision_log",
        "indicator_history",
        "pair_history",
        "jev_signals",
        "token_indicators",
        "tokens",
        "run_checkpoint",
        "execution_log",
        "account",
        "positions",
        "runs",
        "news_events",
    }
)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Word boundaries do not match the prefix of updated_at / created_at.
_WRITE_RE = re.compile(
    r"(?i)\b(insert|update|delete|replace|alter|drop|create|attach|detach|vacuum|reindex)\b"
)
_CLOSED_STATUS = {"closed", "close", "cancelled", "canceled", "liquidated", "expired"}
_POSITION_FIELDS = (
    "id",
    "chain_id",
    "pair_address",
    "token_address",
    "qty",
    "entry_price",
    "size_usd",
    "opened_at",
    "open_ts",
    "status",
    "tp_pct",
    "sl_pct",
    "max_hold_min",
    "closed_at",
    "exit_price",
    "pnl_usd",
)


@dataclass(frozen=True)
class DbPaths:
    main: Path
    execution: Path
    paper: Path
    backtest: Path
    news: Path

    @classmethod
    def from_env(cls) -> DbPaths:
        root = db_root()
        return cls(
            main=resolve_db_file(("DASHBOARD_MAIN_DB", "DB_PATH"), "dexscreener.db", root),
            execution=resolve_db_file(EXECUTION_DB_ENV, "execution_ledger.db", root),
            paper=resolve_db_file(("DASHBOARD_PAPER_DB", "PAPER_DB_PATH"), "paper_trading.db", root),
            backtest=resolve_db_file(("DASHBOARD_BACKTEST_DB",), "backtest_ledger.db", root),
            news=resolve_db_file(("DASHBOARD_NEWS_DB",), "news_events.db", root),
        )


@dataclass(frozen=True)
class DataResult:
    available: bool
    reason: str = ""
    data: Any = None
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class RateBlock:
    """Row-grain rates: numerator / n, where n is the execution_log row count."""

    n: int
    n_ok: int
    n_executed: int
    ok_rate: float | None
    executed_rate: float | None


@dataclass(frozen=True)
class DecisionWindowStats:
    """Decision-grain rates, same definitions as review.data.summarize_execution."""

    n_decisions: int
    n_with_exec: int
    n_ok: int
    n_executed: int
    ok_rate: float | None
    executed_rate: float | None
    # Same token inside 60 minutes counts once. First row in the window is kept.
    n_decisions_dedup: int = 0


@dataclass(frozen=True)
class FailureRow:
    id: int
    ts: str
    decision_log_id: str
    strategy_id: str
    action: str
    token_address: str
    validation_errors: str


@dataclass(frozen=True)
class ProcessProbe:
    name: str
    state: Liveness
    age_seconds: float | None
    detail: str


@dataclass(frozen=True)
class BacktestRun:
    run_id: str
    started_at: str
    finished_at: str
    strategy_id: str
    start_cash: float | None
    end_equity: float | None
    status: str
    notes: str


@dataclass(frozen=True)
class NewsItem:
    id: str
    source: str
    source_tier: str
    title: str
    url: str
    published_at: str
    onchain_status: str


@dataclass(frozen=True)
class PositionRow:
    id: str
    chain_id: str
    pair_address: str
    token_address: str
    symbol: str
    entry_price: float | None
    mark_price: float | None
    pnl_pct: float | None
    pnl_usd: float | None
    mtm_usd: float | None
    size_usd: float | None
    tp_pct: float | None
    sl_pct: float | None
    max_hold_min: float | None
    hold_seconds: float | None
    status: str


@dataclass(frozen=True)
class MarketRow:
    chain_id: str
    token_address: str
    pair_address: str
    price_usd: float | None
    liquidity_usd: float | None
    change_5m: float | None
    change_1h: float | None
    change_4h: float | None
    jev_strength: float | None
    coverage: float | None
    tier: str
    volatility: float | None = None
    sentiment: str = ""
    symbol: str = ""
    change_15m: float | None = None
    change_24h: float | None = None
    # tech_updated_at when present and non-blank; empty if the column or value is missing.
    tech_at: str = ""
    # Clock used for sort and staleness: tech_at, else updated_at / recorded_at. Never invented.
    fresh_at: str = ""
    fresh_state: Liveness = "unknown"


@dataclass(frozen=True)
class PositionsPayload:
    cash_usd: float | None
    cash_note: str
    equity_usd: float | None
    invested_usd: float
    position_ratio: float | None
    open_count: int
    missing_price: int
    price_note: str
    rows: tuple[PositionRow, ...]


@dataclass(frozen=True)
class MarketBaseStats:
    """Hot-tier indicator_history snapshot. Percents are already in percent units."""

    n_1h: int
    med_1h: str
    med_4h: str
    mean_1h_w: str


@dataclass(frozen=True)
class StrategyBucketRow:
    strategy_id: str
    n: int
    ok_rate: str
    n_dedup: int = 0


@dataclass(frozen=True)
class StrategyBuckets:
    rows: tuple[StrategyBucketRow, ...]


@dataclass(frozen=True)
class SignalRow:
    decision_id: int
    ts: str
    strategy_id: str
    action: str
    chain_id: str
    token_address: str
    pair_address: str
    jev_strength: float | None
    coverage: float | None
    liquidity_usd: float | None
    reason: str
    validation_ok: int | None
    validation_errors: str
    executed: int | None
    executor: str
    exec_dup: bool


@dataclass(frozen=True)
class SignalPayload:
    rows: tuple[SignalRow, ...]
    exec_note: str


@dataclass(frozen=True)
class ExecutionRow:
    id: int
    ts: str
    decision_log_id: str
    strategy_id: str
    action: str
    token_address: str
    executed: int | None
    executor: str
    receipt_summary: str
    validation_ok: int | None = None
    validation_errors: str = ""


@dataclass(frozen=True)
class RejectionBucket:
    key: str
    count: int


@dataclass(frozen=True)
class RejectionReport:
    days: int
    total: int
    buckets: tuple[RejectionBucket, ...]


@dataclass(frozen=True)
class PaperClosedBucket:
    """Observe-only attribution: exit_reason × liq bucket (min_liq=50k aligned)."""
    exit_reason: str
    liq_bucket: str
    count: int
    pnl_usd: float


@dataclass(frozen=True)
class PaperClosedReport:
    days: int
    closed: int
    total_pnl_usd: float
    buckets: tuple[PaperClosedBucket, ...]
    win_count: int = 0
    loss_count: int = 0
    win_rate: float | None = None
    worst_pnl_usd: float | None = None
    rest_pnl_usd: float | None = None
    single_wipe: bool = False
    # First-trade hit rate (Prediction Arena P-1): earliest closed per pair.
    first_hit_n: int = 0
    first_hit_wins: int = 0
    first_hit_rate: float | None = None
    first_hit_live_n: int = 0
    first_hit_live_wins: int = 0
    first_hit_live_rate: float | None = None
    first_hit_dedup: int = 0
    first_hit_live_dedup: int = 0
    # Exit-reason dominance (10-14 lesson: stop_loss can eat almost all loss).
    stop_loss_pnl_usd: float | None = None
    stop_loss_share: float | None = None


@dataclass(frozen=True)
class PaperTradeDay:
    """G-1 observe: daily open count vs realized pnl (Gann trade-frequency cost)."""
    day: str
    opens: int
    closed: int
    pnl_usd: float


@dataclass(frozen=True)
class PaperTradeFrequencyReport:
    days: int
    rows: tuple[PaperTradeDay, ...]
    total_opens: int
    total_closed: int
    total_pnl_usd: float
    avg_opens_per_active_day: float | None = None


@dataclass(frozen=True)
class SignalStreakReport:
    """F-1 observe: consecutive loss streak by strategy_id (no auto fuse)."""
    strategy_id: str
    closed: int
    max_loss_streak: int
    current_loss_streak: int
    cond_n: int
    cond_third_loss_rate: float | None  # P(3rd loss | 2 losses)


@dataclass(frozen=True)
class ExitHoldBucket:
    exit_reason: str
    count: int
    median_hold_min: float | None
    mean_hold_min: float | None
    pnl_usd: float


@dataclass(frozen=True)
class ExitHoldReport:
    days: int
    buckets: tuple[ExitHoldBucket, ...]


@dataclass(frozen=True)
class IntelHolderPoint:
    """One sampled snapshot. None metrics are gaps, not zeros."""

    chain_id: str
    token_address: str
    snapshot_id: str | None
    recorded_at: str | None
    top10_share: float | None
    dev_balance_delta: float | None


@dataclass(frozen=True)
class IntelHolderReport:
    """I-1 observe sample. Weight stays 0 and nothing here is wired to a decision."""

    snapshot_n: int
    non_null_share_n: int
    non_null_delta_n: int
    token_n: int
    latest: tuple[IntelHolderPoint, ...]
    weight: float = 0.0
    wired: bool = False
    gap: str = ""


@dataclass(frozen=True)
class IntelSlippageReport:
    """I-3 observe. ``high_bot_skip_wired`` is reported and never acted on."""

    slippage_n: int
    mean: float | None
    p50: float | None
    p90: float | None
    bot_share: float | None
    bot_status: str
    bot_source: str
    bot_fills: int | None
    total_fills: int | None
    high_bot_skip_wired: bool = False
    gap: str = ""


@dataclass(frozen=True)
class IntelCrowdingNote:
    """Short decision-side crowding footnote. Does not veto or fuse."""

    strategy_id: str
    live_n: int | None
    sample_n: int | None
    live_peak_hour_density: float | None
    line: str


@dataclass(frozen=True)
class TokenAgeBucket:
    """One token_age bucket. Counts are observe-only, not a validity score."""

    label: str
    count: int


@dataclass(frozen=True)
class TokenAgeSample:
    """A recent decision that carried a usable extras.pairCreatedAt."""

    decision_id: int
    age_days: float


@dataclass(frozen=True)
class TokenAgeReport:
    """Near-term decision_log token age. Not wired into gates or weights."""

    total: int
    with_field: int
    unknown: int
    unknown_ratio: float | None
    buckets: tuple[TokenAgeBucket, ...]
    samples: tuple[TokenAgeSample, ...]
    limit: int
    total_dedup: int = 0


# Paper pace vs the stated $20k mark. Observe-only; not a forecast or a gate.
GOAL_TARGET_USD = 20_000.0
GOAL_START_DAY = date(2026, 10, 20)
GOAL_DEADLINE_DAY = date(2026, 10, 30)
_HKT = ZoneInfo("Asia/Hong_Kong")

# Main-book open pause. Counts use opened_at; the flag file is display-only.
PAPER_OPEN_PAUSE_AT = datetime(2026, 10, 21, 0, 55, 16, tzinfo=_HKT)
PAPER_OPEN_PAUSE_FLAG = "PAPER_OPEN_PAUSED"
_TOKEN_DEDUP_WINDOW = timedelta(minutes=60)


@dataclass(frozen=True)
class GoalPaceReport:
    """Rough paper equity versus the $20k / 10-day mark.

    ``equity_usd`` is account cash plus open position ``size_usd``. It is not
    mark-to-market. ``daily_need_usd`` is None when no calendar days remain.
    Not wired into gates, weights, size, or live params.
    """

    cash_usd: float
    open_size_usd: float
    open_count: int
    size_gap_count: int
    equity_usd: float
    target_usd: float
    gap_usd: float
    as_of_day: str
    start_day: str
    deadline_day: str
    days_left: int
    daily_need_usd: float | None
    mark_to_market: bool
    note: str
    # 原账 is cash + open size. 保守 subtracts upward bad ticks only.
    equity_raw_usd: float = 0.0
    equity_conservative_usd: float = 0.0
    equity_down_only_usd: float = 0.0
    equity_both_usd: float = 0.0
    gap_raw_usd: float = 0.0
    gap_conservative_usd: float = 0.0
    true_tp_line: str = ""


# 注解库缺席时的坏报价调整。向上从原账扣，向下加回。对不上的格数另见 BONFERRONI（取大）。
BAD_TICK_UP_USD = -437.66
BAD_TICK_DOWN_USD = 603.36
TRUE_TP_FALLBACK = "真止盈 0/143（3 笔止盈均为向上假报价）"
ANNOTATIONS_DB_NAME = "paper_trading_annotations.db"
_BAD_TICK_UP = frozenset({"up", "upward", "向上", "up_tick", "uptick", "up-tick"})
_BAD_TICK_DOWN = frozenset({"down", "downward", "向下", "down_tick", "downtick", "down-tick"})
_TAKE_PROFIT_REASONS = frozenset({"take_profit", "tp", "takeprofit"})


# Reverse the $20k pace into a per-close requirement. Observe-only.
KPI_WINDOW_DAYS = 7
KPI_PACE_COUNTS = (5, 10, 20)
KPI_LEVERAGE_STEPS = (1, 2, 3)
# Stated daily-loss line for the leverage footnote. Not read from the environment.
KPI_DAILY_LOSS_LINE_USD = 200.0
_KpiVsLine = Literal["低于线", "触线", "超线"]


@dataclass(frozen=True)
class KpiPaceRow:
    """One assumed closes-per-day and the average pnl that pace would require.

    ``need_per_trade_usd`` is daily need divided by ``closes_per_day``. It stays
    None when the day count or the daily need is missing, including a zero rate.
    ``contrast`` is ``符号相反``, ``差 N 倍``, or empty when the comparison has a gap.
    """

    label: str
    closes_per_day: float | None
    need_per_trade_usd: float | None
    live_avg_pnl_usd: float | None
    contrast: str


@dataclass(frozen=True)
class KpiLeverageRow:
    """Configured stop scaled by a leverage step, beside the $200 daily-loss line."""

    multiple: int
    stop_usd: float | None
    vs_line: str


@dataclass(frozen=True)
class KpiReverseReport:
    """$20k pace turned into per-close pnl, next to live closed trades.

    Gap, days left, and daily need are the ``GoalPaceReport`` fields. Live
    averages use closed positions only. A missing pnl or an unreadable HKT day
    stays None. Stop dollars are ``size_usd * sl_pct / 100`` and exclude
    cancels. Not a gate, not an env write, and not a leverage change.
    """

    gap_usd: float
    days_left: int
    daily_need_usd: float | None
    as_of_day: str
    closed_n: int
    pnl_sum_usd: float | None
    avg_pnl_usd: float | None
    avg_closes_per_day: float | None
    window_days: int
    pace_rows: tuple[KpiPaceRow, ...]
    max_stop_usd: float | None
    leverage_rows: tuple[KpiLeverageRow, ...]
    negative_expectancy: bool | None
    daily_loss_line_usd: float
    gap_note: str
    equity_raw_usd: float = 0.0
    equity_conservative_usd: float = 0.0
    gap_raw_usd: float = 0.0
    gap_conservative_usd: float = 0.0
    true_tp_line: str = ""


# M7 shadow register. Observe-only buckets; the live floor stays where it is.
_M7_S0_MIN = 50_000.0
_M7_S1_MIN = 100_000.0
_M7_CSV_NAME = "m7_s0_s1_shadow_register.csv"
_M7_DB_SOURCE = "decision_log.raw_json.extras.liquidity_usd"
_M7_PHASE0_KEYS = ("shadow_liq_lt_150k", "shadow_R3", "shadow_skip_stop_prone")
_M7_LEAK_HINTS = frozenset(
    {"leak", "leak_below_s0", "liq_<50k", "liq_below_min", "below_s0"}
)
_M7_BLANK_WORDS = frozenset({"null", "none", "nan", "na", "n/a"})
_M7Bucket = Literal["S1", "S0", "liq_missing", "leak"]
_M7Origin = Literal["csv", "decision_log"]


@dataclass(frozen=True)
class _M7Obs:
    """One signal before the live_candidate buy filter."""

    decision_id: str | None
    liq_raw: Any
    has_liq: bool
    bucket_hint: str | None
    purpose: str | None
    action: str | None
    phase0: bool
    token: str = ""
    ts: datetime | None = None


@dataclass(frozen=True)
class M7ShadowReport:
    """S0 / S1 / liq_missing counts. Not a gate and not a Phase0 score.

    ``would_open_s0`` is S0+S1. ``would_open_s1`` is S1. ``n_leak`` is liquidity
    below 50k and stays outside the three buckets. ``n_phase0`` only counts
    rows that also carry a Phase0 flag; the flag does not pick the bucket.
    """

    n_s1: int
    n_s0: int
    n_missing: int
    n_leak: int
    n_phase0: int
    would_open_s0: int
    would_open_s1: int
    cohort: str
    source: str
    source_kind: _M7Origin
    counted: int
    dupes: int
    note: str
    n_s1_dedup: int = 0
    n_s0_dedup: int = 0
    n_missing_dedup: int = 0
    n_leak_dedup: int = 0
    n_phase0_dedup: int = 0
    would_open_s0_dedup: int = 0
    would_open_s1_dedup: int = 0


_INTEL_HOLDER_SAMPLE = 8
_INTEL_HOLDER_SHOW = 5
_INTEL_CROWD_STRATEGY = "jev_strength_v1"
# dexscreener.db 源表损坏时计数留空，不当作 0。碰到的表写入索引可疑。
SOURCE_DAMAGE_PREFIX = "源表损坏 · 待修（dexscreener.db malformed 2026-10-21）"
HOLDER_SUSPICIOUS_TABLES = (
    "token_holder_snapshots",
    "token_transfer_edges",
    "address_labels",
)
SLIP_SUSPICIOUS_TABLES = ("bot_fill_observations",)


def source_table_banner(tables: tuple[str, ...]) -> str:
    """Damage banner plus the tables this read touches."""
    return f"{SOURCE_DAMAGE_PREFIX}· 索引可疑：{'、'.join(tables)}"


SOURCE_TABLE_DAMAGE_BANNER = source_table_banner(HOLDER_SUSPICIOUS_TABLES)
_TOKEN_AGE_LIMIT = 300
_TOKEN_AGE_SAMPLES = 5
_TOKEN_AGE_LABELS = ("<1d", "1-7d", ">7d", "unknown")
_MS_PER_DAY = 86_400_000.0
_HOLDER_METRICS: Any = None
_CROWDING_MOD: Any = None



def _coerce_created_ms(value: Any) -> int | None:
    """Normalize a creation stamp to ms. Missing or unusable values stay None."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number == float("inf") or number == float("-inf"):
        return None
    if number > 1e12:
        return int(number)
    if number > 1e9:
        return int(number * 1000.0)
    return None


def _extras_pair_created_ms(raw: dict[str, Any]) -> int | None:
    """Read only raw_json.extras.pairCreatedAt. Other keys are not a fallback."""
    extras = raw.get("extras")
    if not isinstance(extras, dict) or "pairCreatedAt" not in extras:
        return None
    return _coerce_created_ms(extras.get("pairCreatedAt"))


def _token_age_days(signal_ts: Any, created_ms: int) -> float | None:
    """Days from pair creation to the decision timestamp. Negative age is unknown."""
    try:
        stamp = parse_ts(signal_ts)
    except (TypeError, ValueError):
        return None
    age_ms = stamp.timestamp() * 1000.0 - float(created_ms)
    if age_ms < 0:
        return None
    return age_ms / _MS_PER_DAY


def _token_age_label(age_days: float | None) -> str:
    """Coarse buckets: <1d / 1-7d / >7d. Exactly 1d and 7d stay in 1-7d."""
    if age_days is None:
        return "unknown"
    if age_days < 1:
        return "<1d"
    if age_days <= 7:
        return "1-7d"
    return ">7d"


def _goal_as_of(now: datetime | None) -> date:
    """Calendar day in Asia/Hong_Kong. Naive clocks are treated as UTC."""
    clock = now if now is not None else datetime.now(_HKT)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(_HKT).date()


def _goal_days_left(as_of: date, deadline: date = GOAL_DEADLINE_DAY) -> int:
    """Whole calendar days from ``as_of`` until the deadline. Never negative."""
    return max(0, (deadline - as_of).days)


def _goal_daily_need(gap_usd: float, days_left: int) -> float | None:
    """Gap per remaining day. Zero days left has no rate."""
    if days_left <= 0:
        return None
    return round(gap_usd / days_left, 2)


def _hkt_date(ts: object) -> date | None:
    """Calendar day in Asia/Hong_Kong. Naive clocks are UTC. Unreadable stays None."""
    raw = str(ts or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(_HKT).date()


def _kpi_status(row: sqlite3.Row, cols: set[str]) -> str:
    if "status" not in cols:
        return ""
    raw = _cell(row, "status")
    if raw is None:
        return ""
    return str(raw).strip().lower()


def _kpi_is_closed(row: sqlite3.Row, cols: set[str]) -> bool:
    """Closed trade. Cancels are not closes. Same open rule as the $20k card."""
    if _kpi_status(row, cols) in {"cancelled", "canceled"}:
        return False
    return not _is_open(row, cols)


def _kpi_contrast(need: float | None, live: float | None) -> str:
    """How far the required per-close pnl sits from the live average."""
    if need is None or live is None or live == 0:
        return ""
    if need == 0 or (need > 0) == (live > 0):
        ratio = abs(need / live)
        return f"差 {ratio:.2f} 倍"
    return "符号相反"


def _kpi_vs_line(stop_usd: float, line: float = KPI_DAILY_LOSS_LINE_USD) -> _KpiVsLine:
    if stop_usd > line:
        return "超线"
    if stop_usd < line:
        return "低于线"
    return "触线"


def _kpi_notes(*parts: str) -> str:
    return " · ".join(part for part in parts if part)


def _kpi_pace_rows(
    daily_need: float | None,
    live_avg: float | None,
    window_avg: float | None,
) -> tuple[KpiPaceRow, ...]:
    specs: list[tuple[str, float | None]] = [("近7日", window_avg)]
    for count in KPI_PACE_COUNTS:
        specs.append((str(count), float(count)))
    rows: list[KpiPaceRow] = []
    for label, per_day in specs:
        need: float | None = None
        if daily_need is not None and per_day is not None and per_day > 0:
            need = round(daily_need / per_day, 4)
        rows.append(
            KpiPaceRow(
                label=label,
                closes_per_day=None if per_day is None else round(per_day, 4),
                need_per_trade_usd=need,
                live_avg_pnl_usd=live_avg,
                contrast=_kpi_contrast(need, live_avg),
            )
        )
    return tuple(rows)


def _kpi_max_stop(rows: list[sqlite3.Row], cols: set[str]) -> tuple[float | None, str]:
    """Largest configured stop. Missing size or sl stays out of the max."""
    absent = [name for name in ("size_usd", "sl_pct") if name not in cols]
    if absent:
        return None, "缺少字段：" + ", ".join(absent)
    found: list[float] = []
    skipped = 0
    for row in rows:
        if _kpi_status(row, cols) in {"cancelled", "canceled"}:
            continue
        size = safe_float(_cell(row, "size_usd"))
        sl = safe_float(_cell(row, "sl_pct"))
        if size is None or sl is None:
            skipped += 1
            continue
        if size <= 0 or sl == 0:
            continue
        found.append(abs(float(size) * float(sl) / 100.0))
    if not found:
        if skipped:
            return None, f"{skipped} 笔缺 size_usd 或 sl_pct"
        return None, "无可用止损"
    note = f"{skipped} 笔缺 size_usd 或 sl_pct，未计入最大" if skipped else ""
    return round(max(found), 2), note


def _kpi_leverage_rows(max_stop: float | None) -> tuple[KpiLeverageRow, ...]:
    rows: list[KpiLeverageRow] = []
    for step in KPI_LEVERAGE_STEPS:
        if max_stop is None:
            rows.append(KpiLeverageRow(step, None, ""))
            continue
        scaled = round(max_stop * step, 2)
        rows.append(KpiLeverageRow(step, scaled, _kpi_vs_line(scaled)))
    return tuple(rows)


def _kpi_reverse_from_rows(
    pace: GoalPaceReport,
    rows: list[sqlite3.Row],
    cols: set[str],
) -> KpiReverseReport:
    """Build the reverse report from rows already read. Does not touch the file."""
    closed = [row for row in rows if _kpi_is_closed(row, cols)]
    closed_n = len(closed)
    pnl_note = ""
    pnl_sum: float | None = None
    avg: float | None = None
    if "pnl_usd" not in cols:
        if closed_n:
            pnl_note = "缺少字段：pnl_usd"
    elif closed_n == 0:
        avg = None
    else:
        missing = 0
        total = 0.0
        for row in closed:
            pnl = safe_float(_cell(row, "pnl_usd"))
            if pnl is None:
                missing += 1
                continue
            total += float(pnl)
        if missing:
            pnl_note = f"{missing} 笔已平缺 pnl_usd"
        else:
            pnl_sum = round(total, 4)
            avg = round(total / closed_n, 4)
    window_note = ""
    window_avg: float | None
    if closed_n == 0:
        window_avg = 0.0
    elif "closed_at" not in cols:
        window_avg = None
        window_note = "缺少字段：closed_at"
    else:
        as_of = date.fromisoformat(pace.as_of_day)
        window_start = as_of - timedelta(days=KPI_WINDOW_DAYS - 1)
        undated = 0
        in_window = 0
        for row in closed:
            day = _hkt_date(_cell(row, "closed_at"))
            if day is None:
                undated += 1
                continue
            if window_start <= day <= as_of:
                in_window += 1
        if undated:
            window_avg = None
            window_note = f"{undated} 笔已平无法按 HKT 切日"
        else:
            window_avg = round(in_window / KPI_WINDOW_DAYS, 4)
    max_stop, stop_note = _kpi_max_stop(rows, cols)
    negative: bool | None = None if avg is None else avg < 0
    return KpiReverseReport(
        gap_usd=pace.gap_usd,
        days_left=pace.days_left,
        daily_need_usd=pace.daily_need_usd,
        as_of_day=pace.as_of_day,
        closed_n=closed_n,
        pnl_sum_usd=pnl_sum,
        avg_pnl_usd=avg,
        avg_closes_per_day=window_avg,
        window_days=KPI_WINDOW_DAYS,
        pace_rows=_kpi_pace_rows(pace.daily_need_usd, avg, window_avg),
        max_stop_usd=max_stop,
        leverage_rows=_kpi_leverage_rows(max_stop),
        negative_expectancy=negative,
        daily_loss_line_usd=KPI_DAILY_LOSS_LINE_USD,
        gap_note=_kpi_notes(pace.note, pnl_note, window_note, stop_note),
        equity_raw_usd=pace.equity_raw_usd,
        equity_conservative_usd=pace.equity_conservative_usd,
        gap_raw_usd=pace.gap_raw_usd,
        gap_conservative_usd=pace.gap_conservative_usd,
        true_tp_line=pace.true_tp_line,
    )
def dedup_token_count(events: list[tuple[datetime | None, str]]) -> int:
    """Keep the first row of each token_address inside a 60-minute window.

    A later row of the same token is dropped when it falls strictly inside
    60 minutes of the last kept row. A row at exactly 60 minutes starts a new
    window. A blank token is not merged with other blanks: a missing address
    is not one shared token. An unparseable time is kept.
    """
    ordered = sorted(
        enumerate(events),
        key=lambda item: (
            item[1][0] is None,
            item[1][0] or datetime.min.replace(tzinfo=timezone.utc),
            item[0],
        ),
    )
    kept_at: dict[str, datetime] = {}
    kept = 0
    for _index, (stamp, token) in ordered:
        key = token.strip().casefold()
        if not key or stamp is None:
            kept += 1
            continue
        previous = kept_at.get(key)
        if previous is not None and stamp - previous < _TOKEN_DEDUP_WINDOW:
            continue
        kept_at[key] = stamp
        kept += 1
    return kept


def _optional_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_ts(text)
    except (TypeError, ValueError):
        return None


def dashboard_repo_root() -> Path:
    """Repo root for the pause flag. The crawler mirror sits one level down."""
    root = Path(__file__).resolve().parents[1]
    if root.name == "crawler":
        return root.parent
    return root


def paper_pause_flag_state() -> tuple[bool | None, str]:
    """Whether PAPER_OPEN_PAUSED exists. Unreadable stays None and does not raise."""
    path = dashboard_repo_root() / PAPER_OPEN_PAUSE_FLAG
    try:
        present = path.is_file()
    except OSError:
        log.warning("paper pause flag unreadable")
        return None, f"{PAPER_OPEN_PAUSE_FLAG}：不可读"
    if present:
        return True, f"{PAPER_OPEN_PAUSE_FLAG}：在"
    return False, f"{PAPER_OPEN_PAUSE_FLAG}：不在"


@dataclass(frozen=True)
class PaperPauseSplit:
    """Main-book opens before and after the pause instant."""

    before: int
    after: int
    flag_present: bool | None
    flag_note: str


def _hkt_day(ts: str | None) -> str:
    """Calendar day in Asia/Hong_Kong for ISO timestamps (align decision/review)."""
    raw = str(ts or "").strip()
    if not raw:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d")
    except Exception:
        return raw[:10]

class MissingTable(LookupError):
    pass


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Open one SQLite file read-only. Raises FileNotFoundError when it is absent."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"db not found: {file_path.name}")
    uri = "file:" + quote(file_path.resolve().as_posix(), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=500")
    conn.execute("PRAGMA query_only=ON")
    return conn


@contextmanager
def ro_connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect_ro(path)
    try:
        yield conn
    finally:
        conn.close()


def latest_gate_receipt() -> _ReceiptFlags:
    """Newest execution_log receipt. A missing ledger is an empty snapshot, not an error.

    Opened with ``connect_ro`` (``mode=ro``, ``query_only``). Called from
    ``settings.read_gates`` so the gate lights can follow the gateway without
    a second connection helper.
    """
    path = resolve_db_file(EXECUTION_DB_ENV, "execution_ledger.db")
    if not path.is_file():
        return _ReceiptFlags(False, None, None, None)
    try:
        with ro_connect(path) as conn:
            if not _table_exists(conn, "execution_log"):
                return _ReceiptFlags(False, None, None, None)
            cols = _columns(conn, "execution_log")
            if "receipt_json" not in cols:
                return _ReceiptFlags(False, None, None, None)
            order = "id" if "id" in cols else "rowid"
            ts_expr = "ts" if "ts" in cols else "NULL"
            found = _exec(
                conn,
                f"SELECT {ts_expr} AS ts, receipt_json FROM execution_log "
                f"ORDER BY {order} DESC LIMIT 1",
            ).fetchone()
    except (OSError, sqlite3.Error, FileNotFoundError):
        log.warning("gateway receipt unreadable")
        return _ReceiptFlags(False, None, None, None)
    if found is None:
        return _ReceiptFlags(False, None, None, None)
    payload = parse_json(str(found["receipt_json"] or ""))
    return _ReceiptFlags(
        True,
        _parse_utc(found["ts"]),
        _as_bool(payload.get("paper_enabled")),
        _as_bool(payload.get("pm_enabled")),
    )


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _ok(data: Any, started: float) -> DataResult:
    return DataResult(True, "", data, _ms(started))


def _unavail(reason: str, started: float) -> DataResult:
    return DataResult(False, reason, None, _ms(started))


def _finish(result: DataResult, what: str) -> DataResult:
    if result.elapsed_ms > 200:
        log.warning("slow read %s %.0fms", what, result.elapsed_ms)
    return result


def _safe_exc(exc: BaseException) -> str:
    try:
        message = clip(str(exc), 160)
    except Exception:
        message = ""
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _ident(name: str) -> str:
    if not _IDENT_RE.fullmatch(name):
        raise RuntimeError("refusing unexpected identifier")
    return name


def _exec(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
    if _WRITE_RE.search(sql):
        raise RuntimeError("read-only dashboard refused a non-select statement")
    return conn.execute(sql, params)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    _ident(table)
    row = _exec(
        conn,
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    _ident(table)
    if table not in _TABLES:
        raise RuntimeError("refusing unexpected table")
    if not _table_exists(conn, table):
        return set()
    rows = _exec(conn, f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _require(cols: set[str], needed: tuple[str, ...]) -> str | None:
    missing = [name for name in needed if name not in cols]
    if not missing:
        return None
    return "缺少字段：" + ", ".join(missing)


def _cell(row: sqlite3.Row, name: str) -> Any:
    if name not in row.keys():
        return None
    return row[name]


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _in_range(ts: Any, start: datetime, end: datetime, *, end_inclusive: bool) -> bool:
    try:
        stamp = parse_ts(ts)
    except (TypeError, ValueError):
        prefix = str(ts or "")[:10]
        if end_inclusive:
            return start.strftime("%Y-%m-%d") <= prefix <= end.strftime("%Y-%m-%d")
        return start.strftime("%Y-%m-%d") <= prefix < end.strftime("%Y-%m-%d")
    if end_inclusive:
        return start <= stamp <= end
    return start <= stamp < end


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _age_seconds(ts: Any, now: datetime) -> float | None:
    try:
        stamp = parse_ts(ts)
    except (TypeError, ValueError):
        return None
    return (now - stamp).total_seconds()


def _rejection_sql_clauses(cols: set[str]) -> list[str]:
    """Static predicates for rows that can be validation, PM, risk, or paper rejects."""
    clauses: list[str] = []
    if "validation_ok" in cols:
        clauses.append("COALESCE(validation_ok, 0) != 1")
    if "validation_errors" in cols:
        clauses.append("(validation_errors IS NOT NULL AND TRIM(validation_errors) != '')")
    if "status" in cols:
        clauses.append("LOWER(COALESCE(status, '')) = 'rejected'")
    if "receipt_json" in cols:
        clauses.append(
            "("
            "INSTR(receipt_json, '\"pm_rejected\"') > 0"
            " OR INSTR(receipt_json, '\"risk_rejected\"') > 0"
            " OR INSTR(receipt_json, '\"paper_failed\"') > 0"
            " OR INSTR(receipt_json, '\"validation_failed\"') > 0"
            ")"
        )
    return clauses


def _liveness(age: float | None, limit: float | None = None) -> Liveness:
    if age is None:
        return "unknown"
    window = stale_seconds() if limit is None else limit
    if age <= window:
        return "alive"
    return "stale"


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _holder_point_key(point: IntelHolderPoint) -> tuple[str, str, str]:
    return (point.recorded_at or "", point.chain_id, point.snapshot_id or "")


def _parse_iso_utc(ts: str | None) -> datetime | None:
    """Same clock parse as indicators.base.parse_iso, without importing it."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None


def _norm_chain(chain_id: str) -> str:
    return str(chain_id).strip().lower()


def _norm_address(address: str) -> str:
    return str(address).strip().lower()


def _helper_path(*parts: str) -> Path:
    """Read-only helper next to this package, or under the nested crawler tree.

    ``dashboard/`` at the repo root looks in ``crawler/``. The mirrored
    ``crawler/dashboard/`` already sits inside that tree.
    """
    package_root = Path(__file__).resolve().parents[1]
    candidates = [
        package_root.joinpath(*parts),
        package_root.joinpath("crawler", *parts),
    ]
    parents = Path(__file__).resolve().parents
    if len(parents) > 2:
        candidates.append(parents[2].joinpath(*parts))
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _holder_readers() -> Any:
    """Load holder_metrics without importing crawler store (the write path).

    The helper module imports ``store.norm_*`` and ``indicators.base.parse_iso``.
    Those two names are bound to local copies for the load, then the stubs are
    removed so this process does not keep a fake ``store`` module around.
    """
    global _HOLDER_METRICS
    if _HOLDER_METRICS is not None:
        return _HOLDER_METRICS
    path = _helper_path("indicators", "holder_metrics.py")
    if not path.is_file():
        raise ImportError("holder_metrics.py missing")
    store_stub = types.ModuleType("store")
    store_stub.norm_chain = _norm_chain
    store_stub.norm_address = _norm_address
    base_stub = types.ModuleType("indicators.base")
    base_stub.parse_iso = _parse_iso_utc
    pkg_stub = types.ModuleType("indicators")
    pkg_stub.__path__ = []  # type: ignore[attr-defined]
    stubs = {
        "store": store_stub,
        "indicators": pkg_stub,
        "indicators.base": base_stub,
    }
    planted: list[str] = []
    for name, module in stubs.items():
        if name not in sys.modules:
            sys.modules[name] = module
            planted.append(name)
    module_name = "_dashboard_holder_metrics"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("holder_metrics spec failed")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    finally:
        for name in planted:
            sys.modules.pop(name, None)
    _HOLDER_METRICS = module
    return module


def _crowding_module() -> Any:
    """Load scripts/signal_crowding.py. It does not touch execution or the crawler writer."""
    global _CROWDING_MOD
    if _CROWDING_MOD is not None:
        return _CROWDING_MOD
    path = _helper_path("scripts", "signal_crowding.py")
    if not path.is_file():
        raise ImportError("signal_crowding.py missing")
    module_name = "_dashboard_signal_crowding"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("signal_crowding spec failed")
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks the class module up in sys.modules during class body.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    _CROWDING_MOD = module
    return module


def _file_has_table(path: Path | None, table: str) -> bool:
    if path is None or not path.is_file():
        return False
    with ro_connect(path) as conn:
        return _table_exists(conn, table)


def _probe_table(path: Path, table: str) -> None:
    """Read one row so a damaged table raises instead of looking like a zero count."""
    with ro_connect(path) as conn:
        if not _table_exists(conn, table):
            return
        _exec(conn, f"SELECT 1 FROM {_ident(table)} LIMIT 1").fetchone()


def _crowding_line(
    strategy_id: str,
    live_n: int | None,
    sample_n: int | None,
    peak: float | None,
) -> str:
    live_s = "data gap" if live_n is None else str(live_n)
    sample_s = "data gap" if sample_n is None else str(sample_n)
    peak_s = "data gap" if peak is None else f"{peak:.2f}"
    return (
        f"拥挤旁证 {strategy_id} · live n={live_s} · 峰值小时 {peak_s} · "
        f"sample n={sample_s} · 不否决 · 不算有效性"
    )


def _m7_classify(value: Any) -> _M7Bucket:
    """S1 ≥ 100k, S0 in [50k, 100k), leak below 50k, else liq_missing."""
    if value is None or isinstance(value, bool):
        return "liq_missing"
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text.lower() in _M7_BLANK_WORDS:
            return "liq_missing"
        value = text
    number = safe_float(value)
    if number is None:
        return "liq_missing"
    if number >= _M7_S1_MIN:
        return "S1"
    if number >= _M7_S0_MIN:
        return "S0"
    return "leak"


def _m7_hint_bucket(hint: str | None) -> _M7Bucket:
    """Use a CSV bucket only when liq_usd itself is absent. Unknown stays missing."""
    text = (hint or "").strip()
    if text == "S1":
        return "S1"
    if text == "S0":
        return "S0"
    if text == "liq_missing":
        return "liq_missing"
    if text.lower() in _M7_LEAK_HINTS:
        return "leak"
    return "liq_missing"


def _m7_row_bucket(row: _M7Obs) -> _M7Bucket:
    if row.has_liq:
        return _m7_classify(row.liq_raw)
    return _m7_hint_bucket(row.bucket_hint)


def _m7_phase0_on(payload: dict[str, Any]) -> bool:
    for key in _M7_PHASE0_KEYS:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None or value is False or value == 0:
            continue
        if isinstance(value, str) and value.strip().lower() in {"", "0", "false", "no", "off"}:
            continue
        return True
    return False


def _m7_header_map(fieldnames: list[str] | None) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for name in fieldnames or []:
        if name is None:
            continue
        key = str(name).strip().lstrip("\ufeff").lower()
        if key and key not in mapped:
            mapped[key] = name
    return mapped


def _m7_cell(row: dict[str, Any], header: dict[str, str], key: str) -> str | None:
    original = header.get(key)
    if original is None or original not in row:
        return None
    value = row.get(original)
    if value is None:
        return ""
    return str(value).strip()


def _m7_purpose(cell: str | None, extras: dict[str, Any]) -> str | None:
    if cell:
        return cell.strip()
    if "purpose" in extras and extras.get("purpose") is not None:
        return str(extras.get("purpose")).strip()
    if cell is not None:
        return ""
    return None


def _m7_action(cell: str | None, extras: dict[str, Any]) -> str | None:
    if cell:
        return cell.strip().lower() or None
    if "action" in extras and extras.get("action") is not None:
        return str(extras.get("action")).strip().lower() or None
    return None


def _m7_obs_from_parts(
    *,
    decision_id: str | None,
    liq_raw: Any,
    has_liq: bool,
    bucket_hint: str | None,
    purpose: str | None,
    action: str | None,
    phase0: bool,
    token: str = "",
    ts: datetime | None = None,
) -> _M7Obs:
    return _M7Obs(
        decision_id=decision_id,
        liq_raw=liq_raw,
        has_liq=has_liq,
        bucket_hint=bucket_hint,
        purpose=purpose,
        action=action,
        phase0=phase0,
        token=token,
        ts=ts,
    )


def _m7_token_from_raw(raw: dict[str, Any]) -> str:
    for key in ("token_address", "token"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    extras = raw.get("extras")
    if isinstance(extras, dict):
        for key in ("token_address", "token"):
            value = extras.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def _m7_obs_from_raw(
    raw: dict[str, Any],
    action: str | None,
    decision_id: str | None,
    *,
    token: str = "",
    ts: datetime | None = None,
) -> _M7Obs:
    extras = raw.get("extras")
    extras_d = extras if isinstance(extras, dict) else {}
    has_liq = "liquidity_usd" in extras_d
    purpose = _m7_purpose(None, extras_d)
    if purpose is None and "purpose" in raw and raw.get("purpose") is not None:
        purpose = str(raw.get("purpose")).strip()
    act = _m7_action(None if action is None else str(action), extras_d)
    found = token.strip() or _m7_token_from_raw(raw)
    return _m7_obs_from_parts(
        decision_id=decision_id,
        liq_raw=extras_d.get("liquidity_usd") if has_liq else None,
        has_liq=has_liq,
        bucket_hint=None,
        purpose=purpose,
        action=act,
        phase0=_m7_phase0_on(extras_d) or _m7_phase0_on(raw),
        token=found,
        ts=ts,
    )


def _m7_obs_from_csv(row: dict[str, Any], header: dict[str, str]) -> _M7Obs:
    extras = parse_json(_m7_cell(row, header, "extras"))
    has_liq = "liq_usd" in header
    decision = _m7_cell(row, header, "decision_log_id")
    phase0 = _m7_phase0_on(extras)
    if not phase0:
        for key in _M7_PHASE0_KEYS:
            cell = _m7_cell(row, header, key.lower())
            if cell is not None and _m7_phase0_on({key: cell}):
                phase0 = True
                break
    token = _m7_cell(row, header, "token") or _m7_cell(row, header, "token_address") or ""
    return _m7_obs_from_parts(
        decision_id=decision or None,
        liq_raw=_m7_cell(row, header, "liq_usd") if has_liq else None,
        has_liq=has_liq,
        bucket_hint=_m7_cell(row, header, "bucket"),
        purpose=_m7_purpose(_m7_cell(row, header, "purpose"), extras),
        action=_m7_action(_m7_cell(row, header, "action"), extras),
        phase0=phase0,
        token=token,
        ts=_optional_ts(_m7_cell(row, header, "ts")),
    )


def _m7_read_csv(path: Path) -> tuple[list[_M7Obs], int]:
    by_id: dict[str, _M7Obs] = {}
    anon: list[_M7Obs] = []
    dupes = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = _m7_header_map(reader.fieldnames)
        for raw_row in reader:
            if raw_row is None:
                continue
            if all(not str(value or "").strip() for value in raw_row.values()):
                continue
            obs = _m7_obs_from_csv(raw_row, header)
            if obs.decision_id is None:
                anon.append(obs)
                continue
            if obs.decision_id in by_id:
                dupes += 1
            by_id[obs.decision_id] = obs
    return list(by_id.values()) + anon, dupes


def _m7_cohort(rows: list[_M7Obs], *, origin: _M7Origin) -> tuple[list[_M7Obs], str, str]:
    """Prefer live_candidate buys. A file with no purpose column keeps every row.

    An explicit non-buy is dropped. Rows that never recorded an action stay,
    because the shadow CSV is the buy register and often has no action column.
    """
    if origin == "decision_log":
        pool = [row for row in rows if row.action == "buy"]
        action_known = True
        extra = ""
    elif any(row.action is not None for row in rows):
        pool = [row for row in rows if row.action in (None, "buy")]
        action_known = True
        extra = ""
    else:
        pool = list(rows)
        action_known = False
        extra = "CSV 无 action 列，按影簿 buy 计"
    if any(row.purpose is not None for row in pool):
        live = [row for row in pool if row.purpose == "live_candidate"]
        if live:
            return live, "live_candidate buy", extra
        if action_known:
            return pool, "buy（无 live_candidate）", ""
        return pool, "全表（无 live_candidate）", extra
    if action_known:
        return pool, "buy", ""
    return pool, "csv 全表", extra


def _m7_dedup_counts(rows: list[_M7Obs]) -> dict[str, int]:
    """Token-window dedup for each displayed M7 count, on the rows that make it."""
    groups: dict[str, list[tuple[datetime | None, str]]] = {
        "S1": [],
        "S0": [],
        "liq_missing": [],
        "leak": [],
        "phase0": [],
        "would_open_s0": [],
        "would_open_s1": [],
    }
    for row in rows:
        event = (row.ts, row.token)
        bucket = _m7_row_bucket(row)
        match bucket:
            case "S1":
                groups["S1"].append(event)
                groups["would_open_s0"].append(event)
                groups["would_open_s1"].append(event)
            case "S0":
                groups["S0"].append(event)
                groups["would_open_s0"].append(event)
            case "liq_missing":
                groups["liq_missing"].append(event)
            case "leak":
                groups["leak"].append(event)
            case _ as unreachable:
                assert_never(unreachable)
        if row.phase0:
            groups["phase0"].append(event)
    return {key: dedup_token_count(value) for key, value in groups.items()}


def _m7_tally(rows: list[_M7Obs]) -> tuple[int, int, int, int, int]:
    s1 = s0 = missing = leak = phase0 = 0
    for row in rows:
        bucket = _m7_row_bucket(row)
        match bucket:
            case "S1":
                s1 += 1
            case "S0":
                s0 += 1
            case "liq_missing":
                missing += 1
            case "leak":
                leak += 1
            case _ as unreachable:
                assert_never(unreachable)
        if row.phase0:
            phase0 += 1
    return s1, s0, missing, leak, phase0


def _m7_report(
    rows: list[_M7Obs],
    dupes: int,
    *,
    origin: _M7Origin,
    source: str,
    note: str,
) -> M7ShadowReport:
    chosen, cohort, cohort_note = _m7_cohort(rows, origin=origin)
    n_s1, n_s0, n_missing, n_leak, n_phase0 = _m7_tally(chosen)
    deduped = _m7_dedup_counts(chosen)
    bits: list[str] = []
    if dupes:
        bits.append(f"decision_log_id 重复 {dupes} 行，保留末行")
    if cohort_note:
        bits.append(cohort_note)
    if note:
        bits.append(note)
    return M7ShadowReport(
        n_s1=n_s1,
        n_s0=n_s0,
        n_missing=n_missing,
        n_leak=n_leak,
        n_phase0=n_phase0,
        would_open_s0=n_s1 + n_s0,
        would_open_s1=n_s1,
        cohort=cohort,
        source=source,
        source_kind=origin,
        counted=n_s1 + n_s0 + n_missing + n_leak,
        dupes=dupes,
        note=" · ".join(bits),
        n_s1_dedup=deduped["S1"],
        n_s0_dedup=deduped["S0"],
        n_missing_dedup=deduped["liq_missing"],
        n_leak_dedup=deduped["leak"],
        n_phase0_dedup=deduped["phase0"],
        would_open_s0_dedup=deduped["would_open_s0"],
        would_open_s1_dedup=deduped["would_open_s1"],
    )


def _m7_source_label(path: Path) -> str:
    parts = path.parts
    if "exports" in parts:
        index = len(parts) - 1 - parts[::-1].index("exports")
        return str(Path(*parts[index:]))
    return path.name


def _annotations_path(paper: Path) -> Path:
    return paper.with_name(ANNOTATIONS_DB_NAME)


def _load_bad_ticks(paper: Path) -> tuple[float, float, frozenset[str], bool]:
    """Up/down adjustments and upward take-profit ids.

    A missing annotations file, or a bad_tick table that cannot be read,
    keeps the audit constants. A damaged file is not treated as a zero
    adjustment.
    """
    fallback = (BAD_TICK_UP_USD, BAD_TICK_DOWN_USD, frozenset(), False)
    path = _annotations_path(paper)
    if not path.is_file():
        return fallback
    try:
        with ro_connect(path) as conn:
            table = "bad_tick" if _table_exists(conn, "bad_tick") else ""
            if not table and _table_exists(conn, "bad_ticks"):
                table = "bad_ticks"
            if not table:
                return fallback
            cols = _columns(conn, table)
            amount_col = next(
                (
                    name
                    for name in ("adjustment_usd", "delta_usd", "pnl_delta", "pnl_usd")
                    if name in cols
                ),
                "",
            )
            if not amount_col or "direction" not in cols:
                return fallback
            selected = ["direction", amount_col]
            if "position_id" in cols:
                selected.append("position_id")
            if "exit_reason" in cols:
                selected.append("exit_reason")
            rows = _exec(
                conn,
                f"SELECT {', '.join(selected)} FROM {_ident(table)}",
            ).fetchall()
    except sqlite3.Error:
        return fallback
    up = 0.0
    down = 0.0
    up_tp: set[str] = set()
    saw = False
    for row in rows:
        direction = str(_cell(row, "direction") or "").strip().casefold()
        amount = safe_float(_cell(row, amount_col))
        if amount is None:
            continue
        saw = True
        if direction in _BAD_TICK_UP:
            up += float(amount)
            reason = str(_cell(row, "exit_reason") or "").strip().casefold()
            if reason in _TAKE_PROFIT_REASONS:
                pid = str(_cell(row, "position_id") or "").strip()
                if pid:
                    up_tp.add(pid)
        elif direction in _BAD_TICK_DOWN:
            down += float(amount)
    if not saw:
        return fallback
    return (round(up, 2), round(down, 2), frozenset(up_tp), True)


def _true_tp_line(
    rows: list[sqlite3.Row],
    cols: set[str],
    up_tp_ids: frozenset[str],
    from_annotations: bool,
) -> str:
    """Audit sentence. Without bad_tick rows, keep the known fallback."""
    if not from_annotations or "exit_reason" not in cols:
        return TRUE_TP_FALLBACK
    closed_n = 0
    tp_n = 0
    fake = 0
    for row in rows:
        if _is_open(row, cols):
            continue
        status = str(_cell(row, "status") or "").strip().casefold()
        if status in {"cancelled", "canceled"}:
            continue
        closed_n += 1
        reason = str(_cell(row, "exit_reason") or "").strip().casefold()
        if reason not in _TAKE_PROFIT_REASONS:
            continue
        tp_n += 1
        pid = str(_cell(row, "id") or "").strip()
        if pid and pid in up_tp_ids:
            fake += 1
    true_n = tp_n - fake
    if true_n < 0:
        true_n = 0
    return f"真止盈 {true_n}/{closed_n}（{fake} 笔止盈均为向上假报价）"


class DataStore:
    def __init__(self, paths: DbPaths | None = None) -> None:
        self.paths = paths if paths is not None else DbPaths.from_env()

    def file_status(self) -> list[tuple[str, str, bool]]:
        items = (
            ("主库", self.paths.main),
            ("执行账本", self.paths.execution),
            ("模拟盘", self.paths.paper),
            ("回测", self.paths.backtest),
            ("新闻", self.paths.news),
        )
        return [(label, path.name, path.is_file()) for label, path in items]

    def today_execution_stats(self) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._today_execution_stats(started), "today_execution_stats")
        except Exception as exc:
            log.warning("read failed today_execution_stats: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "today_execution_stats")

    def week_decision_stats(self) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._week_decision_stats(started), "week_decision_stats")
        except Exception as exc:
            log.warning("read failed week_decision_stats: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "week_decision_stats")

    def recent_failures(self, limit: int = 10) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._recent_failures(started, limit), "recent_failures")
        except Exception as exc:
            log.warning("read failed recent_failures: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "recent_failures")

    def process_liveness(self) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._process_liveness(started), "process_liveness")
        except Exception as exc:
            log.warning("read failed process_liveness: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "process_liveness")

    def latest_backtest(self) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._latest_backtest(started), "latest_backtest")
        except Exception as exc:
            log.warning("read failed latest_backtest: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "latest_backtest")

    def latest_news(self, limit: int = 5) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._latest_news(started, limit), "latest_news")
        except Exception as exc:
            log.warning("read failed latest_news: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "latest_news")

    def open_positions(self) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._open_positions(started), "open_positions")
        except Exception as exc:
            log.warning("read failed open_positions: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "open_positions")

    def recent_signals(self, limit: int = 50) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._recent_signals(started, limit), "recent_signals")
        except Exception as exc:
            log.warning("read failed recent_signals: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "recent_signals")

    def recent_executions(self, limit: int = 50) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._recent_executions(started, limit), "recent_executions")
        except Exception as exc:
            log.warning("read failed recent_executions: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "recent_executions")

    def paper_signal_streaks(self, days: int = 14) -> DataResult:
        """F-1 observe-only: per-strategy loss streaks + P(3rd L|2L)."""
        started = time.perf_counter()
        try:
            return _finish(self._paper_signal_streaks(started, days), "paper_signal_streaks")
        except Exception as exc:
            log.warning("read failed paper_signal_streaks: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "paper_signal_streaks")

    def paper_exit_holds(self, days: int = 14) -> DataResult:
        """F-3 observe-only: hold minutes by exit_reason (sync exit vs max_hold)."""
        started = time.perf_counter()
        try:
            return _finish(self._paper_exit_holds(started, days), "paper_exit_holds")
        except Exception as exc:
            log.warning("read failed paper_exit_holds: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "paper_exit_holds")

    def paper_trade_frequency(self, days: int = 14) -> DataResult:

        """G-1 observe-only: opens/closed/pnl by HKT day from paper fills+positions."""
        started = time.perf_counter()
        try:
            return _finish(self._paper_trade_frequency(started, days), "paper_trade_frequency")
        except Exception as exc:
            log.warning("read failed paper_trade_frequency: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "paper_trade_frequency")

    def intel_holder_observe(self, limit: int = _INTEL_HOLDER_SAMPLE) -> DataResult:
        """I-1 observe-only sample of top10_share and dev_balance_delta.

        Calls the read-only holder helpers. A missing database or table is
        unavailable. An empty table or an unsummable snapshot stays None.
        """
        started = time.perf_counter()
        try:
            return _finish(self._intel_holder_observe(started, limit), "intel_holder_observe")
        except Exception as exc:
            log.warning("read failed intel_holder_observe: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "intel_holder_observe")

    def intel_slippage_observe(self) -> DataResult:
        """I-3 observe-only slippage_1h and bot_trade_share.

        Uses ``execution.slippage_report.build_report``. Does not skip orders.
        Headline bot share is collection #81 only; anything else is a data gap.
        """
        started = time.perf_counter()
        try:
            return _finish(self._intel_slippage_observe(started), "intel_slippage_observe")
        except Exception as exc:
            log.warning("read failed intel_slippage_observe: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "intel_slippage_observe")

    def intel_crowding_note(self) -> DataResult:
        """Optional decision-side crowding footnote for jev_strength_v1."""
        started = time.perf_counter()
        try:
            return _finish(self._intel_crowding_note(started), "intel_crowding_note")
        except Exception as exc:
            log.warning("read failed intel_crowding_note: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "intel_crowding_note")

    def token_age_observe(self, limit: int = _TOKEN_AGE_LIMIT) -> DataResult:
        """Observe-only token age from decision_log.raw_json extras.pairCreatedAt.

        Reads the newest rows on the main DB. A missing field or unusable stamp
        is the unknown bucket. Missing table or column stays unavailable.
        Not a validity score and not wired into gates, weights, or live params.
        """
        started = time.perf_counter()
        try:
            return _finish(self._token_age_observe(started, limit), "token_age_observe")
        except Exception as exc:
            log.warning("read failed token_age_observe: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "token_age_observe")

    def paper_pause_split(self) -> DataResult:
        """Opened counts on the main book split at the 2026-10-21 HKT pause.

        Read-only. A position with opened_at after the pause is the after
        count. The PAPER_OPEN_PAUSED flag is only whether the file exists.
        """
        started = time.perf_counter()
        try:
            return _finish(self._paper_pause_split(started), "paper_pause_split")
        except Exception as exc:
            log.warning("read failed paper_pause_split: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "paper_pause_split")

    def paper_goal_pace(self, *, now: datetime | None = None) -> DataResult:
        """Observe-only paper equity versus $20k by 2026-10-30 HKT.

        Reads ``account.cash_usd`` and open ``positions.size_usd`` only.
        Equity is not mark-to-market. Does not change gates or live params.
        """
        started = time.perf_counter()
        try:
            return _finish(self._paper_goal_pace(started, now), "paper_goal_pace")
        except Exception as exc:
            log.warning("read failed paper_goal_pace: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "paper_goal_pace")

    def paper_kpi_reverse(self, *, now: datetime | None = None) -> DataResult:
        """Observe-only reverse of the $20k pace into per-close pnl.

        Reuses ``GoalPaceReport`` for the gap, days left, and daily need.
        Closed pnl and configured stops are read from the paper DB with
        ``ro_connect``. A missing pnl or an unreadable HKT close day stays
        empty. Does not write the database, the environment, or any gate.
        """
        started = time.perf_counter()
        try:
            return _finish(self._paper_kpi_reverse(started, now), "paper_kpi_reverse")
        except Exception as exc:
            log.warning("read failed paper_kpi_reverse: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "paper_kpi_reverse")

    def m7_shadow_register(self, *, csv_path: Path | None = None) -> DataResult:
        """Observe-only M7 S0 / S1 / liq_missing counts.

        Prefer ``exports/m7_s0_s1_shadow_register.csv`` when that file exists.
        Otherwise tally ``decision_log.raw_json.extras.liquidity_usd`` for
        live_candidate buys. A missing or unreadable CSV does not raise.
        Does not write the register, the databases, or any gate.
        """
        started = time.perf_counter()
        try:
            return _finish(self._m7_shadow_register(started, csv_path), "m7_shadow_register")
        except Exception as exc:
            log.warning("read failed m7_shadow_register: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "m7_shadow_register")

    def risk_daily_loss_today(self) -> DataResult:

        """Observe-only: whether RISK daily_loss fired today (order_events / receipt)."""
        started = time.perf_counter()
        try:
            return _finish(self._risk_daily_loss_today(started), "risk_daily_loss_today")
        except Exception as exc:
            log.warning("read failed risk_daily_loss_today: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "risk_daily_loss_today")

    def rejection_buckets(self, days: int = 7) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._rejection_buckets(started, days), "rejection_buckets")
        except Exception as exc:
            log.warning("read failed rejection_buckets: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "rejection_buckets")

    def paper_closed_buckets(self, days: int = 14) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._paper_closed_buckets(started, days), "paper_closed_buckets")
        except Exception as exc:
            log.warning("read failed paper_closed_buckets: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "paper_closed_buckets")

    def _today_execution_stats(self, started: float) -> DataResult:
        path = self.paths.execution
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        lower = (start - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "execution_log", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "execution_log")
                absent = _require(cols, ("ts", "validation_ok", "executed"))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                rows = _exec(
                    conn,
                    "SELECT ts, validation_ok, executed FROM execution_log WHERE ts >= ?",
                    (lower,),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        n = n_ok = n_executed = 0
        for row in rows:
            if not _in_range(row["ts"], start, end, end_inclusive=False):
                continue
            n += 1
            if _as_int(row["validation_ok"]) == 1:
                n_ok += 1
            if _as_int(row["executed"]) == 1:
                n_executed += 1
        return _ok(
            RateBlock(
                n=n,
                n_ok=n_ok,
                n_executed=n_executed,
                ok_rate=(n_ok / n) if n else None,
                executed_rate=(n_executed / n) if n else None,
            ),
            started,
        )

    def _week_decision_stats(self, started: float) -> DataResult:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        decisions, error = self._decision_ids_between(start, end)
        if error:
            return _unavail(error, started)
        if not decisions:
            return _ok(
                DecisionWindowStats(0, 0, 0, 0, None, None, n_decisions_dedup=0),
                started,
            )
        latest, _dups, note = self._latest_execs([item[0] for item in decisions])
        if note:
            return _unavail(note, started)
        n_with = n_ok = n_executed = 0
        events: list[tuple[datetime | None, str]] = []
        for decision_id, ts, token in decisions:
            events.append((_optional_ts(ts), token))
            row = latest.get(decision_id)
            if row is None:
                continue
            n_with += 1
            if _as_int(_cell(row, "validation_ok")) == 1:
                n_ok += 1
            if _as_int(_cell(row, "executed")) == 1:
                n_executed += 1
        return _ok(
            DecisionWindowStats(
                n_decisions=len(decisions),
                n_with_exec=n_with,
                n_ok=n_ok,
                n_executed=n_executed,
                ok_rate=(n_ok / n_with) if n_with else None,
                executed_rate=(n_executed / n_with) if n_with else None,
                n_decisions_dedup=dedup_token_count(events),
            ),
            started,
        )

    def _recent_failures(self, started: float, limit: int) -> DataResult:
        path = self.paths.execution
        bounded = _clamp(limit, 1, 50)
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "execution_log", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "execution_log")
                absent = _require(cols, ("id", "validation_errors"))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                wanted = [
                    name
                    for name in (
                        "id",
                        "ts",
                        "decision_log_id",
                        "strategy_id",
                        "action",
                        "token_address",
                        "validation_ok",
                        "validation_errors",
                        "status",
                        "receipt_json",
                    )
                    if name in cols
                ]
                clauses = _rejection_sql_clauses(cols)
                if not clauses:
                    return _unavail(f"缺少字段：validation_errors（{path.name}）", started)
                scan = min(max(bounded * 5, bounded), 200)
                sql = (
                    f"SELECT {', '.join(wanted)} FROM execution_log "
                    f"WHERE {' OR '.join(clauses)} "
                    "ORDER BY id DESC LIMIT ?"
                )
                rows = _exec(conn, sql, (scan,)).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        kept: list[FailureRow] = []
        for row in rows:
            raw_errors = str(_cell(row, "validation_errors") or "")
            receipt = parse_json(str(_cell(row, "receipt_json") or ""))
            status_text = _cell(row, "status")
            key = rejection_key(
                _as_int(_cell(row, "validation_ok")),
                raw_errors,
                None if status_text is None else str(status_text),
                receipt or None,
            )
            if key is None:
                continue
            shown = validation_error_text(raw_errors).strip() or key
            kept.append(
                FailureRow(
                    id=int(row["id"]),
                    ts=str(_cell(row, "ts") or ""),
                    decision_log_id=_text_id(_cell(row, "decision_log_id")),
                    strategy_id=clip(_cell(row, "strategy_id"), 40),
                    action=clip(_cell(row, "action"), 24),
                    token_address=str(_cell(row, "token_address") or ""),
                    validation_errors=clip(shown, 180),
                )
            )
            if len(kept) >= bounded:
                break
        return _ok(tuple(kept), started)

    def _process_liveness(self, started: float) -> DataResult:
        path = self.paths.main
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        now = datetime.now(timezone.utc)
        try:
            with ro_connect(path) as conn:
                crawler = _probe_checkpoint(conn, now)
                indicator = _probe_indicator(conn, now)
                jev = _probe_max(conn, "jev_signals", "fetched_at", "JEV", now)
                if jev.state == "unknown":
                    jev = _probe_max(conn, "jev_signals", "analyzed_at", "JEV", now)
                decision = _probe_max(conn, "decision_log", "ts", "决策引擎", now)
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        gateway = self._probe_gateway(now)
        return _ok((crawler, indicator, jev, decision, gateway), started)

    def _probe_gateway(self, now: datetime) -> ProcessProbe:
        """Last execution_log.ts. Missing ledger is a dash, not a failed read of the main db."""
        path = self.paths.execution
        if not path.is_file():
            return ProcessProbe("执行网关", "unknown", None, "")
        try:
            with ro_connect(path) as conn:
                return _probe_max(
                    conn,
                    "execution_log",
                    "ts",
                    "执行网关",
                    now,
                    limit=gateway_stale_seconds(),
                )
        except sqlite3.Error:
            log.warning("gateway heartbeat unreadable")
            return ProcessProbe("执行网关", "unknown", None, "")

    def _latest_backtest(self, started: float) -> DataResult:
        path = self.paths.backtest
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "runs", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "runs")
                if "run_id" not in cols:
                    return _unavail(f"缺少字段：run_id（{path.name}）", started)
                wanted = [
                    name
                    for name in (
                        "run_id",
                        "started_at",
                        "finished_at",
                        "strategy_id",
                        "start_cash",
                        "end_equity",
                        "status",
                        "notes",
                    )
                    if name in cols
                ]
                order = "started_at DESC" if "started_at" in cols else "rowid DESC"
                row = _exec(
                    conn,
                    f"SELECT {', '.join(wanted)} FROM runs ORDER BY {order} LIMIT 1",
                ).fetchone()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        if row is None:
            return _ok(None, started)
        return _ok(
            BacktestRun(
                run_id=clip(_cell(row, "run_id"), 64),
                started_at=str(_cell(row, "started_at") or ""),
                finished_at=str(_cell(row, "finished_at") or ""),
                strategy_id=clip(_cell(row, "strategy_id"), 40),
                start_cash=safe_float(_cell(row, "start_cash")),
                end_equity=safe_float(_cell(row, "end_equity")),
                status=clip(_cell(row, "status"), 32),
                notes=clip(_cell(row, "notes"), 120),
            ),
            started,
        )

    def _latest_news(self, started: float, limit: int) -> DataResult:
        path = self.paths.news
        bounded = _clamp(limit, 1, 20)
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "news_events", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "news_events")
                wanted = [
                    name
                    for name in (
                        "id",
                        "source",
                        "source_tier",
                        "title",
                        "url",
                        "published_at",
                        "onchain_status",
                    )
                    if name in cols
                ]
                if not wanted:
                    return _unavail(f"缺少字段：news_events（{path.name}）", started)
                order = "id DESC" if "id" in cols else "rowid DESC"
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(wanted)} FROM news_events ORDER BY {order} LIMIT ?",
                    (bounded,),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        payload = tuple(
            NewsItem(
                id=_text_id(_cell(row, "id")),
                source=clip(_cell(row, "source"), 40),
                source_tier=clip(_cell(row, "source_tier"), 24),
                title=clip(_cell(row, "title"), 140),
                url=clip(_cell(row, "url"), 300),
                published_at=str(_cell(row, "published_at") or ""),
                onchain_status=clip(_cell(row, "onchain_status"), 32),
            )
            for row in rows
        )
        return _ok(payload, started)

    def _open_positions(self, started: float) -> DataResult:
        path = self.paths.paper
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "positions", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "positions")
                if "pair_address" not in cols and "token_address" not in cols:
                    return _unavail(f"缺少字段：pair_address（{path.name}）", started)
                selected = [name for name in _POSITION_FIELDS if name in cols]
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(selected)} FROM positions",
                ).fetchall()
                cash, cash_note = _read_cash(conn, path.name)
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)

        open_rows = [row for row in rows if _is_open(row, cols)]
        keys = [
            (str(_cell(row, "chain_id") or ""), str(_cell(row, "pair_address") or ""))
            for row in open_rows
        ]
        token_keys = [
            (str(_cell(row, "chain_id") or ""), str(_cell(row, "token_address") or ""))
            for row in open_rows
        ]
        quotes, symbols, price_note = self._market_lookups(keys, token_keys)
        built: list[PositionRow] = []
        for row in open_rows:
            chain = str(_cell(row, "chain_id") or "")
            pair = str(_cell(row, "pair_address") or "")
            token = str(_cell(row, "token_address") or "")
            entry = _positive(_cell(row, "entry_price"))
            qty = _positive(_cell(row, "qty"))
            size_usd = safe_float(_cell(row, "size_usd"))
            mark, _liq = quotes.get((chain, pair), (None, None))
            pnl_pct, pnl_usd = _unrealized(entry, mark, qty, size_usd)
            opened = _cell(row, "opened_at") or _cell(row, "open_ts")
            raw_id = _cell(row, "id")
            row_id = _text_id(raw_id) if raw_id is not None else f"row-{len(built) + 1}"
            built.append(
                PositionRow(
                    id=row_id,
                    chain_id=chain,
                    pair_address=pair,
                    token_address=token,
                    symbol=symbols.get((chain, token), ""),
                    entry_price=entry,
                    mark_price=mark,
                    pnl_pct=pnl_pct,
                    pnl_usd=pnl_usd,
                    mtm_usd=_mtm(entry, mark, qty, size_usd),
                    size_usd=size_usd,
                    tp_pct=safe_float(_cell(row, "tp_pct")),
                    sl_pct=safe_float(_cell(row, "sl_pct")),
                    max_hold_min=safe_float(_cell(row, "max_hold_min")),
                    hold_seconds=_hold_seconds(opened),
                    status=clip(_cell(row, "status") or "open", 24),
                )
            )
        built.sort(key=lambda item: item.hold_seconds if item.hold_seconds is not None else -1)
        invested = 0.0
        for item in built:
            if item.mtm_usd is not None:
                invested += item.mtm_usd
        if cash is None:
            equity = invested if built else None
        else:
            equity = cash + invested
        ratio = (invested / equity) if equity not in (None, 0) else (0.0 if equity == 0 and invested == 0 else None)
        if equity == 0 and invested == 0:
            ratio = None
        missing_price = sum(1 for item in built if item.mark_price is None)
        return _ok(
            PositionsPayload(
                cash_usd=cash,
                cash_note=cash_note,
                equity_usd=equity,
                invested_usd=invested,
                position_ratio=ratio,
                open_count=len(built),
                missing_price=missing_price,
                price_note=price_note,
                rows=tuple(built),
            ),
            started,
        )

    def _recent_signals(self, started: float, limit: int) -> DataResult:
        path = self.paths.main
        bounded = _clamp(limit, 1, 50)
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "decision_log", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "decision_log")
                absent = _require(cols, ("id", "ts"))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                wanted = [
                    name
                    for name in (
                        "id",
                        "ts",
                        "strategy_id",
                        "action",
                        "chain_id",
                        "token_address",
                        "reason",
                        "raw_json",
                    )
                    if name in cols
                ]
                decisions = _exec(
                    conn,
                    f"SELECT {', '.join(wanted)} FROM decision_log ORDER BY id DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
                coverage = _coverage_map(conn, decisions)
                parsed = [
                    (row, parse_json(str(_cell(row, "raw_json") or "")))
                    for row in decisions
                ]
                needs_strength = [
                    row
                    for row, raw in parsed
                    if extract_jev_strength(raw, str(_cell(row, "reason") or "")) is None
                ]
                strength_fallback = _strength_fallback(conn, needs_strength)
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)

        ids = [int(row["id"]) for row, _raw in parsed]
        latest, dups, exec_note = self._latest_execs(ids)
        quote_keys: list[tuple[str, str]] = []
        resolved_pairs: list[str] = []
        for row, raw in parsed:
            pair = extract_pair(raw) or ""
            exec_row = latest.get(int(row["id"]))
            if not pair and exec_row is not None:
                pair = str(_cell(exec_row, "pair_address") or "")
            resolved_pairs.append(pair)
            quote_keys.append((str(_cell(row, "chain_id") or ""), pair))
        quotes: dict[tuple[str, str], tuple[float | None, float | None]] = {}
        try:
            if path.is_file():
                with ro_connect(path) as conn:
                    if _table_exists(conn, "pair_history"):
                        quotes = _load_quotes(conn, quote_keys)
        except sqlite3.Error:
            quotes = {}
        rows_out: list[SignalRow] = []
        for row, raw in parsed:
            decision_id = int(row["id"])
            chain = str(_cell(row, "chain_id") or "")
            token = str(_cell(row, "token_address") or "")
            reason = str(_cell(row, "reason") or "")
            pair = resolved_pairs[len(rows_out)]
            exec_row = latest.get(decision_id)
            strength = extract_jev_strength(raw, reason)
            if strength is None:
                strength = strength_fallback.get((chain, token))
            cov = coverage.get((chain, token))
            if cov is None:
                cov = extract_coverage(raw)
            quote = quotes.get((chain, pair))
            liquidity = quote[1] if quote else None
            if liquidity is None:
                liquidity = extract_liquidity_usd(raw)
            rows_out.append(
                SignalRow(
                    decision_id=decision_id,
                    ts=str(_cell(row, "ts") or ""),
                    strategy_id=clip(_cell(row, "strategy_id"), 40),
                    action=clip(_cell(row, "action"), 24),
                    chain_id=chain,
                    token_address=token,
                    pair_address=pair,
                    jev_strength=strength,
                    coverage=cov,
                    liquidity_usd=liquidity,
                    reason=clip(reason, 120),
                    validation_ok=_as_int(_cell(exec_row, "validation_ok")) if exec_row is not None else None,
                    validation_errors=clip(_cell(exec_row, "validation_errors"), 160) if exec_row is not None else "",
                    executed=_as_int(_cell(exec_row, "executed")) if exec_row is not None else None,
                    executor=clip(_cell(exec_row, "executor"), 40) if exec_row is not None else "",
                    exec_dup=decision_id in dups,
                )
            )
        return _ok(SignalPayload(tuple(rows_out), exec_note), started)

    def _recent_executions(self, started: float, limit: int) -> DataResult:
        path = self.paths.execution
        bounded = _clamp(limit, 1, 50)
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "execution_log", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "execution_log")
                if "id" not in cols:
                    return _unavail(f"缺少字段：id（{path.name}）", started)
                wanted = [
                    name
                    for name in (
                        "id",
                        "ts",
                        "decision_log_id",
                        "strategy_id",
                        "action",
                        "token_address",
                        "executed",
                        "executor",
                        "receipt_json",
                        "validation_ok",
                        "validation_errors",
                    )
                    if name in cols
                ]
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(wanted)} FROM execution_log ORDER BY id DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        payload = tuple(
            ExecutionRow(
                id=int(row["id"]),
                ts=str(_cell(row, "ts") or ""),
                decision_log_id=_text_id(_cell(row, "decision_log_id")),
                strategy_id=clip(_cell(row, "strategy_id"), 40),
                action=clip(_cell(row, "action"), 24),
                token_address=str(_cell(row, "token_address") or ""),
                executed=_as_int(_cell(row, "executed")),
                executor=clip(_cell(row, "executor"), 40),
                receipt_summary=receipt_summary(parse_json(str(_cell(row, "receipt_json") or ""))),
                validation_ok=_as_int(_cell(row, "validation_ok")),
                validation_errors=clip(_cell(row, "validation_errors"), 160),
            )
            for row in rows
        )
        return _ok(payload, started)

    def _paper_signal_streaks(self, started: float, days: int) -> DataResult:
        path = self.paths.paper
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        window = max(1, min(int(days), 60))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).strftime("%Y-%m-%d")
        try:
            with ro_connect(path) as conn:
                if "positions" not in {
                    str(r[0]) for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }:
                    return _unavail("缺少 positions 表", started)
                rows = conn.execute(
                    "SELECT closed_at, pnl_usd, meta_json FROM positions "
                    "WHERE status = 'closed' AND closed_at IS NOT NULL "
                    "ORDER BY closed_at ASC"
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        by: dict[str, list[int]] = {}
        for row in rows:
            closed_at = str(_cell(row, "closed_at") or "")
            if closed_at[:10] < cutoff:
                continue
            meta_raw = _cell(row, "meta_json")
            sid = "unknown"
            if meta_raw:
                try:
                    meta = json.loads(str(meta_raw))
                    if isinstance(meta, dict) and meta.get("strategy_id"):
                        sid = str(meta.get("strategy_id"))
                except Exception:
                    pass
            pnl = safe_float(_cell(row, "pnl_usd")) or 0.0
            by.setdefault(sid, []).append(1 if pnl > 0 else 0)
        reports: list[SignalStreakReport] = []
        for sid, seq in sorted(by.items()):
            streak = 0
            max_s = 0
            cur = 0
            for bit in seq:
                if bit == 0:
                    streak += 1
                    max_s = max(max_s, streak)
                    cur = streak
                else:
                    streak = 0
                    cur = 0
            after2: list[int] = []
            for i in range(len(seq) - 2):
                if seq[i] == 0 and seq[i + 1] == 0:
                    after2.append(seq[i + 2])
            rate = (sum(1 for x in after2 if x == 0) / len(after2)) if after2 else None
            reports.append(
                SignalStreakReport(
                    strategy_id=sid,
                    closed=len(seq),
                    max_loss_streak=max_s,
                    current_loss_streak=cur,
                    cond_n=len(after2),
                    cond_third_loss_rate=round(rate, 4) if rate is not None else None,
                )
            )
        return _ok(tuple(reports), started)

    def _paper_exit_holds(self, started: float, days: int) -> DataResult:
        path = self.paths.paper
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        window = max(1, min(int(days), 60))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).strftime("%Y-%m-%d")
        try:
            with ro_connect(path) as conn:
                rows = conn.execute(
                    "SELECT exit_reason, opened_at, closed_at, pnl_usd FROM positions "
                    "WHERE status = 'closed' AND closed_at IS NOT NULL"
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        buckets: dict[str, list[float]] = {}
        pnls: dict[str, float] = {}
        for row in rows:
            closed_at = str(_cell(row, "closed_at") or "")
            if closed_at[:10] < cutoff:
                continue
            er = str(_cell(row, "exit_reason") or "unknown")
            opened = str(_cell(row, "opened_at") or "")
            try:
                a = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                b = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                mins = (b - a).total_seconds() / 60.0
            except Exception:
                continue
            buckets.setdefault(er, []).append(mins)
            pnls[er] = pnls.get(er, 0.0) + (safe_float(_cell(row, "pnl_usd")) or 0.0)
        out: list[ExitHoldBucket] = []
        for er, holds in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            holds_sorted = sorted(holds)
            n = len(holds_sorted)
            mid = holds_sorted[n // 2] if n else None
            if n and n % 2 == 0:
                mid = (holds_sorted[n // 2 - 1] + holds_sorted[n // 2]) / 2.0
            mean = (sum(holds_sorted) / n) if n else None
            out.append(
                ExitHoldBucket(
                    exit_reason=er,
                    count=n,
                    median_hold_min=round(mid, 2) if mid is not None else None,
                    mean_hold_min=round(mean, 2) if mean is not None else None,
                    pnl_usd=round(pnls.get(er, 0.0), 4),
                )
            )
        return _ok(ExitHoldReport(window, tuple(out)), started)

    def _paper_trade_frequency(self, started: float, days: int) -> DataResult:
        path = self.paths.paper
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        window = max(1, min(int(days), 60))
        try:
            with ro_connect(path) as conn:
                tabs = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                opens_by: dict[str, int] = {}
                closed_by: dict[str, list] = {}
                if "fills" in tabs:
                    for row in conn.execute(
                        "SELECT created_at FROM fills WHERE note = 'open' OR side = 'buy'"
                    ).fetchall():
                        day = _hkt_day(row[0] if row else "")
                        if day:
                            opens_by[day] = opens_by.get(day, 0) + 1
                if "positions" in tabs:
                    for row in conn.execute(
                        "SELECT closed_at, pnl_usd FROM positions "
                        "WHERE status = 'closed' AND closed_at IS NOT NULL AND closed_at != ''"
                    ).fetchall():
                        day = _hkt_day(row[0] if row else "")
                        if not day:
                            continue
                        slot = closed_by.setdefault(day, [0, 0.0])
                        slot[0] += 1
                        slot[1] += float(row[1] or 0.0)
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        days_sorted = sorted(set(opens_by) | set(closed_by), reverse=True)[:window]
        rows = tuple(
            PaperTradeDay(
                day=day,
                opens=int(opens_by.get(day, 0)),
                closed=int((closed_by.get(day) or [0, 0.0])[0]),
                pnl_usd=round(float((closed_by.get(day) or [0, 0.0])[1]), 4),
            )
            for day in sorted(days_sorted)
        )
        total_opens = sum(r.opens for r in rows)
        total_closed = sum(r.closed for r in rows)
        total_pnl = round(sum(r.pnl_usd for r in rows), 4)
        active = [r for r in rows if r.opens or r.closed]
        avg = (total_opens / len(active)) if active else None
        return _ok(
            PaperTradeFrequencyReport(
                window,
                rows,
                total_opens,
                total_closed,
                total_pnl,
                avg_opens_per_active_day=round(avg, 2) if avg is not None else None,
            ),
            started,
        )

    def _intel_holder_observe(self, started: float, limit: int) -> DataResult:
        path = self.paths.main
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        sample = max(1, min(int(limit), 50))
        try:
            readers = _holder_readers()
        except (OSError, ImportError, SyntaxError) as exc:
            return _unavail(f"查询失败：{_safe_exc(exc)}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "token_holder_snapshots", path.name)
                if missing:
                    return _unavail(missing, started)
                for table in HOLDER_SUSPICIOUS_TABLES:
                    if _table_exists(conn, table):
                        _exec(conn, f"SELECT 1 FROM {_ident(table)} LIMIT 1").fetchone()
                token_rows = _exec(
                    conn,
                    "SELECT chain_id, token_address, MAX(recorded_at) AS recorded_at "
                    "FROM token_holder_snapshots "
                    "GROUP BY chain_id, token_address "
                    "ORDER BY recorded_at DESC "
                    "LIMIT ?",
                    (sample,),
                ).fetchall()
                points: list[IntelHolderPoint] = []
                snapshot_n = 0
                non_null_share_n = 0
                non_null_delta_n = 0
                for row in token_rows:
                    chain = str(row["chain_id"] or "")
                    token = str(row["token_address"] or "")
                    history = readers.read_top10_share_history(conn, chain, token)
                    delta = readers.read_dev_balance_delta(conn, chain, token)
                    if delta is not None:
                        non_null_delta_n += 1
                    if not history:
                        points.append(
                            IntelHolderPoint(chain, token, None, None, None, delta)
                        )
                        continue
                    for item in history:
                        share = item.get("top10_share")
                        share_value = share if isinstance(share, (int, float)) and not isinstance(share, bool) else None
                        snapshot_n += 1
                        if share_value is not None:
                            non_null_share_n += 1
                        points.append(
                            IntelHolderPoint(
                                chain,
                                token,
                                _text_or_none(item.get("snapshot_id")),
                                _text_or_none(item.get("recorded_at")),
                                share_value,
                                delta,
                            )
                        )
        except sqlite3.OperationalError as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        except sqlite3.DatabaseError:
            return _unavail(SOURCE_TABLE_DAMAGE_BANNER, started)
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        latest = tuple(sorted(points, key=_holder_point_key, reverse=True)[:_INTEL_HOLDER_SHOW])
        gap = "" if non_null_share_n or non_null_delta_n else "empty"
        return _ok(
            IntelHolderReport(
                snapshot_n=snapshot_n,
                non_null_share_n=non_null_share_n,
                non_null_delta_n=non_null_delta_n,
                token_n=len(token_rows),
                latest=latest,
                weight=0.0,
                wired=False,
                gap=gap,
            ),
            started,
        )

    def _intel_slippage_observe(self, started: float) -> DataResult:
        ledger_path = self.paths.execution
        paper_path = self.paths.paper
        crawler_path = self.paths.main
        ledger = ledger_path if ledger_path.is_file() else None
        paper = paper_path if paper_path.is_file() else None
        crawler = crawler_path if crawler_path.is_file() else None
        if ledger is None and paper is None and crawler is None:
            return _unavail(
                "数据库文件不存在：execution_ledger.db / paper_trading.db / dexscreener.db",
                started,
            )
        try:
            slip_table = _file_has_table(ledger, "execution_log") or _file_has_table(paper, "fills")
            bot_table = _file_has_table(crawler, "bot_fill_observations")
            if bot_table and crawler is not None:
                _probe_table(crawler, "bot_fill_observations")
        except sqlite3.OperationalError as exc:
            return _unavail(f"查询失败：{_safe_exc(exc)}", started)
        except sqlite3.DatabaseError:
            return _unavail(source_table_banner(SLIP_SUSPICIOUS_TABLES), started)
        except sqlite3.Error as exc:
            return _unavail(f"查询失败：{_safe_exc(exc)}", started)
        if not slip_table and not bot_table:
            return _unavail(
                "缺少数据表：execution_log / fills / bot_fill_observations",
                started,
            )
        try:
            report = build_slippage_report(ledger=ledger, paper=paper, crawler_db=crawler)
        except sqlite3.OperationalError as exc:
            return _unavail(f"查询失败：{_safe_exc(exc)}", started)
        except sqlite3.DatabaseError:
            return _unavail(source_table_banner(SLIP_SUSPICIOUS_TABLES), started)
        except (OSError, sqlite3.Error) as exc:
            return _unavail(f"查询失败：{_safe_exc(exc)}", started)
        slip = report.get("slippage_1h") if isinstance(report, dict) else None
        bot = report.get("bot_trade_share") if isinstance(report, dict) else None
        skip = report.get("high_bot_skip") if isinstance(report, dict) else None
        if not isinstance(slip, dict) or not isinstance(bot, dict):
            return _unavail("查询失败：slippage report 不完整", started)
        n = _as_int(slip.get("n")) or 0
        mean = safe_float(slip.get("mean")) if n else None
        p50 = safe_float(slip.get("p50")) if n else None
        p90 = safe_float(slip.get("p90")) if n else None
        source = str(bot.get("source") or "")
        status = str(bot.get("status") or "data_gap")
        share = safe_float(bot.get("share"))
        # Collection #81 is the headline. Receipt tags stay a gap, not a share.
        if source != "bot_fill_observations" or status != "observed" or share is None:
            share = None
            if status == "observed":
                status = "data_gap"
        fills = _as_int(bot.get("total_fills"))
        bot_fills = _as_int(bot.get("bot_fills"))
        wired = bool(skip.get("wired")) if isinstance(skip, dict) else False
        if mean is None and share is None:
            gap = "both"
        elif mean is None:
            gap = "slippage"
        elif share is None:
            gap = "bot"
        else:
            gap = ""
        return _ok(
            IntelSlippageReport(
                slippage_n=n,
                mean=mean,
                p50=p50,
                p90=p90,
                bot_share=share,
                bot_status=status,
                bot_source=source or "bot_fill_observations",
                bot_fills=bot_fills,
                total_fills=fills,
                high_bot_skip_wired=wired,
                gap=gap,
            ),
            started,
        )

    def _intel_crowding_note(self, started: float) -> DataResult:
        path = self.paths.main
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            crowding = _crowding_module()
        except (OSError, ImportError, SyntaxError) as exc:
            return _unavail(f"查询失败：{_safe_exc(exc)}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "decision_log", path.name)
                if missing:
                    return _unavail(missing, started)
                report = crowding.compute_for_weekly(
                    decision_conn=conn,
                    strategy_id=_INTEL_CROWD_STRATEGY,
                    window_days=7,
                )
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        if not getattr(report, "available", False):
            return _unavail(str(getattr(report, "note", "") or "查询失败"), started)
        live = report.cohort("live_candidate")
        sample = report.cohort("sample")
        live_n = int(live.n_buys) if live is not None else None
        sample_n = int(sample.n_buys) if sample is not None else None
        peak = None
        if live is not None and live_n:
            peak = safe_float(live.peak_hour_density)
        note = IntelCrowdingNote(
            strategy_id=_INTEL_CROWD_STRATEGY,
            live_n=live_n,
            sample_n=sample_n,
            live_peak_hour_density=peak,
            line=_crowding_line(_INTEL_CROWD_STRATEGY, live_n, sample_n, peak),
        )
        return _ok(note, started)

    def _token_age_observe(self, started: float, limit: int) -> DataResult:
        path = self.paths.main
        window = _clamp(limit, 1, 2000)
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "decision_log", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "decision_log")
                absent = _require(cols, ("id", "ts", "raw_json"))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                selected = ["id", "ts", "raw_json"]
                if "token_address" in cols:
                    selected.append("token_address")
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(selected)} FROM decision_log ORDER BY id DESC LIMIT ?",
                    (window,),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        counts = {label: 0 for label in _TOKEN_AGE_LABELS}
        with_field = 0
        samples: list[TokenAgeSample] = []
        age_events: list[tuple[datetime | None, str]] = []
        for row in rows:
            age_events.append((_optional_ts(_cell(row, "ts")), str(_cell(row, "token_address") or "")))
            raw_text = _cell(row, "raw_json")
            raw = parse_json(None if raw_text is None else str(raw_text))
            created = _extras_pair_created_ms(raw)
            age_days = _token_age_days(_cell(row, "ts"), created) if created is not None else None
            if created is not None:
                with_field += 1
            label = _token_age_label(age_days)
            counts[label] += 1
            if age_days is not None and len(samples) < _TOKEN_AGE_SAMPLES:
                decision_id = _as_int(_cell(row, "id"))
                if decision_id is not None:
                    samples.append(TokenAgeSample(decision_id, age_days))
        total = len(rows)
        unknown = counts["unknown"]
        ratio = (unknown / total) if total else None
        buckets = tuple(TokenAgeBucket(label, counts[label]) for label in _TOKEN_AGE_LABELS)
        return _ok(
            TokenAgeReport(
                total=total,
                with_field=with_field,
                unknown=unknown,
                unknown_ratio=round(ratio, 4) if ratio is not None else None,
                buckets=buckets,
                samples=tuple(samples),
                limit=window,
                total_dedup=dedup_token_count(age_events),
            ),
            started,
        )

    def _paper_goal_pace(self, started: float, now: datetime | None) -> DataResult:
        path = self.paths.paper
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        as_of = _goal_as_of(now)
        try:
            with ro_connect(path) as conn:
                cash, cash_note = _read_cash(conn, path.name)
                if cash is None:
                    return _unavail(cash_note or "账户无现金记录", started)
                missing = _missing_table(conn, "positions", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "positions")
                absent = _require(cols, ("size_usd",))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                selected = [
                    name
                    for name in ("id", "size_usd", "status", "closed_at", "exit_reason")
                    if name in cols
                ]
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(selected)} FROM positions",
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        fetched = set(selected)
        open_size = 0.0
        open_count = 0
        size_gaps = 0
        for row in rows:
            if not _is_open(row, fetched):
                continue
            open_count += 1
            size = safe_float(_cell(row, "size_usd"))
            if size is None:
                size_gaps += 1
                continue
            open_size += float(size)
        equity = round(float(cash) + open_size, 4)
        gap = round(GOAL_TARGET_USD - equity, 4)
        days_left = _goal_days_left(as_of)
        note = f"{size_gaps} 笔未平缺 size_usd，未计入名义" if size_gaps else ""
        up_usd, down_usd, up_tp_ids, from_annotations = _load_bad_ticks(path)
        raw_equity = round(equity + 0.0, 2)
        conservative = round(raw_equity + up_usd, 2)
        both = round(raw_equity + up_usd + down_usd, 2)
        return _ok(
            GoalPaceReport(
                cash_usd=float(cash),
                open_size_usd=round(open_size, 4),
                open_count=open_count,
                size_gap_count=size_gaps,
                equity_usd=equity,
                target_usd=GOAL_TARGET_USD,
                gap_usd=gap,
                as_of_day=as_of.isoformat(),
                start_day=GOAL_START_DAY.isoformat(),
                deadline_day=GOAL_DEADLINE_DAY.isoformat(),
                days_left=days_left,
                daily_need_usd=_goal_daily_need(gap, days_left),
                mark_to_market=False,
                note=note,
                equity_raw_usd=raw_equity,
                equity_conservative_usd=conservative,
                equity_down_only_usd=round(raw_equity + down_usd, 2),
                equity_both_usd=both,
                gap_raw_usd=round(GOAL_TARGET_USD - raw_equity, 2),
                gap_conservative_usd=round(GOAL_TARGET_USD - conservative, 2),
                true_tp_line=_true_tp_line(rows, fetched, up_tp_ids, from_annotations),
            ),
            started,
        )

    def _paper_kpi_reverse(self, started: float, now: datetime | None) -> DataResult:
        pace = self._paper_goal_pace(started, now)
        if not pace.available or not isinstance(pace.data, GoalPaceReport):
            return pace
        path = self.paths.paper
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "positions", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "positions")
                selected = [
                    name
                    for name in ("size_usd", "status", "closed_at", "pnl_usd", "sl_pct")
                    if name in cols
                ]
                if not selected:
                    rows: list[sqlite3.Row] = []
                else:
                    rows = list(
                        _exec(
                            conn,
                            f"SELECT {', '.join(selected)} FROM positions",
                        ).fetchall()
                    )
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        return _ok(_kpi_reverse_from_rows(pace.data, rows, set(selected)), started)

    def _paper_pause_split(self, started: float) -> DataResult:
        path = self.paths.paper
        flag_present, flag_note = paper_pause_flag_state()
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "positions", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "positions")
                if "opened_at" not in cols:
                    return _ok(
                        PaperPauseSplit(0, 0, flag_present, flag_note),
                        started,
                    )
                rows = _exec(conn, "SELECT opened_at FROM positions").fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        pause = PAPER_OPEN_PAUSE_AT
        before = 0
        after = 0
        for row in rows:
            opened = _optional_ts(_cell(row, "opened_at"))
            if opened is None:
                continue
            if opened > pause:
                after += 1
            else:
                before += 1
        return _ok(PaperPauseSplit(before, after, flag_present, flag_note), started)

    def _m7_csv_candidates(self) -> list[Path]:
        root = db_root()
        ordered = (
            root / "exports" / _M7_CSV_NAME,
            root.parent / "exports" / _M7_CSV_NAME,
            root / "crawler" / "exports" / _M7_CSV_NAME,
            self.paths.main.parent / "exports" / _M7_CSV_NAME,
        )
        found: list[Path] = []
        seen: set[str] = set()
        for path in ordered:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
        return found

    def _m7_locate_csv(self, csv_path: Path | None) -> Path | None:
        if csv_path is not None:
            return csv_path if csv_path.is_file() else None
        for candidate in self._m7_csv_candidates():
            if candidate.is_file():
                return candidate
        return None

    def _m7_from_decision_log(self, started: float, *, source_suffix: str) -> DataResult:
        path = self.paths.main
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "decision_log", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "decision_log")
                absent = _require(cols, ("action", "raw_json"))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                selected = ["action", "raw_json"]
                if "id" in cols:
                    selected.insert(0, "id")
                if "ts" in cols:
                    selected.append("ts")
                if "token_address" in cols:
                    selected.append("token_address")
                fetched = _exec(
                    conn,
                    f"SELECT {', '.join(selected)} FROM decision_log",
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        by_id: dict[str, _M7Obs] = {}
        anon: list[_M7Obs] = []
        dupes = 0
        for row in fetched:
            raw_text = _cell(row, "raw_json")
            raw = parse_json(None if raw_text is None else str(raw_text))
            raw_id = _cell(row, "id")
            decision_id = None if raw_id is None else str(raw_id)
            action_cell = _cell(row, "action")
            action = None if action_cell is None else str(action_cell)
            token = str(_cell(row, "token_address") or "")
            obs = _m7_obs_from_raw(
                raw,
                action,
                decision_id,
                token=token,
                ts=_optional_ts(_cell(row, "ts")),
            )
            if obs.decision_id is None:
                anon.append(obs)
                continue
            if obs.decision_id in by_id:
                dupes += 1
            by_id[obs.decision_id] = obs
        report = _m7_report(
            list(by_id.values()) + anon,
            dupes,
            origin="decision_log",
            source=_M7_DB_SOURCE + source_suffix,
            note="",
        )
        return _ok(report, started)

    def _m7_shadow_register(self, started: float, csv_path: Path | None) -> DataResult:
        path = self._m7_locate_csv(csv_path)
        csv_failed = ""
        if path is not None:
            try:
                rows, dupes = _m7_read_csv(path)
            except (OSError, UnicodeError, csv.Error) as exc:
                csv_failed = _safe_exc(exc)
            else:
                return _ok(
                    _m7_report(
                        rows,
                        dupes,
                        origin="csv",
                        source=_m7_source_label(path),
                        note="",
                    ),
                    started,
                )
        suffix = "（CSV 无法读取）" if csv_failed else "（CSV 缺）"
        db_result = self._m7_from_decision_log(started, source_suffix=suffix)
        if db_result.available and db_result.data is not None:
            if csv_failed and isinstance(db_result.data, M7ShadowReport):
                note = db_result.data.note
                extra = csv_failed
                joined = f"{note} · {extra}" if note else extra
                return _ok(
                    M7ShadowReport(
                        n_s1=db_result.data.n_s1,
                        n_s0=db_result.data.n_s0,
                        n_missing=db_result.data.n_missing,
                        n_leak=db_result.data.n_leak,
                        n_phase0=db_result.data.n_phase0,
                        would_open_s0=db_result.data.would_open_s0,
                        would_open_s1=db_result.data.would_open_s1,
                        cohort=db_result.data.cohort,
                        source=db_result.data.source,
                        source_kind=db_result.data.source_kind,
                        counted=db_result.data.counted,
                        dupes=db_result.data.dupes,
                        note=joined,
                        n_s1_dedup=db_result.data.n_s1_dedup,
                        n_s0_dedup=db_result.data.n_s0_dedup,
                        n_missing_dedup=db_result.data.n_missing_dedup,
                        n_leak_dedup=db_result.data.n_leak_dedup,
                        n_phase0_dedup=db_result.data.n_phase0_dedup,
                        would_open_s0_dedup=db_result.data.would_open_s0_dedup,
                        would_open_s1_dedup=db_result.data.would_open_s1_dedup,
                    ),
                    started,
                )
            return db_result
        if csv_failed:
            return _unavail(f"CSV 无法读取；{db_result.reason}", started)
        return _unavail(f"CSV 缺 · {db_result.reason}", started)

    def _risk_daily_loss_today(self, started: float) -> DataResult:
        path = self.paths.execution
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            from zoneinfo import ZoneInfo
            now_hkt = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        except Exception:
            now_hkt = datetime.now(timezone.utc)
        today = now_hkt.strftime("%Y-%m-%d")
        cutoff = (now_hkt.astimezone(timezone.utc) - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%S")
        hit = False
        detail = ""
        event_day = today
        try:
            with ro_connect(path) as conn:
                tabs = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "order_events" in tabs:
                    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(order_events)")}
                    if "detail_json" in cols and "event" in cols:
                        rows = conn.execute(
                            "SELECT detail_json, event_ts FROM order_events "
                            "WHERE event = 'rejected' ORDER BY id DESC LIMIT 120"
                        ).fetchall()
                        for row in rows:
                            blob = str(row[0] or "")
                            ts = str(row[1] or "")
                            if "daily_loss" not in blob:
                                continue
                            if ts >= cutoff or today in blob or (ts[:10] == today):
                                hit = True
                                detail = blob[:160]
                                event_day = ts[:10] or today
                                break
                if not hit and "execution_log" in tabs:
                    rows = conn.execute(
                        "SELECT receipt_json, ts FROM execution_log "
                        "ORDER BY id DESC LIMIT 60"
                    ).fetchall()
                    for row in rows:
                        blob = str(row[0] or "")
                        ts = str(row[1] or "")
                        if "daily_loss" not in blob:
                            continue
                        if ts >= cutoff or today in blob:
                            hit = True
                            detail = "receipt daily_loss"
                            event_day = ts[:10] or today
                            break
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        return _ok({"hit": hit, "day": event_day, "detail": detail}, started)

    def _rejection_buckets(self, started: float, days: int) -> DataResult:
        path = self.paths.execution
        window_days = _clamp(days, 1, 30)
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=window_days)
        lower = (start - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "execution_log", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "execution_log")
                absent = _require(cols, ("ts", "validation_ok", "validation_errors"))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                selected = ["ts", "validation_ok", "validation_errors"]
                if "status" in cols:
                    selected.append("status")
                if "receipt_json" in cols:
                    selected.append("receipt_json")
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(selected)} FROM execution_log WHERE ts >= ?",
                    (lower,),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        counts: dict[str, int] = defaultdict(int)
        total = 0
        for row in rows:
            if not _in_range(row["ts"], start, end, end_inclusive=True):
                continue
            receipt = parse_json(str(_cell(row, "receipt_json") or ""))
            status_text = _cell(row, "status")
            key = rejection_key(
                _as_int(row["validation_ok"]),
                str(row["validation_errors"] or ""),
                None if status_text is None else str(status_text),
                receipt or None,
            )
            if key is None:
                continue
            counts[key] += 1
            total += 1
        ordered = tuple(
            RejectionBucket(key=key, count=count)
            for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        )
        return _ok(RejectionReport(window_days, total, ordered), started)

    def _paper_closed_buckets(self, started: float, days: int) -> DataResult:
        path = self.paths.paper
        window_days = _clamp(days, 1, 60)
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=window_days)
        lower = (start - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "positions", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "positions")
                need = ("status", "closed_at", "exit_reason", "pnl_usd")
                absent = _require(cols, need)
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                selected = ["status", "closed_at", "exit_reason", "pnl_usd"]
                if "pair_address" in cols:
                    selected.append("pair_address")
                if "opened_at" in cols:
                    selected.append("opened_at")
                if "token_address" in cols:
                    selected.append("token_address")
                if "meta_json" in cols:
                    selected.append("meta_json")
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(selected)} FROM positions WHERE status = ? AND closed_at >= ?",
                    ("closed", lower),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        decision_ids: list[int] = []
        for row in rows:
            meta = parse_json(str(_cell(row, "meta_json") or ""))
            if not isinstance(meta, dict):
                continue
            raw_id = meta.get("decision_log_id")
            if isinstance(raw_id, bool):
                continue
            if isinstance(raw_id, (int, float)):
                decision_ids.append(int(raw_id))
            elif isinstance(raw_id, str) and raw_id.strip().isdigit():
                decision_ids.append(int(raw_id.strip()))
        purposes = self._decision_purposes(decision_ids)
        agg: dict[tuple[str, str], list[float]] = defaultdict(list)
        closed = 0
        total_pnl = 0.0
        pnls: list[float] = []
        # pair -> (opened_at sort key, closed_at, hit, purpose, token, opened)
        first_by_pair: dict[str, tuple[str, str, bool, str | None, str, datetime | None]] = {}
        for row in rows:
            if not _in_range(row["closed_at"], start, end, end_inclusive=True):
                continue
            reason = clip(str(_cell(row, "exit_reason") or "(empty)"), 40) or "(empty)"
            pnl = safe_float(_cell(row, "pnl_usd"))
            if pnl is None:
                pnl = 0.0
            meta = parse_json(str(_cell(row, "meta_json") or ""))
            liq = None
            purpose: str | None = None
            if isinstance(meta, dict):
                liq = safe_float(meta.get("liquidity_usd"))
                if liq is None:
                    extras = meta.get("extras")
                    if isinstance(extras, dict):
                        liq = safe_float(extras.get("liquidity_usd"))
                raw_id = meta.get("decision_log_id")
                did: int | None = None
                if isinstance(raw_id, bool):
                    did = None
                elif isinstance(raw_id, (int, float)):
                    did = int(raw_id)
                elif isinstance(raw_id, str) and raw_id.strip().isdigit():
                    did = int(raw_id.strip())
                if did is not None:
                    purpose = purposes.get(did)
            bucket = liq_bucket_label(liq)
            agg[(reason, bucket)].append(pnl)
            pnls.append(pnl)
            closed += 1
            total_pnl += pnl
            pair = str(_cell(row, "pair_address") or "").strip()
            if pair:
                opened = str(_cell(row, "opened_at") or "")
                closed_at = str(_cell(row, "closed_at") or "")
                sort_key = opened or closed_at
                hit = pnl > 0
                token = str(_cell(row, "token_address") or "")
                prev = first_by_pair.get(pair)
                if prev is None or (sort_key, closed_at) < (prev[0], prev[1]):
                    first_by_pair[pair] = (
                        sort_key,
                        closed_at,
                        hit,
                        purpose,
                        token,
                        _optional_ts(opened),
                    )
        ordered = tuple(
            PaperClosedBucket(
                exit_reason=key[0],
                liq_bucket=key[1],
                count=len(values),
                pnl_usd=round(sum(values), 4),
            )
            for key, values in sorted(
                agg.items(),
                key=lambda item: (-abs(sum(item[1])), -len(item[1]), item[0][0], item[0][1]),
            )
        )
        win_count = sum(1 for value in pnls if value > 0)
        loss_count = sum(1 for value in pnls if value <= 0)
        win_rate = (win_count / closed) if closed else None
        worst = min(pnls) if pnls else None
        rest = (total_pnl - worst) if worst is not None else None
        # Case lesson: one loss wipes the rest (rest>0 and total<0).
        single_wipe = bool(
            worst is not None and rest is not None and rest > 0 and total_pnl < 0
        )
        first_n = len(first_by_pair)
        first_wins = sum(1 for item in first_by_pair.values() if item[2])
        first_rate = (first_wins / first_n) if first_n else None
        live_items = [item for item in first_by_pair.values() if item[3] == "live_candidate"]
        live_n = len(live_items)
        live_wins = sum(1 for item in live_items if item[2])
        live_rate = (live_wins / live_n) if live_n else None
        first_events = [(item[5], item[4]) for item in first_by_pair.values()]
        live_events = [(item[5], item[4]) for item in live_items]
        first_dedup = dedup_token_count(first_events) if first_n else 0
        live_dedup = dedup_token_count(live_events) if live_n else 0
        stop_loss_pnl = sum(sum(vals) for (er, _lb), vals in agg.items() if er == "stop_loss")
        stop_loss_pnl = float(stop_loss_pnl) if any(er == "stop_loss" for er, _ in agg) else None
        stop_loss_share = None
        if stop_loss_pnl is not None and total_pnl < 0 and abs(total_pnl) > 1e-9:
            stop_loss_share = min(1.0, abs(stop_loss_pnl) / abs(total_pnl))
        return _ok(
            PaperClosedReport(
                window_days,
                closed,
                round(total_pnl, 4),
                ordered,
                win_count=win_count,
                loss_count=loss_count,
                win_rate=round(win_rate, 4) if win_rate is not None else None,
                worst_pnl_usd=round(worst, 4) if worst is not None else None,
                rest_pnl_usd=round(rest, 4) if rest is not None else None,
                single_wipe=single_wipe,
                first_hit_n=first_n,
                first_hit_wins=first_wins,
                first_hit_rate=round(first_rate, 4) if first_rate is not None else None,
                first_hit_live_n=live_n,
                first_hit_live_wins=live_wins,
                first_hit_live_rate=round(live_rate, 4) if live_rate is not None else None,
                first_hit_dedup=first_dedup,
                first_hit_live_dedup=live_dedup,
                stop_loss_pnl_usd=round(stop_loss_pnl, 4) if stop_loss_pnl is not None else None,
                stop_loss_share=round(stop_loss_share, 4) if stop_loss_share is not None else None,
            ),
            started,
        )

    def _decision_purposes(self, decision_ids: list[int]) -> dict[int, str | None]:
        """Map decision_log.id -> purpose when the column exists; empty on miss."""
        unique = sorted({int(item) for item in decision_ids if isinstance(item, int)})
        if not unique:
            return {}
        path = self.paths.main
        if not path.is_file():
            return {}
        try:
            with ro_connect(path) as conn:
                if _missing_table(conn, "decision_log", path.name):
                    return {}
                cols = _columns(conn, "decision_log")
                if "id" not in cols or "purpose" not in cols:
                    return {}
                out: dict[int, str | None] = {}
                chunk = 400
                for index in range(0, len(unique), chunk):
                    batch = unique[index : index + chunk]
                    placeholders = ", ".join("?" for _ in batch)
                    for row in _exec(
                        conn,
                        f"SELECT id, purpose FROM decision_log WHERE id IN ({placeholders})",
                        tuple(batch),
                    ).fetchall():
                        raw = _cell(row, "purpose")
                        if raw is None:
                            out[int(row["id"])] = None
                        else:
                            text_purpose = str(raw).strip()
                            out[int(row["id"])] = text_purpose or None
                return out
        except sqlite3.Error:
            log.warning("decision purpose lookup failed")
            return {}

    def _decision_ids_between(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[list[tuple[int, str, str]], str]:
        path = self.paths.main
        if not path.is_file():
            return [], f"数据库文件不存在：{path.name}"
        lower = (start - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "decision_log", path.name)
                if missing:
                    return [], missing
                cols = _columns(conn, "decision_log")
                absent = _require(cols, ("id", "ts"))
                if absent:
                    return [], f"{absent}（{path.name}）"
                selected = ["id", "ts"]
                if "token_address" in cols:
                    selected.append("token_address")
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(selected)} FROM decision_log WHERE ts >= ?",
                    (lower,),
                ).fetchall()
        except sqlite3.Error as exc:
            return [], f"查询失败（{path.name}）：{_safe_exc(exc)}"
        kept = [
            (
                int(row["id"]),
                str(row["ts"] or ""),
                str(_cell(row, "token_address") or ""),
            )
            for row in rows
            if _in_range(row["ts"], start, end, end_inclusive=True)
        ]
        return kept, ""

    def _latest_execs(
        self,
        decision_ids: list[int],
    ) -> tuple[dict[int, sqlite3.Row], set[int], str]:
        path = self.paths.execution
        if not decision_ids:
            return {}, set(), ""
        if not path.is_file():
            return {}, set(), f"数据库文件不存在：{path.name}"
        latest: dict[int, sqlite3.Row] = {}
        counts: dict[int, int] = defaultdict(int)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "execution_log", path.name)
                if missing:
                    return {}, set(), missing
                cols = _columns(conn, "execution_log")
                absent = _require(cols, ("id", "ts", "decision_log_id"))
                if absent:
                    return {}, set(), f"{absent}（{path.name}）"
                wanted = [
                    name
                    for name in (
                        "id",
                        "ts",
                        "decision_log_id",
                        "validation_ok",
                        "validation_errors",
                        "executed",
                        "executor",
                        "pair_address",
                        "receipt_json",
                    )
                    if name in cols
                ]
                for part in _chunks(decision_ids, 400):
                    marks = ",".join("?" * len(part))
                    found = _exec(
                        conn,
                        f"SELECT {', '.join(wanted)} FROM execution_log "
                        f"WHERE decision_log_id IN ({marks})",
                        tuple(part),
                    ).fetchall()
                    for row in found:
                        decision_id = _as_int(row["decision_log_id"])
                        if decision_id is None:
                            continue
                        counts[decision_id] += 1
                        prev = latest.get(decision_id)
                        if prev is None or _exec_sort_key(row) >= _exec_sort_key(prev):
                            latest[decision_id] = row
        except sqlite3.Error as exc:
            return {}, set(), f"查询失败（{path.name}）：{_safe_exc(exc)}"
        dups = {decision_id for decision_id, count in counts.items() if count > 1}
        return latest, dups, ""


    def market_base_stats(self) -> DataResult:
        """Hot tier, last 24h: n / median 1h / median 4h / winsor mean 1h."""
        started = time.perf_counter()
        try:
            return _finish(self._market_base_stats(started), "market_base_stats")
        except Exception as exc:
            log.warning("read failed market_base_stats: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "market_base_stats")

    def strategy_buckets(self) -> DataResult:
        """decision_log grouped by strategy_id over 168h, with execution pass rate."""
        started = time.perf_counter()
        try:
            return _finish(self._strategy_buckets(started), "strategy_buckets")
        except Exception as exc:
            log.warning("read failed strategy_buckets: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "strategy_buckets")

    def market_rows(self, chain: str | None = None, limit: int = 100) -> DataResult:
        started = time.perf_counter()
        try:
            return _finish(self._market_rows(started, chain, limit), "market_rows")
        except Exception as exc:
            log.warning("read failed market_rows: %s", type(exc).__name__)
            return _finish(_unavail(f"查询失败：{_safe_exc(exc)}", started), "market_rows")

    def _market_rows(self, started: float, chain: str | None, limit: int) -> DataResult:
        path = self.paths.main
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        limit = _clamp(limit, 1, 300)
        try:
            with ro_connect(path) as conn:
                miss = _missing_table(conn, "token_indicators", path.name)
                if miss:
                    return _unavail(miss, started)
                ti_cols = _columns(conn, "token_indicators")
                clock = _indicator_clock_expr(ti_cols)
                if not clock:
                    return _unavail("token_indicators 缺少 updated_at/recorded_at", started)
                where = ""
                params: list[Any] = []
                if chain and chain.lower() not in ("all", "*", "any"):
                    where = "WHERE chain_id = ?"
                    params.append(chain)
                order = f"ORDER BY CASE WHEN tier = 'hot' THEN 0 ELSE 1 END, {clock} DESC"
                sql = f"SELECT * FROM token_indicators {where} {order} LIMIT ?"
                params.append(limit)
                rows = _exec(conn, sql, tuple(params)).fetchall()
                pair_keys: list[tuple[str, str]] = []
                token_keys: list[tuple[str, str]] = []
                for row in rows:
                    ch = str(_cell(row, "chain_id") or "")
                    tok = str(_cell(row, "token_address") or "")
                    pa = str(_cell(row, "primary_pair_address") or "")
                    if pa:
                        pair_keys.append((ch, pa))
                    token_keys.append((ch, tok))
                quotes = _load_quotes(conn, pair_keys) if _table_exists(conn, "pair_history") else {}
                cov = _load_coverage(conn, token_keys) if _table_exists(conn, "jev_signals") else {}
                symbols = _load_symbols(conn, token_keys) if _table_exists(conn, "tokens") else {}
                now = datetime.now(timezone.utc)
                out: list[MarketRow] = []
                for row in rows:
                    ch = str(_cell(row, "chain_id") or "")
                    tok = str(_cell(row, "token_address") or "")
                    pa = str(_cell(row, "primary_pair_address") or "")
                    price, liq = quotes.get((ch, pa), (None, None))
                    strength = safe_float(_cell(row, "jev_strength"))
                    if strength is None:
                        strength = safe_float(_cell(row, "jev_score"))
                    coverage = cov.get((ch, tok))
                    if coverage is None:
                        conf = safe_float(_cell(row, "jev_confidence"))
                        if conf is not None:
                            coverage = conf * 100.0 if conf <= 1.0 else conf
                    vol = safe_float(_cell(row, "volatility"))
                    if vol is None:
                        vol = safe_float(_cell(row, "volatility_24h"))
                    sent_parts: list[str] = []
                    for key in (
                        "sentiment_score",
                        "sentiment_heat_score",
                        "sentiment_polarity",
                        "sentiment_mentions_1h",
                    ):
                        val = safe_float(_cell(row, key))
                        if val is not None:
                            sent_parts.append(f"{key}={val:.4g}")
                    tech_at, fresh_at, fresh_state = _indicator_freshness(row, ti_cols, now)
                    out.append(
                        MarketRow(
                            chain_id=ch,
                            token_address=tok,
                            pair_address=pa,
                            price_usd=price,
                            liquidity_usd=liq,
                            change_5m=safe_float(_cell(row, "change_5m")),
                            change_1h=safe_float(_cell(row, "change_1h")),
                            change_4h=safe_float(_cell(row, "change_4h")),
                            jev_strength=strength,
                            coverage=coverage,
                            tier=str(_cell(row, "tier") or ""),
                            volatility=vol,
                            sentiment="; ".join(sent_parts),
                            symbol=symbols.get((ch, tok), "") or str(_cell(row, "symbol") or ""),
                            change_15m=safe_float(_cell(row, "change_15m")),
                            change_24h=safe_float(_cell(row, "change_24h")),
                            tech_at=tech_at,
                            fresh_at=fresh_at,
                            fresh_state=fresh_state,
                        )
                    )
                return _ok(out, started)
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)

    def _market_base_stats(self, started: float) -> DataResult:
        path = self.paths.main
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=24)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "indicator_history", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "indicator_history")
                absent = _require(cols, ("change_1h", "change_4h", "tier"))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                if "recorded_at" in cols:
                    ts_col = _ident("recorded_at")
                elif "ts" in cols:
                    ts_col = _ident("ts")
                else:
                    return _unavail(f"缺少字段：recorded_at（{path.name}）", started)
                lower = (start - timedelta(days=1)).strftime("%Y-%m-%d")
                rows = _exec(
                    conn,
                    f"SELECT {ts_col} AS ts, change_1h, change_4h FROM indicator_history "
                    f"WHERE tier = 'hot' AND {ts_col} >= ?",
                    (lower,),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        change_1h: list[float] = []
        change_4h: list[float] = []
        kept = 0
        for row in rows:
            if not _in_range(row["ts"], start, end, end_inclusive=True):
                continue
            kept += 1
            value_1h = safe_float(row["change_1h"])
            value_4h = safe_float(row["change_4h"])
            if value_1h is not None:
                change_1h.append(value_1h)
            if value_4h is not None:
                change_4h.append(value_4h)
        if kept == 0:
            return _unavail("indicator_history tier=hot 近 24h 无行", started)
        winsor = [value for value in change_1h if abs(value) <= 500]
        return _ok(
            MarketBaseStats(
                n_1h=len(change_1h),
                med_1h=_fmt_signed_pct(_median(change_1h)),
                med_4h=_fmt_signed_pct(_median(change_4h)),
                mean_1h_w=_fmt_signed_pct(_mean(winsor)),
            ),
            started,
        )

    def _strategy_buckets(self, started: float) -> DataResult:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        path = self.paths.main
        if not path.is_file():
            return _unavail(f"数据库文件不存在：{path.name}", started)
        try:
            with ro_connect(path) as conn:
                missing = _missing_table(conn, "decision_log", path.name)
                if missing:
                    return _unavail(missing, started)
                cols = _columns(conn, "decision_log")
                absent = _require(cols, ("id", "ts", "strategy_id"))
                if absent:
                    return _unavail(f"{absent}（{path.name}）", started)
                selected = ["id", "ts", "strategy_id"]
                if "token_address" in cols:
                    selected.append("token_address")
                lower = (start - timedelta(days=1)).strftime("%Y-%m-%d")
                rows = _exec(
                    conn,
                    f"SELECT {', '.join(selected)} FROM decision_log WHERE ts >= ?",
                    (lower,),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        kept = [
            (
                int(row["id"]),
                str(row["strategy_id"] or "").strip() or "—",
                _optional_ts(row["ts"]),
                str(_cell(row, "token_address") or ""),
            )
            for row in rows
            if _in_range(row["ts"], start, end, end_inclusive=True)
        ]
        if not kept:
            return _ok(StrategyBuckets(rows=()), started)
        latest, _dups, note = self._latest_execs([item[0] for item in kept])
        counts: dict[str, list[Any]] = defaultdict(lambda: [0, 0, 0, []])
        for decision_id, strategy_id, stamp, token in kept:
            cell = counts[strategy_id]
            cell[0] += 1
            cell[3].append((stamp, token))
            if note:
                continue
            exec_row = latest.get(decision_id)
            if exec_row is None:
                continue
            cell[1] += 1
            if _as_int(_cell(exec_row, "validation_ok")) == 1:
                cell[2] += 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1][0], item[0]))[:12]
        payload = tuple(
            StrategyBucketRow(
                strategy_id=strategy_id,
                n=n,
                ok_rate=(f"{(n_ok / n_with) * 100:.1f}%" if n_with else "—"),
                n_dedup=dedup_token_count(events),
            )
            for strategy_id, (n, n_with, n_ok, events) in ordered
        )
        return _ok(StrategyBuckets(rows=payload), started)

    def _market_lookups(
        self,
        pair_keys: list[tuple[str, str]],
        token_keys: list[tuple[str, str]],
    ) -> tuple[dict[tuple[str, str], tuple[float | None, float | None]], dict[tuple[str, str], str], str]:
        path = self.paths.main
        if not path.is_file():
            return {}, {}, f"数据库文件不存在：{path.name}（现价无法计算）"
        try:
            with ro_connect(path) as conn:
                if not _table_exists(conn, "pair_history"):
                    quotes: dict[tuple[str, str], tuple[float | None, float | None]] = {}
                    note = "缺少数据表：pair_history（现价无法计算）"
                else:
                    quotes = _load_quotes(conn, pair_keys)
                    note = ""
                symbols = _load_symbols(conn, token_keys)
        except sqlite3.Error as exc:
            return {}, {}, f"查询失败：{_safe_exc(exc)}"
        return quotes, symbols, note


def _missing_table(conn: sqlite3.Connection, table: str, db_name: str) -> str:
    if _table_exists(conn, table):
        return ""
    return f"缺少数据表：{table}（{db_name}）"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.fmean(values))


def _fmt_signed_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _clamp(value: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _text_id(value: Any) -> str:
    if value is None:
        return "—"
    return str(value)


def _positive(value: Any) -> float | None:
    number = safe_float(value)
    if number is None or number <= 0:
        return None
    return number


def _exec_sort_key(row: sqlite3.Row) -> tuple[str, int]:
    return (str(_cell(row, "ts") or ""), _as_int(_cell(row, "id")) or 0)


def _is_open(row: sqlite3.Row, cols: set[str]) -> bool:
    if "closed_at" in cols and _cell(row, "closed_at") not in (None, ""):
        return False
    if "status" not in cols:
        return True
    status = _cell(row, "status")
    if status is None or str(status).strip() == "":
        return True
    return str(status).strip().lower() not in _CLOSED_STATUS


def _read_cash(conn: sqlite3.Connection, db_name: str) -> tuple[float | None, str]:
    if not _table_exists(conn, "account"):
        return None, f"缺少数据表：account（{db_name}）"
    cols = _columns(conn, "account")
    if "cash_usd" not in cols:
        return None, "缺少字段：cash_usd"
    order = "id ASC" if "id" in cols else "rowid ASC"
    row = _exec(conn, f"SELECT cash_usd FROM account ORDER BY {order} LIMIT 1").fetchone()
    if row is None:
        return None, "账户无现金记录"
    return safe_float(row["cash_usd"]), ""


def _unrealized(
    entry: float | None,
    mark: float | None,
    qty: float | None,
    size_usd: float | None,
) -> tuple[float | None, float | None]:
    if entry is None or mark is None or entry <= 0:
        return None, None
    pct = (mark - entry) / entry * 100.0
    if qty is not None:
        usd = (mark - entry) * qty
    elif size_usd is not None:
        usd = size_usd * (mark / entry - 1.0)
    else:
        usd = None
    return pct, usd


def _mtm(
    entry: float | None,
    mark: float | None,
    qty: float | None,
    size_usd: float | None,
) -> float | None:
    if mark is not None and qty is not None:
        return mark * qty
    if mark is not None and entry is not None and entry > 0 and size_usd is not None:
        return size_usd * (mark / entry)
    if size_usd is not None:
        return size_usd
    if entry is not None and qty is not None:
        return entry * qty
    return None


def _hold_seconds(opened: Any) -> float | None:
    if not opened:
        return None
    try:
        stamp = parse_ts(opened)
    except (TypeError, ValueError):
        return None
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())



def _load_coverage(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], float | None]:
    if not _table_exists(conn, "jev_signals"):
        return {}
    cols = _columns(conn, "jev_signals")
    if "token_address" not in cols or "coverage_pct" not in cols:
        return {}
    has_chain = "chain_id" in cols
    time_bits = [c for c in ("fetched_at", "analyzed_at") if c in cols]
    if len(time_bits) >= 2:
        order = f"COALESCE({', '.join(time_bits)}) DESC"
    elif len(time_bits) == 1:
        order = f"{time_bits[0]} DESC"
    else:
        order = "rowid DESC"
    out: dict[tuple[str, str], float | None] = {}
    seen: set[tuple[str, str]] = set()
    for chain, token in keys:
        if not token or (chain, token) in seen:
            continue
        seen.add((chain, token))
        if has_chain and chain:
            row = _exec(
                conn,
                f"SELECT coverage_pct FROM jev_signals WHERE chain_id=? AND token_address=? ORDER BY {order} LIMIT 1",
                (chain, token),
            ).fetchone()
        else:
            row = _exec(
                conn,
                f"SELECT coverage_pct FROM jev_signals WHERE token_address=? ORDER BY {order} LIMIT 1",
                (token,),
            ).fetchone()
        out[(chain, token)] = safe_float(row["coverage_pct"]) if row else None
    return out


def _load_quotes(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], tuple[float | None, float | None]]:
    cols = _columns(conn, "pair_history")
    result: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    if "pair_address" not in cols or "price_usd" not in cols:
        return result
    has_chain = "chain_id" in cols
    has_liq = "liquidity_usd" in cols
    has_time = "recorded_at" in cols
    has_id = "id" in cols
    liq_sql = "liquidity_usd" if has_liq else "NULL AS liquidity_usd"
    if has_time and has_id:
        order = "recorded_at DESC, id DESC"
    elif has_time:
        order = "recorded_at DESC"
    else:
        order = "rowid DESC"
    seen: set[tuple[str, str]] = set()
    for chain, pair in keys:
        if not pair or (chain, pair) in seen:
            continue
        seen.add((chain, pair))
        if has_chain and chain:
            sql = (
                f"SELECT price_usd, {liq_sql} FROM pair_history "
                f"WHERE chain_id = ? AND pair_address = ? ORDER BY {order} LIMIT 1"
            )
            params: tuple[Any, ...] = (chain, pair)
        else:
            sql = (
                f"SELECT price_usd, {liq_sql} FROM pair_history "
                f"WHERE pair_address = ? ORDER BY {order} LIMIT 1"
            )
            params = (pair,)
        row = _exec(conn, sql, params).fetchone()
        if row is None:
            continue
        price = safe_float(row["price_usd"])
        if price is not None and price <= 0:
            price = None
        result[(chain, pair)] = (price, safe_float(row["liquidity_usd"]))
    return result


def _load_symbols(conn: sqlite3.Connection, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    if not _table_exists(conn, "tokens"):
        return {}
    cols = _columns(conn, "tokens")
    if "raw_json" not in cols or "token_address" not in cols:
        return {}
    found: dict[tuple[str, str], str] = {}
    has_chain = "chain_id" in cols
    seen: set[tuple[str, str]] = set()
    for chain, token in keys:
        if not token or (chain, token) in seen:
            continue
        seen.add((chain, token))
        if has_chain and chain:
            row = _exec(
                conn,
                "SELECT raw_json FROM tokens WHERE chain_id = ? AND token_address = ? LIMIT 1",
                (chain, token),
            ).fetchone()
        else:
            row = _exec(
                conn,
                "SELECT raw_json FROM tokens WHERE token_address = ? LIMIT 1",
                (token,),
            ).fetchone()
        if row is None:
            continue
        symbol = extract_symbol(parse_json(str(row["raw_json"] or "")))
        if symbol:
            found[(chain, token)] = symbol
    return found


def _coverage_map(
    conn: sqlite3.Connection,
    decisions: list[sqlite3.Row],
) -> dict[tuple[str, str], float]:
    """Prefer jev_signals.coverage_pct, then token_indicators.jev_confidence * 100."""
    found: dict[tuple[str, str], float] = {}
    keys = [
        (str(_cell(row, "chain_id") or ""), str(_cell(row, "token_address") or ""))
        for row in decisions
        if _cell(row, "token_address")
    ]
    if _table_exists(conn, "jev_signals"):
        cols = _columns(conn, "jev_signals")
        if {"chain_id", "token_address", "coverage_pct"} <= cols:
            for chain, token in keys:
                row = _exec(
                    conn,
                    "SELECT coverage_pct FROM jev_signals WHERE chain_id = ? AND token_address = ? LIMIT 1",
                    (chain, token),
                ).fetchone()
                if row is None:
                    continue
                value = safe_float(row["coverage_pct"])
                if value is not None:
                    found[(chain, token)] = value
    if _table_exists(conn, "token_indicators"):
        cols = _columns(conn, "token_indicators")
        if {"chain_id", "token_address", "jev_confidence"} <= cols:
            for chain, token in keys:
                if (chain, token) in found:
                    continue
                row = _exec(
                    conn,
                    "SELECT jev_confidence FROM token_indicators "
                    "WHERE chain_id = ? AND token_address = ? LIMIT 1",
                    (chain, token),
                ).fetchone()
                if row is None:
                    continue
                value = safe_float(row["jev_confidence"])
                if value is not None:
                    found[(chain, token)] = value * 100.0
    return found


def _strength_fallback(
    conn: sqlite3.Connection,
    decisions: list[sqlite3.Row],
) -> dict[tuple[str, str], float]:
    if not _table_exists(conn, "indicator_history"):
        return {}
    cols = _columns(conn, "indicator_history")
    if "jev_strength" not in cols or "token_address" not in cols:
        return {}
    found: dict[tuple[str, str], float] = {}
    has_chain = "chain_id" in cols
    order = "recorded_at DESC" if "recorded_at" in cols else "rowid DESC"
    seen: set[tuple[str, str]] = set()
    for row in decisions:
        chain = str(_cell(row, "chain_id") or "")
        token = str(_cell(row, "token_address") or "")
        if not token or (chain, token) in seen:
            continue
        seen.add((chain, token))
        if has_chain and chain:
            found_row = _exec(
                conn,
                "SELECT jev_strength FROM indicator_history "
                f"WHERE chain_id = ? AND token_address = ? AND jev_strength IS NOT NULL "
                f"ORDER BY {order} LIMIT 1",
                (chain, token),
            ).fetchone()
        else:
            found_row = _exec(
                conn,
                "SELECT jev_strength FROM indicator_history "
                f"WHERE token_address = ? AND jev_strength IS NOT NULL ORDER BY {order} LIMIT 1",
                (token,),
            ).fetchone()
        if found_row is None:
            continue
        value = safe_float(found_row["jev_strength"])
        if value is not None:
            found[(chain, token)] = value
    return found


def _text_stamp(row: sqlite3.Row, name: str) -> str:
    if name not in row.keys():
        return ""
    return str(row[name] or "").strip()


def _null_if_blank(column: str) -> str:
    _ident(column)
    return f"NULLIF(TRIM({column}), '')"


def _indicator_clock_expr(cols: set[str]) -> str | None:
    """SQL clock for token_indicators sort and staleness.

    Indicator #32 keeps technical freshness on tech_updated_at. When PRAGMA
    shows that column, it wins. A NULL or blank value falls back to updated_at,
    then recorded_at — the same degrade as decision #31's reader when the
    technical column is absent. No stamp is synthesized.
    """
    fallbacks = [name for name in ("updated_at", "recorded_at") if name in cols]
    parts: list[str] = []
    if "tech_updated_at" in cols:
        parts.append(_null_if_blank("tech_updated_at"))
    parts.extend(_null_if_blank(name) for name in fallbacks)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "COALESCE(" + ", ".join(parts) + ")"


def _indicator_freshness(
    row: sqlite3.Row,
    cols: set[str],
    now: datetime,
) -> tuple[str, str, Liveness]:
    """Return (tech_at, fresh_at, state) for one token_indicators row.

    tech_at stays empty when the column is missing or the value is NULL/blank.
    fresh_at is that tech stamp, otherwise updated_at / recorded_at. Both empty
    means unknown — the caller must not invent a time.
    """
    tech = _text_stamp(row, "tech_updated_at") if "tech_updated_at" in cols else ""
    fallback = ""
    if not tech:
        for name in ("updated_at", "recorded_at"):
            if name not in cols:
                continue
            fallback = _text_stamp(row, name)
            if fallback:
                break
    chosen = tech or fallback
    if not chosen:
        return "", "", "unknown"
    return tech, chosen, _liveness(_age_seconds(chosen, now))


def _probe_indicator(conn: sqlite3.Connection, now: datetime) -> ProcessProbe:
    """Indicator heartbeat. Technical life is tech_updated_at when the column exists.

    Funding, sentiment, and composite bump token_indicators.updated_at and can
    also append indicator_history.recorded_at. Once tech_updated_at is present,
    MAX of that column is the pulse, including when it is stale or entirely
    NULL. Those shared clocks must not report the technical pass as alive.
    Without the column, fall back to indicator_history.recorded_at, then
    token_indicators.updated_at.
    """
    if _table_exists(conn, "token_indicators"):
        cols = _columns(conn, "token_indicators")
        if "tech_updated_at" in cols:
            found = _probe_max(conn, "token_indicators", "tech_updated_at", "指标", now)
            return ProcessProbe(found.name, found.state, found.age_seconds, "tech_updated_at")
    history = _probe_max(conn, "indicator_history", "recorded_at", "指标", now)
    if history.state != "unknown":
        return history
    return _probe_max(conn, "token_indicators", "updated_at", "指标", now)


def _probe_checkpoint(conn: sqlite3.Connection, now: datetime) -> ProcessProbe:
    if not _table_exists(conn, "run_checkpoint"):
        return ProcessProbe("主爬虫", "unknown", None, "")
    cols = _columns(conn, "run_checkpoint")
    time_col = "updated_at" if "updated_at" in cols else ("started_at" if "started_at" in cols else "")
    if not time_col:
        return ProcessProbe("主爬虫", "unknown", None, "")
    extra = [name for name in ("status", "step") if name in cols]
    selected = ", ".join([time_col, *extra])
    row = _exec(
        conn,
        f"SELECT {selected} FROM run_checkpoint ORDER BY {time_col} DESC LIMIT 1",
    ).fetchone()
    if row is None:
        return ProcessProbe("主爬虫", "unknown", None, "")
    age = _age_seconds(row[time_col], now)
    detail_parts = [clip(_cell(row, name), 24) for name in extra if _cell(row, name)]
    return ProcessProbe("主爬虫", _liveness(age), age, " · ".join(detail_parts))


def _probe_max(
    conn: sqlite3.Connection,
    table: str,
    time_col: str,
    name: str,
    now: datetime,
    limit: float | None = None,
) -> ProcessProbe:
    if table not in _TABLES or not _table_exists(conn, table):
        return ProcessProbe(name, "unknown", None, "")
    cols = _columns(conn, table)
    if time_col not in cols:
        return ProcessProbe(name, "unknown", None, "")
    _ident(time_col)
    row = _exec(conn, f"SELECT MAX({time_col}) AS ts FROM {table}").fetchone()
    if row is None or not row["ts"]:
        return ProcessProbe(name, "unknown", None, "")
    age = _age_seconds(row["ts"], now)
    return ProcessProbe(name, _liveness(age, limit), age, "")
