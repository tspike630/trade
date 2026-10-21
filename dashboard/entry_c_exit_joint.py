"""Read-only Experiment A card: entry shadow × C-exit joint evidence.

Prefers ``exports/shadow_entry_x_c_exit_joint_*.csv`` when that export exists.
A missing, empty, or unreadable export falls back to the packaged 2026-10-20
sample-in fixture. Side-book rows are counted read-only and never replace the
table: zero fills keep the sample-in numbers and the nail. Σpnl is not a
field on the report. This module does not write a database, does not edit
environment files, and does not promote a parameter onto the main book.
"""

from __future__ import annotations

import csv
import logging
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Literal, Never

from dashboard.c_exit_shadow import c_exit_db_path
from dashboard.data import ro_connect
from dashboard.settings import db_root

log = logging.getLogger("dashboard.entry_c_exit_joint")

CSV_ENV = "ENTRY_C_EXIT_JOINT_CSV"
CSV_GLOB = "shadow_entry_x_c_exit_joint_*.csv"
FIXTURE_NAME = "shadow_entry_x_c_exit_joint_2026-10-20.csv"
PACKAGED_ASOF = "2026-10-20"
FLAG_R3 = "shadow_R3_vol_chg_rsi"
FLAG_LIQ = "shadow_liq_lt_150k"

# Visible nail when the side book has no fills and the join is sample-in.
JOINT_NAIL = "样本内假设 · 旁簿成交=0 · ≠升主/≠抬权/≠Phase1"
# liq NULL is a collection gap, not low-liquidity alpha. R3 is the primary column.
JOINT_LIQ_NAIL = "liq：NULL/missing ≠ 真<150k · R3 优先"
JOINT_LIQ_LIVE = "现网 hot_liq missing≈38.9% · 采集缺口≠低流动性 alpha"
JOINT_DISCIPLINE = "只读旁证 · 不改 .env · 不升主 · 不抬权 · 不 Phase1"
LIQ_THR = Decimal("150000")
JOINT_SHARE = "止损占负 = stop_loss 负 pnl 合计 / 全部负贡献合计"
JOINT_PNL_NOTE = "Σpnl 不展示 · 不是升主信号 · 优先止损占负 / 峰 / n"
JOINT_VERDICT = "部分支持「入口影子↔更差左尾」→ 不升主、不抬权、不 Phase1；旁簿攒成交后再重跑。"
JOINT_VERDICT_HOLD = "未保持 flag=1 止损占负不低于 flag=0 · 不升主、不抬权、不 Phase1；旁簿攒成交后再重跑。"
JOINT_VERDICT_FORWARD = "占负同向但非纯样本内 · 不升主、不抬权、不 Phase1；旁簿攒成交后再重跑。"

_STOP = "stop_loss"
_NULL = frozenset({"", "null", "none", "nan", "na", "n/a"})
_TAGS = ("c_sl5_h60", "c_sl5_h90", "c_sl4_h90")
_FLAG_ORDER = ("R3", "liq", "R3∨liq")
_HIGHLIGHT_TAGS = frozenset({"c_sl5_h60", "c_sl5_h90"})
_HIGHLIGHT_FLAGS = frozenset({"R3", "liq"})
TABLE_HEADERS: tuple[tuple[str, str], ...] = (
    ("旗", ""),
    ("格", ""),
    ("占负1", "num"),
    ("占负0", "num"),
    ("Δ", "num"),
    ("SL笔", "num"),
    ("峰1", "num"),
    ("峰0", "num"),
)
LIQ_SPLIT_HEADERS: tuple[tuple[str, str], ...] = (
    ("列", ""),
    ("n", "num"),
    ("读法", ""),
)

SourceKind = Literal["export", "packaged"]
FallbackKind = Literal["missing", "unreadable", "empty", "columns", "bad", "no_rows"]
SideTable = Literal["positions", "shadow_positions", "c_exit_positions", "paper_positions"]
_SIDE_TABLES: tuple[SideTable, ...] = (
    "positions",
    "shadow_positions",
    "c_exit_positions",
    "paper_positions",
)


