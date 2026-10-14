"""Sole SQLite access for the dashboard.

Every connection uses ``file:...?mode=ro`` plus ``PRAGMA query_only=ON``.
Write statements are rejected before they reach SQLite. Missing files and
tables return :class:`DataResult` instead of raising.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import statistics
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

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
                DecisionWindowStats(0, 0, 0, 0, None, None),
                started,
            )
        latest, _dups, note = self._latest_execs([item[0] for item in decisions])
        if note:
            return _unavail(note, started)
        n_with = n_ok = n_executed = 0
        for decision_id, _ts in decisions:
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
        # pair -> (opened_at sort key, closed_at, hit, purpose)
        first_by_pair: dict[str, tuple[str, str, bool, str | None]] = {}
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
                prev = first_by_pair.get(pair)
                if prev is None or (sort_key, closed_at) < (prev[0], prev[1]):
                    first_by_pair[pair] = (sort_key, closed_at, hit, purpose)
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
    ) -> tuple[list[tuple[int, str]], str]:
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
                rows = _exec(
                    conn,
                    "SELECT id, ts FROM decision_log WHERE ts >= ?",
                    (lower,),
                ).fetchall()
        except sqlite3.Error as exc:
            return [], f"查询失败（{path.name}）：{_safe_exc(exc)}"
        kept = [
            (int(row["id"]), str(row["ts"] or ""))
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
                lower = (start - timedelta(days=1)).strftime("%Y-%m-%d")
                rows = _exec(
                    conn,
                    "SELECT id, ts, strategy_id FROM decision_log WHERE ts >= ?",
                    (lower,),
                ).fetchall()
        except sqlite3.Error as exc:
            return _unavail(f"查询失败（{path.name}）：{_safe_exc(exc)}", started)
        kept = [
            (int(row["id"]), str(row["strategy_id"] or "").strip() or "—")
            for row in rows
            if _in_range(row["ts"], start, end, end_inclusive=True)
        ]
        if not kept:
            return _ok(StrategyBuckets(rows=()), started)
        latest, _dups, note = self._latest_execs([item[0] for item in kept])
        counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for decision_id, strategy_id in kept:
            cell = counts[strategy_id]
            cell[0] += 1
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
            )
            for strategy_id, (n, n_with, n_ok) in ordered
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
