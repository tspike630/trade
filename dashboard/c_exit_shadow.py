"""Read-only C-exit shadow book (旁簿) for the OVERVIEW card.

Four tags, TP fixed at 10%, SL in {4%, 5%} and hold in {60, 90}:
``c_sl4_h60``, ``c_sl5_h60``, ``c_sl4_h90``, ``c_sl5_h90``.

Live rows come from ``paper_trading_c_exit.db`` (env ``C_EXIT_SHADOW_DB``
or that file under the dashboard db root). A tag with fewer than 30 closed
rows keeps the packaged 2026-10-20 offline path replay. Those offline
numbers are a coarse replay of the main book, not the shadow book's cash.

This module does not write a database, does not edit environment files,
and does not promote a parameter onto the main book.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal, Never

from dashboard.data import ro_connect
from dashboard.settings import db_root

log = logging.getLogger("dashboard.c_exit_shadow")

DB_ENV = "C_EXIT_SHADOW_DB"
DB_NAME = "paper_trading_c_exit.db"
PACKAGED_ASOF = "2026-10-20"
MIN_CLOSED = 30
MAIN_PNL = Decimal("-1295.71")
ROLLBACK_SINGLE = Decimal("-500")
ROLLBACK_CUM = Decimal("-400")

# Visible nail. The shadow book is not a change to the main gate.
C_EXIT_NAIL = "假设旁簿≠已改主闸"
C_EXIT_DISCIPLINE = "只读旁证 · 不改 .env · 不升主"
C_EXIT_GRID = "TP=10% 固定 · SL∈{4%,5%} × hold∈{60,90}"
C_EXIT_THRESHOLD = "每格 ≥30 平仓才比止损占负"
C_EXIT_ROLLBACK = "回滚：单笔≤−$500 / 旁簿累计≤−$400 / 污染主簿 → 停"
C_EXIT_VERDICT = "hold90 Σpnl 更好，止损占负未改善 → 不升主"
C_EXIT_VERDICT_HOLD = "未同时满足止损占负下降且 Σpnl 不差于对照 → 不升主"

_OFFLINE_TAIL = f"回退离线四格 {PACKAGED_ASOF} · 路径重放非旁簿实盘"
_CLOSED = frozenset({"closed", "close"})
_STOP = "stop_loss"
_NULL = frozenset({"", "null", "none", "nan", "na", "n/a"})
_TAG_ORDER = ("c_sl4_h60", "c_sl5_h60", "c_sl4_h90", "c_sl5_h90")
_TAG_SET = frozenset(_TAG_ORDER)
_TAG_COLS = ("grid_tag", "tag", "account_tag", "shadow_tag")
_TOKEN_COLS = ("token_address", "token", "mint", "base_address")
_PNL_COLS = ("pnl_usd", "pnl")
_REASON_COLS = ("exit_reason", "reason", "close_reason")
_PREFERRED = ("positions", "shadow_positions", "c_exit_positions", "paper_positions")
_ACCOUNT_TABLES = ("account", "accounts", "shadow_account")
_SKIP_TABLES = frozenset({"fills", "order_events", "execution_log", "sqlite_sequence"})
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SourceKind = Literal["live", "packaged", "mixed"]
OriginKind = Literal["live", "packaged"]
FallbackKind = Literal["missing", "unreadable", "schema", "empty", "no_closed", "bad"]


@dataclass(frozen=True)
class ExitTag:
    """One grid tag as the card prints it."""

    tag: str
    n: int
    sum_pnl: Decimal
    sl_share: Decimal | None
    sl_peak: Decimal | None
    origin: OriginKind
    live_n: int | None = None


@dataclass(frozen=True)
class CExitReport:
    """Numbers and the note that says where they came from."""

    source: SourceKind
    note: str
    tags: tuple[ExitTag, ExitTag, ExitTag, ExitTag]
    main_pnl: Decimal
    rollback_single: bool
    rollback_cum: bool


@dataclass(frozen=True)
class _LiveTag:
    n: int
    sum_pnl: Decimal
    sl_share: Decimal | None
    sl_peak: Decimal | None


@dataclass(frozen=True)
class _Live:
    tags: tuple[_LiveTag, _LiveTag, _LiveTag, _LiveTag]
    rollback_single: bool
    rollback_cum: bool


@dataclass
class _Bucket:
    n: int = 0
    total: Decimal = Decimal("0")
    neg: Decimal = Decimal("0")
    sl_neg: Decimal = Decimal("0")
    sl_peak: Decimal | None = None


class _BadRow(Exception):
    """A closed row that cannot be summed. The card falls back offline."""


def _never(value: Never) -> Never:
    raise AssertionError(f"unhandled variant: {value!r}")


def fallback_note(kind: FallbackKind) -> str:
    """Visible fallback label. The offline table still fills the card."""
    match kind:
        case "missing":
            return f"旁簿库缺失 · {_OFFLINE_TAIL}"
        case "unreadable":
            return f"旁簿库不可读 · {_OFFLINE_TAIL}"
        case "schema":
            return f"旁簿库缺列 · {_OFFLINE_TAIL}"
        case "empty":
            return f"旁簿库空 · {_OFFLINE_TAIL}"
        case "no_closed":
            return f"旁簿无 closed · {_OFFLINE_TAIL}"
        case "bad":
            return f"旁簿坏数据 · {_OFFLINE_TAIL}"
        case _ as unreachable:
            return _never(unreachable)


def _q_cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_cents_usd(value: Decimal) -> str:
    """Signed cents. Minus is U+2212, matching the Phase1 card."""
    quantized = _q_cents(value)
    text = f"{abs(quantized):.2f}"
    if quantized > 0:
        return f"+${text}"
    if quantized < 0:
        return f"−${text}"
    return "$0.00"


def fmt_share(value: Decimal | None) -> str:
    if value is None:
        return "—"
    quantized = value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{quantized}%"


def fmt_peak(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return fmt_cents_usd(value)


def _pack_tag(
    tag: str,
    n: int,
    pnl: str,
    share: str,
    peak: str,
) -> ExitTag:
    return ExitTag(
        tag=tag,
        n=n,
        sum_pnl=Decimal(pnl),
        sl_share=Decimal(share),
        sl_peak=Decimal(peak),
        origin="packaged",
        live_n=None,
    )


def packaged_tags() -> tuple[ExitTag, ExitTag, ExitTag, ExitTag]:
    """Offline path replay, 2026-10-20. Not the shadow book's cash."""
    return (
        _pack_tag("c_sl4_h60", 141, "-1144.01", "81.6", "-491.67"),
        _pack_tag("c_sl5_h60", 141, "-1155.34", "80.8", "-491.67"),
        _pack_tag("c_sl4_h90", 142, "-538.06", "87.6", "-491.67"),
        _pack_tag("c_sl5_h90", 142, "-546.98", "86.1", "-491.67"),
    )


