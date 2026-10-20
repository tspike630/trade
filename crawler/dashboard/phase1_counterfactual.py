"""Read-only Phase1 soft-skip counterfactual for the OVERVIEW card.

The packaged 2026-10-20 table (evidence pack §2) is the card source. When
``exports/shadow_skip_stop_prone_live.csv`` is present, closed rows are
recomputed. If those cents differ from the package, the card shows the CSV
figures and says so. A missing, empty, or unreadable file falls back to the
packaged table.

A is ``shadow_R3_vol_chg_rsi``. B is ``shadow_liq_lt_150k``, and a NULL or
missing ``liq_usd`` is not low liquidity. C is the OR of A and B, not the
CSV alias column. This module does not soft-skip, does not open a database,
and does not change gates, weights, or environment files.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal, Never

from dashboard.shadow_phase0 import FLAG_LIQ, FLAG_R3, shadow_phase0_csv_path

log = logging.getLogger("dashboard.phase1_counterfactual")

CSV_ENV = "PHASE1_CF_CSV"
START_CASH = Decimal("10000")
PACKAGED_ASOF = "2026-10-20"

# Short nail. Visible on the card. Not a gate change.
PHASE1_CF_NAIL = (
    "影簿/反事实≠已改闸 · 不算有效性 · 不 soft-skip · 薄样本勿批改闸 · "
    "与 Phase0 双列并存 · #71/权0/日亏200 不动"
)
PHASE1_WINDOW = (
    "Phase0 墙钟仅≈5–6h · 影标=历史回贴非 OOS · 反事实≠已 soft-skip / ≠已改闸"
)
PHASE1_CANDIDATE = "互批预置候选优先 B（少跳过、同抓 SL、误伤 TP 0）· 候选"
NOTE_MATCH = "CSV 与证据包总表一致"
NOTE_DIVERGED = "与证据包总表不一致 · 以 CSV 重算"

_CLOSED = "closed"
_STOP = "stop_loss"
_TAKE = "take_profit"
_NULL_TOKENS = {"", "null", "none", "nan", "na", "n/a"}
_REQUIRED = ("status", "exit_reason", "pnl_usd", FLAG_R3, FLAG_LIQ)

CaseKey = Literal["A", "B", "C"]
SourceKind = Literal["csv", "packaged"]
FallbackKind = Literal["missing", "unreadable", "empty", "columns", "bad", "no_closed"]


@dataclass(frozen=True)
class CfCase:
    """One counterfactual skip set on the closed sample."""

    key: CaseKey
    title: str
    skip_n: int
    sl_hit: int
    sl_total: int
    tp_false: int
    remain_pnl: Decimal
    delta_pnl: Decimal


@dataclass(frozen=True)
class Phase1CfReport:
    """Numbers the card prints. ``note`` says where they came from."""

    source: SourceKind
    note: str
    closed: int
    baseline_pnl: Decimal
    equity: Decimal
    cases: tuple[CfCase, CfCase, CfCase]


def _never(value: Never) -> Never:
    raise AssertionError(f"unhandled variant: {value!r}")


def fallback_note(kind: FallbackKind) -> str:
    """Visible fallback label. The packaged table still fills the card."""
    label = f"回退证据包总表 {PACKAGED_ASOF}"
    if kind == "missing":
        return f"CSV 缺失 · {label}"
    if kind == "unreadable":
        return f"CSV 不可读 · {label}"
    if kind == "empty":
        return f"CSV 空 · {label}"
    if kind == "columns":
        return f"CSV 缺列 · {label}"
    if kind == "bad":
        return f"CSV 坏数据 · {label}"
    if kind == "no_closed":
        return f"CSV 无 closed · {label}"
    return _never(kind)


def _q_cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q_dollar(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def fmt_cents_usd(value: Decimal) -> str:
    """Signed cents without a thousands separator, minus as U+2212."""
    quantized = _q_cents(value)
    text = f"{abs(quantized):.2f}"
    if quantized > 0:
        return f"+${text}"
    if quantized < 0:
        return f"−${text}"
    return "$0.00"


def fmt_approx_usd(value: Decimal) -> str:
    """Nearest dollar, half up. Positive values keep the plus sign."""
    number = int(_q_dollar(value))
    if number > 0:
        return f"+${number}"
    if number < 0:
        return f"−${abs(number)}"
    return "$0"


def fmt_equity_dollar(value: Decimal) -> str:
    number = int(_q_dollar(value))
    if number < 0:
        return f"−${abs(number)}"
    return f"${number}"


def fmt_thousands_k(value: Decimal) -> str:
    thousands = (value / Decimal("1000")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"${thousands}k"


def _case(
    key: CaseKey,
    title: str,
    skip_n: int,
    sl_hit: int,
    sl_total: int,
    tp_false: int,
    remain_pnl: str,
    delta_pnl: str,
) -> CfCase:
    return CfCase(
        key=key,
        title=title,
        skip_n=skip_n,
        sl_hit=sl_hit,
        sl_total=sl_total,
        tp_false=tp_false,
        remain_pnl=Decimal(remain_pnl),
        delta_pnl=Decimal(delta_pnl),
    )


def packaged_report(note: str) -> Phase1CfReport:
    """Evidence-pack §2, 2026-10-20. Equity is 10000 + baseline ΣPnL."""
    baseline = Decimal("-1295.71")
    cases = (
        _case("A", "A R3", 30, 5, 10, 0, "-474.11", "821.60"),
        _case("B", "B liq<150k", 27, 5, 10, 0, "-450.20", "845.51"),
        _case("C", "C 并集", 41, 5, 10, 0, "-424.82", "870.89"),
    )
    return Phase1CfReport(
        source="packaged",
        note=note,
        closed=142,
        baseline_pnl=baseline,
        equity=START_CASH + baseline,
        cases=cases,
    )


def baseline_line(report: Phase1CfReport) -> str:
    return (
        f"基线：closed={report.closed} · ΣPnL≈{fmt_cents_usd(report.baseline_pnl)} · "
        f"权益≈{fmt_equity_dollar(report.equity)}"
    )


def case_line(case: CfCase) -> str:
    return (
        f"{case.title} skip{case.skip_n}：抓 SL {case.sl_hit}/{case.sl_total}、"
        f"TP {case.tp_false} → 剩余 Σ≈{fmt_approx_usd(case.remain_pnl)}"
        f"（Δ{fmt_approx_usd(case.delta_pnl)}）"
    )


def _case_by_key(report: Phase1CfReport, key: CaseKey) -> CfCase:
    for case in report.cases:
        if case.key == key:
            return case
    raise AssertionError(key)


def honest_line(report: Phase1CfReport) -> str:
    """C is the best of the three on this sample, and still short of $20k."""
    equity = START_CASH + _case_by_key(report, "C").remain_pnl
    label = fmt_thousands_k(equity)
    if equity < Decimal("20000"):
        return f"即使最优 C 权益也只≈{label}，填不满 $20k"
    return f"最优 C 权益≈{label}，对照目标 $20k"


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
    text = value.strip().lower()
    if text in _NULL_TOKENS:
        return False
    try:
        number = float(text)
    except ValueError:
        return False
    return number == number


def _parse_pnl(value: str) -> Decimal | None:
    text = value.strip()
    if text.lower() in _NULL_TOKENS:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def phase1_csv_path() -> Path | None:
    """CSV to check. ``PHASE1_CF_CSV`` wins, even when that path is missing.

    Otherwise the Phase0 resolver is used, including ``SHADOW_PHASE0_CSV``,
    so both cards read the same export.
    """
    override = os.environ.get(CSV_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return shadow_phase0_csv_path()


def _matches_package(report: Phase1CfReport) -> bool:
    package = packaged_report("")
    if report.closed != package.closed or report.baseline_pnl != package.baseline_pnl:
        return False
    if report.equity != package.equity:
        return False
    for got, expected in zip(report.cases, package.cases, strict=True):
        if got.key != expected.key or got.skip_n != expected.skip_n:
            return False
        if got.sl_hit != expected.sl_hit or got.sl_total != expected.sl_total:
            return False
        if got.tp_false != expected.tp_false:
            return False
        if got.remain_pnl != expected.remain_pnl or got.delta_pnl != expected.delta_pnl:
            return False
    return True


def _build_case(
    key: CaseKey,
    title: str,
    mask: list[bool],
    reasons: list[str],
    pnls: list[Decimal],
    baseline: Decimal,
    sl_total: int,
) -> CfCase:
    skip_pnl = Decimal("0")
    skip_n = 0
    sl_hit = 0
    tp_false = 0
    for flagged, reason, pnl in zip(mask, reasons, pnls, strict=True):
        if not flagged:
            continue
        skip_n += 1
        skip_pnl += pnl
        if reason == _STOP:
            sl_hit += 1
        elif reason == _TAKE:
            tp_false += 1
    skip_cents = _q_cents(skip_pnl)
    remain = baseline - skip_cents
    return CfCase(
        key=key,
        title=title,
        skip_n=skip_n,
        sl_hit=sl_hit,
        sl_total=sl_total,
        tp_false=tp_false,
        remain_pnl=remain,
        delta_pnl=-skip_cents,
    )


def summarize_phase1(
    rows: list[dict[str, str | None]],
    fieldnames: list[str] | None,
) -> Phase1CfReport | FallbackKind:
    """Closed-only A/B/C. Returns a fallback kind when the file cannot be summed."""
    names = [name for name in (fieldnames or []) if name]
    if not names:
        return "empty"
    present = set(names)
    if any(column not in present for column in _REQUIRED):
        return "columns"
    has_liq_value = "liq_usd" in present

    reasons: list[str] = []
    pnls: list[Decimal] = []
    r3_mask: list[bool] = []
    liq_mask: list[bool] = []
    sl_total = 0
    for row in rows:
        if _cell(row, "status").lower() != _CLOSED:
            continue
        pnl = _parse_pnl(_cell(row, "pnl_usd"))
        if pnl is None:
            return "bad"
        reason = _cell(row, "exit_reason")
        reasons.append(reason)
        pnls.append(pnl)
        r3_on = _flag_on(_cell(row, FLAG_R3))
        liq_on = _flag_on(_cell(row, FLAG_LIQ))
        if has_liq_value and not _liq_measured(_cell(row, "liq_usd")):
            liq_on = False
        r3_mask.append(r3_on)
        liq_mask.append(liq_on)
        if reason == _STOP:
            sl_total += 1

    if not pnls:
        return "no_closed"

    baseline = _q_cents(sum(pnls, Decimal("0")))
    union = [left or right for left, right in zip(r3_mask, liq_mask, strict=True)]
    cases = (
        _build_case("A", "A R3", r3_mask, reasons, pnls, baseline, sl_total),
        _build_case("B", "B liq<150k", liq_mask, reasons, pnls, baseline, sl_total),
        _build_case("C", "C 并集", union, reasons, pnls, baseline, sl_total),
    )
    return Phase1CfReport(
        source="csv",
        note="",
        closed=len(pnls),
        baseline_pnl=baseline,
        equity=START_CASH + baseline,
        cases=cases,
    )


def load_phase1_counterfactual(path: Path | None = None) -> Phase1CfReport:
    """Packaged table, replaced by a CSV recomputation when the cents differ."""
    target = path if path is not None else phase1_csv_path()
    if target is None or not target.is_file():
        return packaged_report(fallback_note("missing"))
    try:
        with target.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeError, csv.Error):
        log.warning("phase1 counterfactual csv unreadable")
        return packaged_report(fallback_note("unreadable"))
    summarized = summarize_phase1(rows, fieldnames)
    if isinstance(summarized, str):
        return packaged_report(fallback_note(summarized))
    if _matches_package(summarized):
        return replace(summarized, note=NOTE_MATCH)
    return replace(summarized, note=NOTE_DIVERGED)