@dataclass(frozen=True)
class JointContrast:
    """flag=1 versus flag=0 on one exit tag. No Σpnl."""

    flag: str
    tag: str
    n1: int
    n0: int
    share1: Decimal | None
    share0: Decimal | None
    delta_pp: Decimal | None
    sl_n1: int
    sl_n0: int
    peak1: Decimal | None
    peak0: Decimal | None
    key: bool
    ge: bool


@dataclass(frozen=True)
class JointReport:
    """Numbers the card prints. ``note`` says why this table is on screen."""

    source: SourceKind
    note: str
    sample_in: bool
    side_fills: int
    closed: int
    contrasts: tuple[JointContrast, ...]
    ge_count: int
    key_ge_count: int
    key_n: int
    main_peak1: Decimal | None
    main_peak0: Decimal | None
    main_sl_r3: int
    main_sl_liq: int
    main_sl_total: int
    r3_n: int
    liq_true_n: int
    liq_missing_n: int
    liq_excluded_n: int
    liq_column: bool
    # None = side book read fine; otherwise why the count is missing (not 0).
    side_missing: str | None = None


@dataclass
class _Bucket:
    n: int = 0
    neg: Decimal = Decimal("0")
    sl_neg: Decimal = Decimal("0")
    sl_n: int = 0
    peak: Decimal | None = None


class _BadRow(Exception):
    """A row that cannot be summed. The card falls back to the fixture."""


def _never(value: Never) -> Never:
    raise AssertionError(f"unhandled variant: {value!r}")


def fallback_note(kind: FallbackKind) -> str:
    """Visible fallback label. The packaged sample-in table still fills the card."""
    tail = f"回退样本内夹具 {PACKAGED_ASOF}"
    match kind:
        case "missing":
            return f"旁簿/live joint 空 · {tail}"
        case "unreadable":
            return f"联合表不可读 · {tail}"
        case "empty":
            return f"联合表空 · {tail}"
        case "columns":
            return f"联合表缺列 · {tail}"
        case "bad":
            return f"联合表坏数据 · {tail}"
        case "no_rows":
            return f"联合表无行 · {tail}"
        case _ as unreachable:
            return _never(unreachable)


