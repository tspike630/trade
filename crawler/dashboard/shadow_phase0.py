"""Read-only Phase0 stop-prone shadow summary.

The card reads ``exports/shadow_skip_stop_prone_live.csv``. It does not
soft-skip, does not write a database, and does not change gates or weights.
NULL or missing ``liq_usd`` is not low liquidity: ``shadow_liq_lt_150k`` stays 0.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dashboard.settings import db_root

log = logging.getLogger("dashboard.shadow_phase0")

FLAG_R3 = "shadow_R3_vol_chg_rsi"
FLAG_LIQ = "shadow_liq_lt_150k"
CSV_NAME = "shadow_skip_stop_prone_live.csv"
CSV_ENV = "SHADOW_PHASE0_CSV"
# Phase0 marks only. The two flags are not a validity result and not a skip.
PHASE0_NAIL = "Phase0 只打标≠有效性；NULL liq 不打 liq 标；48h 后再批 Phase1"

_CLOSED = "closed"
_STOP = "stop_loss"
_TAKE = "take_profit"
_NULL_LIQ = {"", "null", "none", "nan", "na"}


@dataclass(frozen=True)
class ShadowColumn:
    """One flag column on closed rows. Missing column leaves the counts empty."""

    name: str
    present: bool
    n: int | None
    sl_hit: int | None
    sl_total: int | None
    tp_false: int | None
    tp_total: int | None


@dataclass(frozen=True)
class ShadowPhase0Report:
    available: bool
    reason: str
    closed: int
    r3: ShadowColumn
    liq: ShadowColumn
    both: int | None
    liq_null: int | None


def _empty_column(name: str, *, present: bool) -> ShadowColumn:
    return ShadowColumn(
        name=name,
        present=present,
        n=0 if present else None,
        sl_hit=None,
        sl_total=None,
        tp_false=None,
        tp_total=None,
    )


def _gap(reason: str) -> ShadowPhase0Report:
    return ShadowPhase0Report(
        available=False,
        reason=reason,
        closed=0,
        r3=_empty_column(FLAG_R3, present=False),
        liq=_empty_column(FLAG_LIQ, present=False),
        both=None,
        liq_null=None,
    )


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


def _liq_measured(value: str) -> bool:
    """A blank, null token, or non-number is not a liquidity measurement."""
    text = value.strip().lower()
    if text in _NULL_LIQ:
        return False
    try:
        number = float(text)
    except ValueError:
        return False
    return number == number


def _search_roots() -> list[Path]:
    package_root = Path(__file__).resolve().parents[1]
    repo = package_root.parent if package_root.name == "crawler" else package_root
    roots = [repo, repo / "crawler", package_root]
    try:
        roots.append(db_root())
    except Exception:
        log.warning("phase0 csv root unavailable")
    roots.append(Path.cwd())
    return roots


def shadow_phase0_csv_path() -> Path | None:
    """First existing ``exports/shadow_skip_stop_prone_live.csv``.

    ``SHADOW_PHASE0_CSV`` wins when set, including when that path is missing,
    so a test can force the gap path. Otherwise repo ``exports/`` is preferred,
    then ``crawler/exports/`` (where the tagger writes).
    """
    override = os.environ.get(CSV_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    seen: set[Path] = set()
    for root in _search_roots():
        candidate = root / "exports" / CSV_NAME
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def summarize_shadow_phase0(
    rows: list[dict[str, str | None]],
    fieldnames: list[str] | None,
) -> ShadowPhase0Report:
    """Closed-only counts. Open rows are ignored. NULL liq cannot raise the liq flag."""
    names = [name for name in (fieldnames or []) if name]
    if not names:
        return _gap("CSV 空")
    present = set(names)
    has_r3 = FLAG_R3 in present
    has_liq = FLAG_LIQ in present
    has_exit = "exit_reason" in present
    has_liq_value = "liq_usd" in present
    has_status = "status" in present

    closed_rows: list[dict[str, str | None]] = []
    for row in rows:
        if has_status:
            if _cell(row, "status").lower() != _CLOSED:
                continue
        closed_rows.append(row)

    sl_total = 0
    tp_total = 0
    if has_exit:
        for row in closed_rows:
            reason = _cell(row, "exit_reason")
            if reason == _STOP:
                sl_total += 1
            elif reason == _TAKE:
                tp_total += 1

    def tally(flag_name: str, *, null_liq_blocks: bool) -> tuple[int, int, int]:
        n_flag = 0
        sl_hit = 0
        tp_false = 0
        for row in closed_rows:
            if not _flag_on(_cell(row, flag_name)):
                continue
            if null_liq_blocks and has_liq_value and not _liq_measured(_cell(row, "liq_usd")):
                continue
            n_flag += 1
            if not has_exit:
                continue
            reason = _cell(row, "exit_reason")
            if reason == _STOP:
                sl_hit += 1
            elif reason == _TAKE:
                tp_false += 1
        return n_flag, sl_hit, tp_false

    r3_n, r3_sl, r3_tp = tally(FLAG_R3, null_liq_blocks=False) if has_r3 else (0, 0, 0)
    liq_n, liq_sl, liq_tp = tally(FLAG_LIQ, null_liq_blocks=True) if has_liq else (0, 0, 0)

    both: int | None
    if has_r3 and has_liq:
        both = 0
        for row in closed_rows:
            if not _flag_on(_cell(row, FLAG_R3)):
                continue
            if not _flag_on(_cell(row, FLAG_LIQ)):
                continue
            if has_liq_value and not _liq_measured(_cell(row, "liq_usd")):
                continue
            both += 1
    else:
        both = None

    liq_null: int | None
    if has_liq_value:
        liq_null = sum(1 for row in closed_rows if not _liq_measured(_cell(row, "liq_usd")))
    else:
        liq_null = None

    def column(
        name: str,
        present_flag: bool,
        n_flag: int,
        sl_hit: int,
        tp_false: int,
    ) -> ShadowColumn:
        if not present_flag:
            return _empty_column(name, present=False)
        return ShadowColumn(
            name=name,
            present=True,
            n=n_flag,
            sl_hit=sl_hit if has_exit else None,
            sl_total=sl_total if has_exit else None,
            tp_false=tp_false if has_exit else None,
            tp_total=tp_total if has_exit else None,
        )

    return ShadowPhase0Report(
        available=True,
        reason="",
        closed=len(closed_rows),
        r3=column(FLAG_R3, has_r3, r3_n, r3_sl, r3_tp),
        liq=column(FLAG_LIQ, has_liq, liq_n, liq_sl, liq_tp),
        both=both,
        liq_null=liq_null,
    )


def load_shadow_phase0(path: Path | None = None) -> ShadowPhase0Report:
    """Read the Phase0 CSV. A missing or unreadable file is a gap, not an exception."""
    target = path if path is not None else shadow_phase0_csv_path()
    if target is None or not target.is_file():
        return _gap("CSV 缺失")
    try:
        with target.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error):
        log.warning("phase0 shadow csv unreadable")
        return _gap("CSV 不可读")
    return summarize_shadow_phase0(rows, fieldnames)


def format_count(column: ShadowColumn) -> str:
    if not column.present or column.n is None:
        return "—"
    return str(column.n)


def format_sl(column: ShadowColumn) -> str:
    if not column.present or column.sl_hit is None or column.sl_total is None:
        return "—"
    return f"{column.sl_hit}/{column.sl_total}"


def format_tp(column: ShadowColumn) -> str:
    if not column.present or column.tp_false is None or column.tp_total is None:
        return "—"
    return f"{column.tp_false}/{column.tp_total}"


def format_both(report: ShadowPhase0Report) -> str:
    if report.both is None:
        return "—"
    return str(report.both)