def packaged_report(note: str) -> CExitReport:
    return CExitReport(
        source="packaged",
        note=note,
        tags=packaged_tags(),
        main_pnl=MAIN_PNL,
        rollback_single=False,
        rollback_cum=False,
    )


def tag_line(row: ExitTag) -> str:
    return (
        f"{row.tag} · n={row.n} · Σpnl {fmt_cents_usd(row.sum_pnl)} · "
        f"止损占负 {fmt_share(row.sl_share)} · 单笔SL峰 {fmt_peak(row.sl_peak)}"
    )


def main_line(report: CExitReport) -> str:
    return f"主簿已平 Σpnl ≈ −$1296（{fmt_cents_usd(report.main_pnl)}）"


def verdict_line(report: CExitReport) -> str:
    """hold90 must beat the c_sl5_h60 control on both Σpnl and 止损占负."""
    by_tag = {row.tag: row for row in report.tags}
    control = by_tag["c_sl5_h60"]
    longer = (by_tag["c_sl4_h90"], by_tag["c_sl5_h90"])
    if control.sl_share is None or any(row.sl_share is None for row in longer):
        return f"止损占负不足 · {C_EXIT_VERDICT_HOLD}"
    better_pnl = all(row.sum_pnl > control.sum_pnl for row in longer)
    not_improved = any(row.sl_share >= control.sl_share for row in longer)
    if better_pnl and not_improved:
        return C_EXIT_VERDICT
    return C_EXIT_VERDICT_HOLD