def _q_cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q_tenth(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def fmt_cents_usd(value: Decimal) -> str:
    """Signed cents. Minus is U+2212, matching the other evidence cards."""
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
    return f"{value}%"


def fmt_delta(value: Decimal | None) -> str:
    if value is None:
        return "—"
    if value > 0:
        return f"+{value}pp"
    if value < 0:
        return f"−{abs(value)}pp"
    return "0.0pp"


def fmt_peak(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return fmt_cents_usd(value)


def _cell(row: dict[str, str | None], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _flag_on(value: str) -> bool:
    text = value.strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    try:
        return float(text) == 1.0
    except ValueError:
        return False


def _parse_pnl(value: str) -> Decimal | None:
    text = value.strip()
    if text.lower() in _NULL:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def _stamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _required_columns() -> tuple[str, ...]:
    columns = [FLAG_R3, FLAG_LIQ, "main_pnl_usd", "main_exit_reason"]
    for tag in _TAGS:
        columns.extend((f"{tag}_hit", f"{tag}_exit_reason", f"{tag}_pnl_usd"))
    return tuple(columns)


def _search_roots() -> list[Path]:
    package_root = Path(__file__).resolve().parents[1]
    repo = package_root.parent if package_root.name == "crawler" else package_root
    roots = [repo, repo / "crawler", package_root]
    try:
        roots.append(db_root())
    except Exception:
        log.warning("joint csv root unavailable")
    roots.append(Path.cwd())
    return roots


def fixture_path() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / FIXTURE_NAME


def joint_export_path() -> Path | None:
    """Newest ``exports/shadow_entry_x_c_exit_joint_*.csv``.

    ``ENTRY_C_EXIT_JOINT_CSV`` wins when set, including when that path is
    missing, so a test can force the packaged sample-in path.
    """
    override = os.environ.get(CSV_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    found: list[Path] = []
    seen: set[Path] = set()
    for root in _search_roots():
        folder = root / "exports"
        if not folder.is_dir():
            continue
        for candidate in folder.glob(CSV_GLOB):
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


def _is_fixture(path: Path) -> bool:
    try:
        return path.resolve() == fixture_path().resolve()
    except OSError:
        return False


def _read_csv(path: Path) -> tuple[list[dict[str, str | None]], list[str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [name for name in (reader.fieldnames or []) if name]
        rows = [dict(row) for row in reader]
    return rows, fieldnames


def _sample_in(rows: list[dict[str, str | None]]) -> bool:
    """True unless a row opened after it was tagged. Missing stamps stay sample-in."""
    for row in rows:
        opened = _stamp(_cell(row, "opened_at"))
        tagged = _stamp(_cell(row, "tagged_at"))
        if opened is None or tagged is None:
            continue
        if opened > tagged:
            return False
    return True


def _absorb(bucket: _Bucket, pnl: Decimal | None, reason: str) -> None:
    bucket.n += 1
    if pnl is None:
        return
    if pnl < 0:
        bucket.neg += pnl
    if reason != _STOP:
        return
    bucket.sl_n += 1
    if pnl < 0:
        bucket.sl_neg += pnl
    if bucket.peak is None or pnl < bucket.peak:
        bucket.peak = pnl


def _shares(bucket: _Bucket) -> tuple[Decimal | None, Decimal | None]:
    if bucket.neg == 0:
        return None, None
    raw = bucket.sl_neg / bucket.neg * Decimal("100")
    return raw, _q_tenth(raw)


def _peak(bucket: _Bucket) -> Decimal | None:
    if bucket.peak is None:
        return None
    return _q_cents(bucket.peak)


def _classify_liq(row: dict[str, str | None], *, has_liq_value: bool) -> tuple[bool, bool, bool]:
    """Return (true_<150k, missing, raw flag dropped).

    A NULL or missing ``liq_usd`` is never low liquidity. R3 is not touched.
    Without the value column the raw flag is dropped, not treated as alpha.
    """
    raw = _flag_on(_cell(row, FLAG_LIQ))
    if not has_liq_value:
        return False, True, raw
    amount = _parse_pnl(_cell(row, "liq_usd"))
    if amount is None:
        return False, True, raw
    true = raw and amount < LIQ_THR
    return true, False, raw and not true


def _tag_pnl(row: dict[str, str | None], tag: str) -> Decimal | None:
    hit = _flag_on(_cell(row, f"{tag}_hit"))
    raw = _cell(row, f"{tag}_pnl_usd")
    if not hit:
        return None
    pnl = _parse_pnl(raw)
    if pnl is None:
        raise _BadRow
    return pnl


def _contrast(
    flag: str,
    tag: str,
    on: _Bucket,
    off: _Bucket,
) -> JointContrast:
    raw1, share1 = _shares(on)
    raw0, share0 = _shares(off)
    delta = None if raw1 is None or raw0 is None else _q_tenth(raw1 - raw0)
    ge = raw1 is not None and raw0 is not None and raw1 >= raw0
    return JointContrast(
        flag=flag,
        tag=tag,
        n1=on.n,
        n0=off.n,
        share1=share1,
        share0=share0,
        delta_pp=delta,
        sl_n1=on.sl_n,
        sl_n0=off.sl_n,
        peak1=_peak(on),
        peak0=_peak(off),
        key=flag in _HIGHLIGHT_FLAGS and tag in _HIGHLIGHT_TAGS,
        ge=ge,
    )


def _main_peak(mask: list[bool], reasons: list[str], pnls: list[Decimal]) -> Decimal | None:
    peak: Decimal | None = None
    for flagged, reason, pnl in zip(mask, reasons, pnls, strict=True):
        if not flagged or reason != _STOP:
            continue
        if peak is None or pnl < peak:
            peak = pnl
    if peak is None:
        return None
    return _q_cents(peak)


def _gap(note: str) -> JointReport:
    return JointReport(
        source="packaged",
        note=note,
        sample_in=True,
        side_fills=0,
        closed=0,
        contrasts=(),
        ge_count=0,
        key_ge_count=0,
        key_n=0,
        main_peak1=None,
        main_peak0=None,
        main_sl_r3=0,
        main_sl_liq=0,
        main_sl_total=0,
        r3_n=0,
        liq_true_n=0,
        liq_missing_n=0,
        liq_excluded_n=0,
        liq_column=False,
    )


def summarize_joint(
    rows: list[dict[str, str | None]],
    fieldnames: list[str] | None,
) -> JointReport | FallbackKind:
    """Nine flag×tag contrasts. Returns a fallback kind when the file cannot be summed."""
    names = [name for name in (fieldnames or []) if name]
    if not names:
        return "empty"
    present = set(names)
    if any(column not in present for column in _required_columns()):
        return "columns"
    if not rows:
        return "no_rows"
    has_liq_value = "liq_usd" in present

    parsed: list[tuple[bool, bool, str, Decimal, dict[str, tuple[Decimal | None, str]]]] = []
    r3_n = 0
    liq_true_n = 0
    liq_missing_n = 0
    liq_excluded_n = 0
    try:
        for row in rows:
            main = _parse_pnl(_cell(row, "main_pnl_usd"))
            if main is None:
                raise _BadRow
            r3_on = _flag_on(_cell(row, FLAG_R3))
            liq_on, missing, excluded = _classify_liq(row, has_liq_value=has_liq_value)
            if r3_on:
                r3_n += 1
            if liq_on:
                liq_true_n += 1
            if missing:
                liq_missing_n += 1
            if excluded:
                liq_excluded_n += 1
            exits = {
                tag: (_tag_pnl(row, tag), _cell(row, f"{tag}_exit_reason"))
                for tag in _TAGS
            }
            parsed.append((r3_on, liq_on, _cell(row, "main_exit_reason"), main, exits))
    except _BadRow:
        return "bad"

    groups: dict[tuple[str, str, bool], _Bucket] = {
        (flag, tag, bit): _Bucket()
        for flag in _FLAG_ORDER
        for tag in _TAGS
        for bit in (True, False)
    }
    reasons: list[str] = []
    pnls: list[Decimal] = []
    r3_mask: list[bool] = []
    liq_mask: list[bool] = []
    sl_total = 0
    sl_r3 = 0
    sl_liq = 0
    for r3_on, liq_on, reason, main, exits in parsed:
        reasons.append(reason)
        pnls.append(main)
        r3_mask.append(r3_on)
        liq_mask.append(liq_on)
        if reason == _STOP:
            sl_total += 1
            if r3_on:
                sl_r3 += 1
            if liq_on:
                sl_liq += 1
        union = r3_on or liq_on
        active = {"R3": r3_on, "liq": liq_on, "R3∨liq": union}
        for tag in _TAGS:
            pnl, exit_reason = exits[tag]
            for flag in _FLAG_ORDER:
                _absorb(groups[(flag, tag, active[flag])], pnl, exit_reason)

    contrasts = tuple(
        _contrast(flag, tag, groups[(flag, tag, True)], groups[(flag, tag, False)])
        for flag in _FLAG_ORDER
        for tag in _TAGS
    )
    key_rows = tuple(row for row in contrasts if row.key)
    return JointReport(
        source="export",
        note="",
        sample_in=_sample_in(rows),
        side_fills=0,
        closed=len(parsed),
        contrasts=contrasts,
        ge_count=sum(1 for row in contrasts if row.ge),
        key_ge_count=sum(1 for row in key_rows if row.ge),
        key_n=len(key_rows),
        main_peak1=_main_peak(r3_mask, reasons, pnls),
        main_peak0=_main_peak([not bit for bit in r3_mask], reasons, pnls),
        main_sl_r3=sl_r3,
        main_sl_liq=sl_liq,
        main_sl_total=sl_total,
        r3_n=r3_n,
        liq_true_n=liq_true_n,
        liq_missing_n=liq_missing_n,
        liq_excluded_n=liq_excluded_n,
        liq_column=has_liq_value,
    )


def _count_sql(table: SideTable) -> str:
    match table:
        case "positions":
            return "SELECT COUNT(*) FROM positions"
        case "shadow_positions":
            return "SELECT COUNT(*) FROM shadow_positions"
        case "c_exit_positions":
            return "SELECT COUNT(*) FROM c_exit_positions"
        case "paper_positions":
            return "SELECT COUNT(*) FROM paper_positions"
        case _ as unreachable:
            return _never(unreachable)


def side_book_count_or_missing() -> tuple[int | None, str | None]:
    """Rows in the C-exit side book, or (None, reason). Missing is never 0."""
    path = c_exit_db_path()
    if path is None:
        return None, "库路径未解析"
    if not path.is_file():
        return None, "库不存在"
    try:
        with ro_connect(path) as conn:
            listed = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            names = {str(row["name"]) for row in listed}
            for table in _SIDE_TABLES:
                if table not in names:
                    continue
                counted = conn.execute(_count_sql(table)).fetchone()
                if counted is None:
                    return None, "计数无返回"
                return int(counted[0]), None
    except (OSError, sqlite3.Error, UnicodeError, ValueError):
        log.warning("entry joint side book unreadable")
        return None, "库不可读"
    return None, "无旁簿表"


def side_book_position_count() -> int | None:
    """Rows in the C-exit side book. None when missing/unreadable (shown as 缺失)."""
    return side_book_count_or_missing()[0]


def _source_note(source: SourceKind, sample_in: bool) -> str:
    match source:
        case "packaged":
            return f"样本内夹具 {PACKAGED_ASOF}"
        case "export":
            if sample_in:
                return "exports 联合表 · 样本内重放"
            return "exports 联合表 · 含打标后新开 · 仍不升主"
        case _ as unreachable:
            return _never(unreachable)


def _from_fixture(note: str) -> JointReport:
    path = fixture_path()
    if not path.is_file():
        return _gap(note or "样本内夹具缺失")
    try:
        rows, fieldnames = _read_csv(path)
    except (OSError, UnicodeError, csv.Error):
        log.warning("entry joint fixture unreadable")
        return _gap(note or "样本内夹具不可读")
    summarized = summarize_joint(rows, fieldnames)
    if isinstance(summarized, str):
        return _gap(fallback_note(summarized))
    return replace(summarized, source="packaged", note=note, side_fills=0)


def _with_side(
    report: JointReport, side_fills: int | None, missing: str | None = None
) -> JointReport:
    note = report.note
    if side_fills is None:
        reason = missing or "不可读"
        note = f"{note} · 旁簿成交：缺失（{reason}）"
        return replace(report, side_fills=0, side_missing=reason, note=note)
    if side_fills > 0:
        note = f"{note} · 旁簿库 {side_fills} 笔 · 联合表无旁簿实盘 pnl"
    return replace(report, side_fills=side_fills, note=note)


def load_entry_c_exit_joint(path: Path | None = None) -> JointReport:
    """Export when present, otherwise the packaged sample-in fixture.

    An empty side book does not blank the card. The fixture stays on screen
    and the nail says the join is sample-in with zero side-book fills.
    """
    side_fills, side_missing = side_book_count_or_missing()
    target = path if path is not None else joint_export_path()
    if target is None or not target.is_file():
        return _with_side(packaged_report(fallback_note("missing")), side_fills, side_missing)
    try:
        rows, fieldnames = _read_csv(target)
    except (OSError, UnicodeError, csv.Error):
        log.warning("entry joint csv unreadable")
        return _with_side(packaged_report(fallback_note("unreadable")), side_fills, side_missing)
    summarized = summarize_joint(rows, fieldnames)
    if isinstance(summarized, str):
        return _with_side(packaged_report(fallback_note(summarized)), side_fills, side_missing)
    source: SourceKind = "packaged" if _is_fixture(target) else "export"
    report = replace(
        summarized,
        source=source,
        note=_source_note(source, summarized.sample_in),
    )
    return _with_side(report, side_fills, side_missing)


def packaged_report(note: str) -> JointReport:
    """Packaged 2026-10-20 sample-in join. Not side-book cash."""
    return _from_fixture(note)


def nail_line(report: JointReport) -> str:
    if report.side_missing:
        kind = "样本内假设" if report.sample_in else "含打标后新开"
        return f"{kind} · 旁簿成交：缺失（{report.side_missing}） · ≠升主/≠抬权/≠Phase1"
    if report.sample_in and report.side_fills == 0:
        return JOINT_NAIL
    kind = "样本内假设" if report.sample_in else "含打标后新开"
    return f"{kind} · 旁簿成交={report.side_fills} · ≠升主/≠抬权/≠Phase1"


def nine_line(report: JointReport) -> str:
    total = len(report.contrasts)
    if total == 0:
        return "止损占负不足"
    return (
        f"止损占负 flag=1 ≥ flag=0：{report.ge_count}/{total}"
        f" · R3/liq × c_sl5_h60、c_sl5_h90 为 {report.key_ge_count}/{report.key_n}"
    )


def _coarse(value: Decimal, *, drop_cents: bool) -> int:
    cents = abs(_q_cents(value))
    if drop_cents:
        return int(cents.to_integral_value(rounding=ROUND_DOWN))
    return int(cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def peak_line(report: JointReport) -> str:
    """Worst replay peaks, plus the thin main-book stop count. Not a Σpnl line."""
    peaks1 = [row.peak1 for row in report.contrasts if row.peak1 is not None]
    peaks0 = [row.peak0 for row in report.contrasts if row.peak0 is not None]
    if not peaks1 or not peaks0:
        return "峰不足 · n_SL 薄 · 不够升主"
    worst1 = min(peaks1)
    worst0 = min(peaks0)
    coarse1 = _coarse(worst1, drop_cents=True)
    coarse0 = _coarse(worst0, drop_cents=False)
    text = f"峰 −{coarse1} vs −{coarse0} · 重放 {fmt_cents_usd(worst1)} vs {fmt_cents_usd(worst0)}"
    if report.main_peak1 is not None and report.main_peak0 is not None:
        text += f" · 主簿 {fmt_cents_usd(report.main_peak1)} vs {fmt_cents_usd(report.main_peak0)}"
    if report.main_sl_total:
        thin = (
            f"n_SL 薄（主簿 R3 {report.main_sl_r3}/{report.main_sl_total}"
            f" · liq {report.main_sl_liq}/{report.main_sl_total}）"
        )
    else:
        thin = "n_SL 薄"
    return f"{text} · {thin}"


def verdict_line(report: JointReport) -> str:
    supported = bool(report.contrasts) and report.ge_count == len(report.contrasts)
    if supported and report.sample_in:
        return JOINT_VERDICT
    if supported:
        return JOINT_VERDICT_FORWARD
    return JOINT_VERDICT_HOLD


def liq_split_rows(report: JointReport) -> tuple[list[list[str]], list[list[str]]]:
    """R3 beside true <150k and NULL/missing. The gap is not an alpha column."""
    if report.liq_column:
        missing_read = "不入 liq=1"
    else:
        missing_read = "无 liq_usd 列"
    rows = [
        ["R3", str(report.r3_n), "主旁证 · 不看 liq 缺口"],
        ["真<150k", str(report.liq_true_n), "liq=1 仅此"],
        ["NULL/missing", str(report.liq_missing_n), missing_read],
    ]
    tones = [
        ["", "", ""],
        ["", "", "mute"],
        ["down", "down", "down"],
    ]
    return rows, tones


def liq_risk_line(report: JointReport) -> str:
    """Side note on liq flag=1. Missing rows stay out of that cell."""
    if not report.liq_column:
        return (
            f"liq flag=1 旁注：无 liq_usd · 未分成 NULL/missing 与真<150k"
            f" · 原始 flag 不入列 · {JOINT_LIQ_NAIL}"
        )
    return (
        f"liq flag=1 旁注：真<150k {report.liq_true_n}"
        f" · NULL/missing {report.liq_missing_n} 不入列"
        f" · 剔出 {report.liq_excluded_n}"
        f" · R3∨liq = R3 或真<150k"
    )


def contrast_cells(row: JointContrast) -> list[str]:
    if row.flag == "R3" and row.key:
        flag = "★ R3"
    elif row.flag == "liq":
        flag = "liq†"
    else:
        flag = row.flag
    return [
        flag,
        row.tag,
        fmt_share(row.share1),
        fmt_share(row.share0),
        fmt_delta(row.delta_pp),
        f"{row.sl_n1}/{row.sl_n0}",
        fmt_peak(row.peak1),
        fmt_peak(row.peak0),
    ]


def table_rows(report: JointReport) -> tuple[list[list[str]], list[list[str]]]:
    rows: list[list[str]] = []
    tones: list[list[str]] = []
    for item in report.contrasts:
        rows.append(contrast_cells(item))
        mark = "down" if item.flag == "liq" or item.key else ""
        tones.append(["", "", mark, "", mark, "", mark, ""])
    return rows, tones