def rollback_hit_line(report: CExitReport) -> str:
    parts: list[str] = []
    if report.rollback_single:
        parts.append("实盘单笔≤−$500")
    if report.rollback_cum:
        parts.append("实盘累计≤−$400")
    if not parts:
        return ""
    return " · ".join(parts) + " → 停旁簿"


def c_exit_db_path() -> Path | None:
    """Shadow db to read. ``C_EXIT_SHADOW_DB`` wins, even when that path is missing."""
    override = os.environ.get(DB_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    candidate = db_root() / DB_NAME
    if candidate.is_file():
        return candidate
    return None


def _ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(name)
    return name


def _pick(cols: set[str], options: tuple[str, ...]) -> str | None:
    for name in options:
        if name in cols:
            return name
    return None


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    names: list[str] = []
    for row in rows:
        name = str(row["name"])
        if _IDENT.fullmatch(name):
            names.append(name)
    return names


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({_ident(table)})").fetchall()
    return tuple(str(row["name"]) for row in rows)


def _sql_expr(alias: str, column: str | None, label: str) -> str:
    if column is None:
        return f"NULL AS {label}"
    return f"{alias}.{_ident(column)} AS {label}"


def _select_sql(
    table: str,
    pnl: str,
    reason: str | None,
    status: str | None,
    closed_at: str | None,
    tag: str | None,
    meta: str | None,
    token: str | None,
    account: str | None = None,
    account_tag: str | None = None,
) -> str:
    tag_sql = _sql_expr("p", tag, "tag")
    if account is not None and account_tag is not None:
        tag_sql = f"a.{_ident(account_tag)} AS tag"
    columns = ", ".join(
        (
            _sql_expr("p", pnl, "pnl"),
            _sql_expr("p", reason, "reason"),
            _sql_expr("p", status, "status"),
            _sql_expr("p", closed_at, "closed_at"),
            tag_sql,
            _sql_expr("p", meta, "meta"),
            _sql_expr("p", token, "token"),
        )
    )
    if account is not None and account_tag is not None:
        return (
            f"SELECT {columns} FROM {_ident(table)} AS p "
            f"LEFT JOIN {_ident(account)} AS a ON a.id = p.account_id"
        )
    return f"SELECT {columns} FROM {_ident(table)} AS p"


def _read_plan(conn: sqlite3.Connection) -> str | FallbackKind:
    tables = _tables(conn)
    if not tables:
        return "empty"
    present = set(tables)
    ordered = [name for name in _PREFERRED if name in present]
    ordered.extend(
        name
        for name in tables
        if name not in ordered and name not in _ACCOUNT_TABLES and name not in _SKIP_TABLES
    )
    accounts = [name for name in _ACCOUNT_TABLES if name in present]
    for table in ordered:
        cols = set(_columns(conn, table))
        pnl = _pick(cols, _PNL_COLS)
        if pnl is None:
            continue
        status = "status" if "status" in cols else None
        closed_at = "closed_at" if "closed_at" in cols else None
        if status is None and closed_at is None:
            continue
        reason = _pick(cols, _REASON_COLS)
        tag = _pick(cols, _TAG_COLS)
        meta = "meta_json" if "meta_json" in cols else None
        token = _pick(cols, _TOKEN_COLS)
        if tag is not None:
            return _select_sql(table, pnl, reason, status, closed_at, tag, None, token)
        if "account_id" in cols and accounts:
            account = accounts[0]
            acols = set(_columns(conn, account))
            account_tag = _pick(acols, _TAG_COLS)
            if account_tag is not None and "id" in acols:
                return _select_sql(
                    table,
                    pnl,
                    reason,
                    status,
                    closed_at,
                    None,
                    None,
                    token,
                    account=account,
                    account_tag=account_tag,
                )
        if meta is not None:
            return _select_sql(table, pnl, reason, status, closed_at, None, meta, token)
    return "schema"


def _is_closed(status: object, closed_at: object, saw_status: bool) -> bool:
    if saw_status:
        if status is None:
            return False
        return str(status).strip().casefold() in _CLOSED
    if closed_at is None:
        return False
    return str(closed_at).strip() != ""


def _parse_pnl(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return Decimal(str(value))
    text = str(value).strip()
    if text.casefold() in _NULL:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _side_token_key(value: object, ordinal: int) -> str:
    """Distinct-token key. A blank mint stays unique so row-only books keep n."""
    if value is None:
        return f"#{ordinal}"
    text = str(value).strip()
    if not text or text.casefold() in _NULL:
        return f"#{ordinal}"
    return text.casefold()


def _canon_tag(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in _NULL:
        return None
    folded = text.casefold()
    for tag in _TAG_ORDER:
        if tag.casefold() == folded:
            return tag
    return text


def _tag_from_meta(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", "replace").strip()
    else:
        text = str(raw).strip()
    if not text or text.casefold() in _NULL:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _BadRow from exc
    if not isinstance(payload, dict):
        return None
    for key in _TAG_COLS:
        if key not in payload:
            continue
        found = _canon_tag(payload.get(key))
        if found:
            return found
    return None


def _share(bucket: _Bucket) -> Decimal | None:
    if bucket.neg == 0:
        return None
    return (bucket.sl_neg / bucket.neg * Decimal("100")).quantize(
        Decimal("0.1"),
        rounding=ROUND_HALF_UP,
    )


def _fold_rows(rows: list[sqlite3.Row], uses_meta: bool) -> _Live | FallbackKind:
    buckets = {tag: _Bucket() for tag in _TAG_ORDER}
    seen_tokens = {tag: set() for tag in _TAG_ORDER}
    saw_status = any(row["status"] is not None for row in rows)
    closed_rows = 0
    known_closed = 0
    cumulative = Decimal("0")
    single = False
    ordinal = 0
    for row in rows:
        status = row["status"]
        closed_at = row["closed_at"]
        if not _is_closed(status, closed_at, saw_status or status is not None):
            continue
        closed_rows += 1
        pnl = _parse_pnl(row["pnl"])
        if pnl is None:
            return "bad"
        cents = _q_cents(pnl)
        cumulative += cents
        if cents <= ROLLBACK_SINGLE:
            single = True
        tag = _canon_tag(row["tag"])
        if tag is None and uses_meta:
            tag = _tag_from_meta(row["meta"])
        if tag not in _TAG_SET:
            continue
        known_closed += 1
        ordinal += 1
        bucket = buckets[str(tag)]
        token_key = _side_token_key(row["token"], ordinal)
        if token_key not in seen_tokens[str(tag)]:
            seen_tokens[str(tag)].add(token_key)
            bucket.n += 1
        bucket.total += cents
        if cents < 0:
            bucket.neg += cents
        reason = "" if row["reason"] is None else str(row["reason"]).strip().casefold()
        if reason == _STOP:
            if cents < 0:
                bucket.sl_neg += cents
            if bucket.sl_peak is None or cents < bucket.sl_peak:
                bucket.sl_peak = cents
    if closed_rows == 0:
        return "no_closed"
    if known_closed == 0:
        return "schema"
    live_tags = tuple(
        _LiveTag(
            n=buckets[tag].n,
            sum_pnl=_q_cents(buckets[tag].total),
            sl_share=_share(buckets[tag]),
            sl_peak=None if buckets[tag].sl_peak is None else _q_cents(buckets[tag].sl_peak),
        )
        for tag in _TAG_ORDER
    )
    return _Live(
        tags=live_tags,
        rollback_single=single,
        rollback_cum=cumulative <= ROLLBACK_CUM,
    )


def _uses_meta(sql: str) -> bool:
    return " AS meta" in sql and "NULL AS meta" not in sql


def _summarize_conn(conn: sqlite3.Connection) -> _Live | FallbackKind:
    plan = _read_plan(conn)
    if isinstance(plan, str) and plan in {"empty", "schema"}:
        return plan
    if not isinstance(plan, str):
        return "schema"
    try:
        rows = list(conn.execute(plan))
    except sqlite3.Error:
        log.warning("c exit shadow select failed")
        return "unreadable"
    try:
        return _fold_rows(rows, _uses_meta(plan))
    except _BadRow:
        return "bad"


def _read_live(path: Path) -> _Live | FallbackKind:
    try:
        with ro_connect(path) as conn:
            return _summarize_conn(conn)
    except FileNotFoundError:
        return "missing"
    except (OSError, sqlite3.Error, UnicodeError, ValueError):
        log.warning("c exit shadow db unreadable")
        return "unreadable"


def _compose(live: _Live) -> CExitReport:
    packed = {row.tag: row for row in packaged_tags()}
    shown: list[ExitTag] = []
    origins: list[OriginKind] = []
    for tag, row in zip(_TAG_ORDER, live.tags, strict=True):
        if row.n >= MIN_CLOSED:
            shown.append(
                ExitTag(
                    tag=tag,
                    n=row.n,
                    sum_pnl=row.sum_pnl,
                    sl_share=row.sl_share,
                    sl_peak=row.sl_peak,
                    origin="live",
                    live_n=row.n,
                )
            )
            origins.append("live")
            continue
        shown.append(replace(packed[tag], live_n=row.n))
        origins.append("packaged")
    counts = " · ".join(f"{tag}={row.n}" for tag, row in zip(_TAG_ORDER, live.tags, strict=True))
    if all(origin == "live" for origin in origins):
        source: SourceKind = "live"
        note = f"旁簿实盘 · 四格均 ≥{MIN_CLOSED} closed"
    elif all(origin == "packaged" for origin in origins):
        source = "packaged"
        note = f"实盘 closed {counts} · 未满 {MIN_CLOSED}/格 · {_OFFLINE_TAIL}"
    else:
        source = "mixed"
        note = f"实盘 closed {counts} · 未满 {MIN_CLOSED} 的格回退离线 {PACKAGED_ASOF}"
    return CExitReport(
        source=source,
        note=note,
        tags=(shown[0], shown[1], shown[2], shown[3]),
        main_pnl=MAIN_PNL,
        rollback_single=live.rollback_single,
        rollback_cum=live.rollback_cum,
    )


def load_c_exit_shadow(path: Path | None = None) -> CExitReport:
    """Offline replay, replaced per tag once that tag has 30 closed live rows."""
    target = path if path is not None else c_exit_db_path()
    if target is None or not target.is_file():
        return packaged_report(fallback_note("missing"))
    live = _read_live(target)
    if isinstance(live, str):
        return packaged_report(fallback_note(live))
    return _compose(live)


# #137 (c6016d7) applied sell slippage on new C-exit / shadow010 fills only.
# Execution posted a read-only recalc; the history rows in the side-book DBs
# were not rewritten. These constants are that snapshot until an export lands.
SELL_SLIP_CITE = "#137 c6016d7"
SELL_SLIP_CSV_ENV = "C_EXIT_SELL_SLIP_CSV"
SELL_SLIP_CSV_GLOB = "c_exit_sell_slip_recalc*.csv"
SHADOW010_DB_ENV = "C_EXIT_SHADOW010_DB"
SHADOW010_DB_NAME = "paper_trading_c_exit_shadow010.db"
_SELL_SLIP_BOOKS = ("c_exit", "shadow010")
_SELL_SLIP_FIELDS = ("book", "closed_n", "book_pnl_usd", "adjusted_pnl_usd", "delta_usd")
# book, closed n, stored book pnl, after sell-slip, delta. Do not invent others.
_SELL_SLIP_CONSTANT: tuple[tuple[str, int, str, str, str], ...] = (
    ("c_exit", 4, "22.66", "-17.79", "-40.45"),
    ("shadow010", 20, "-201.30", "-274.47", "-73.17"),
)
_PLAN_FAIL = frozenset({"missing", "unreadable", "schema", "empty", "no_closed", "bad"})

BookId = Literal["c_exit", "shadow010"]
SlipOrigin = Literal["export", "constant"]


@dataclass(frozen=True)
class BookCash:
    """Closed pnl stored in one side-book DB. None means the read failed."""

    book: BookId
    closed_n: int | None
    sum_pnl: Decimal | None
    missing: str | None


@dataclass(frozen=True)
class SellSlipSnap:
    """One book's #137 read-only sell-slip snapshot. Not a rewritten ledger."""

    book: BookId
    closed_n: int
    book_pnl: Decimal
    adjusted_pnl: Decimal
    delta: Decimal
    origin: SlipOrigin
    origin_label: str


def _book_id(value: str) -> BookId | None:
    match value:
        case "c_exit":
            return "c_exit"
        case "shadow010":
            return "shadow010"
        case _:
            return None


def shadow010_db_path() -> Path | None:
    """shadow010 side book. ``C_EXIT_SHADOW010_DB`` wins, even when missing."""
    override = os.environ.get(SHADOW010_DB_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    candidate = db_root() / SHADOW010_DB_NAME
    if candidate.is_file():
        return candidate
    return None


def _book_db_path(book: BookId) -> Path | None:
    match book:
        case "c_exit":
            return c_exit_db_path()
        case "shadow010":
            return shadow010_db_path()
        case _ as unreachable:
            return _never(unreachable)


def book_cash_line(row: BookCash) -> str:
    """Book Σpnl as stored. Missing stays missing, never a made-up zero."""
    if row.sum_pnl is None or row.closed_n is None:
        reason = row.missing or "不可读"
        return f"{row.book} 旁簿账面 Σpnl 不可读（{reason}）"
    return f"{row.book} 旁簿账面 Σpnl {fmt_cents_usd(row.sum_pnl)} · 已平 {row.closed_n}"


def sell_slip_note_line(snap: SellSlipSnap) -> str:
    """Visible #137 annotation. The history DB was not rewritten."""
    return (
        f"{SELL_SLIP_CITE} 卖出滑点补扣旁注 · 只读重算 {snap.book} {snap.closed_n} 笔已平 "
        f"账面 {fmt_cents_usd(snap.book_pnl)} → 补扣后 {fmt_cents_usd(snap.adjusted_pnl)}"
        f"（Δ {fmt_cents_usd(snap.delta)}）· 历史库未回写 · {snap.origin_label}"
    )


def _sum_closed(book: BookId, rows: list[sqlite3.Row]) -> BookCash:
    saw_status = any(row["status"] is not None for row in rows)
    closed_n = 0
    total = Decimal("0")
    for row in rows:
        status = row["status"]
        if not _is_closed(status, row["closed_at"], saw_status or status is not None):
            continue
        pnl = _parse_pnl(row["pnl"])
        if pnl is None:
            return BookCash(book, None, None, "坏数据")
        closed_n += 1
        total += _q_cents(pnl)
    return BookCash(book, closed_n, _q_cents(total), None)


def load_book_cash(book: BookId, path: Path | None = None) -> BookCash:
    """Closed Σpnl from the side-book file. Read-only; does not rewrite it."""
    target = path if path is not None else _book_db_path(book)
    if target is None or not target.is_file():
        return BookCash(book, None, None, "库不存在")
    try:
        with ro_connect(target) as conn:
            plan = _read_plan(conn)
            if plan in _PLAN_FAIL:
                reason = "库空" if plan == "empty" else "缺列"
                return BookCash(book, None, None, reason)
            rows = list(conn.execute(plan))
    except FileNotFoundError:
        return BookCash(book, None, None, "库不存在")
    except (OSError, sqlite3.Error, UnicodeError, ValueError):
        log.warning("side book cash unreadable")
        return BookCash(book, None, None, "库不可读")
    return _sum_closed(book, rows)


def _sell_slip_roots() -> list[Path]:
    package_root = Path(__file__).resolve().parents[1]
    repo = package_root.parent if package_root.name == "crawler" else package_root
    roots = [repo, repo / "crawler", package_root]
    try:
        roots.append(db_root())
    except Exception:
        log.warning("sell slip csv root unavailable")
    roots.append(Path.cwd())
    return roots


def sell_slip_export_path() -> Path | None:
    """Newest ``exports/c_exit_sell_slip_recalc*.csv``, if one exists.

    ``C_EXIT_SELL_SLIP_CSV`` wins when set, including when that path is missing,
    so a test can force the labeled display constant.
    """
    override = os.environ.get(SELL_SLIP_CSV_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    found: list[Path] = []
    seen: set[Path] = set()
    for root in _sell_slip_roots():
        folder = root / "exports"
        if not folder.is_dir():
            continue
        for candidate in folder.glob(SELL_SLIP_CSV_GLOB):
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            found.append(resolved)
    if not found:
        return None
    found.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
    return found[0]


def _parse_export_number(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "").replace("−", "-").replace(" ", "")
    if text.startswith("+"):
        text = text[1:]
    if not text or text.casefold() in _NULL:
        return None
    try:
        return _q_cents(Decimal(text))
    except InvalidOperation:
        return None


def _parse_closed_n(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.casefold() in _NULL:
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    if number < 0:
        return None
    return number


def _constant_snaps(label: str) -> tuple[SellSlipSnap, SellSlipSnap]:
    snaps: list[SellSlipSnap] = []
    for book_name, closed_n, book_pnl, adjusted, delta in _SELL_SLIP_CONSTANT:
        book = _book_id(book_name)
        if book is None:
            raise AssertionError(book_name)
        snaps.append(
            SellSlipSnap(
                book=book,
                closed_n=closed_n,
                book_pnl=Decimal(book_pnl),
                adjusted_pnl=Decimal(adjusted),
                delta=Decimal(delta),
                origin="constant",
                origin_label=label,
            )
        )
    return (snaps[0], snaps[1])


def _load_sell_slip_export(path: Path) -> tuple[SellSlipSnap, SellSlipSnap] | None:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            names = {name for name in (reader.fieldnames or []) if name}
            if not set(_SELL_SLIP_FIELDS).issubset(names):
                return None
            parsed: dict[BookId, SellSlipSnap] = {}
            for row in reader:
                book = _book_id(str(row.get("book") or "").strip())
                if book is None:
                    continue
                closed_n = _parse_closed_n(row.get("closed_n"))
                book_pnl = _parse_export_number(row.get("book_pnl_usd"))
                adjusted = _parse_export_number(row.get("adjusted_pnl_usd"))
                delta = _parse_export_number(row.get("delta_usd"))
                if closed_n is None or book_pnl is None or adjusted is None or delta is None:
                    return None
                parsed[book] = SellSlipSnap(
                    book=book,
                    closed_n=closed_n,
                    book_pnl=book_pnl,
                    adjusted_pnl=adjusted,
                    delta=delta,
                    origin="export",
                    origin_label=f"导出 {path.name}",
                )
    except (OSError, UnicodeError, csv.Error):
        log.warning("sell slip export unreadable")
        return None
    snaps: list[SellSlipSnap] = []
    for book in _SELL_SLIP_BOOKS:
        found = parsed.get(book)
        if found is None:
            return None
        snaps.append(found)
    return (snaps[0], snaps[1])


def load_sell_slip_snaps() -> tuple[SellSlipSnap, SellSlipSnap]:
    """Export under ``exports/`` when present; otherwise the labeled constant."""
    path = sell_slip_export_path()
    if path is None or not path.is_file():
        return _constant_snaps("旁注常量")
    loaded = _load_sell_slip_export(path)
    if loaded is None:
        log.warning("sell slip export unreadable")
        return _constant_snaps("旁注常量（导出不可读）")
    return loaded


def sell_slip_card_lines() -> tuple[str, ...]:
    """DB book Σpnl, then the #137 sell-slip note, for c_exit and shadow010."""
    lines: list[str] = []
    for snap in load_sell_slip_snaps():
        lines.append(book_cash_line(load_book_cash(snap.book)))
        lines.append(sell_slip_note_line(snap))
    return tuple(lines)
