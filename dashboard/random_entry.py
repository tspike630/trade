"""Packaged v2 U/L/J random-entry baseline for the entry cards.

The numbers are TP10/SL5/h60 from
``exports/random_entry_shared_seed20261021_v2_ulj.csv``. When that file is
absent the packaged triple is shown. The low-liquidity new-coin slice is
retired either way. This module only reads.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

from dashboard.settings import db_root

log = logging.getLogger("dashboard.random_entry")

CSV_NAME = "random_entry_shared_seed20261021_v2_ulj.csv"
CSV_ENV = "RANDOM_ENTRY_CSV"
# Packaged v2 U/L/J, percent points. CSV cells override these when readable.
PACKAGED_PRE_COST = -1.46
PACKAGED_COST_120 = -2.65
PACKAGED_TIER_COST = -4.92
LOW_LIQ_SLICE = "新币低liq(<5万)切片 已淘汰"


def _fmt_pct(value: float) -> str:
    text = f"{abs(value):.2f}"
    if value < 0:
        return f"−{text}%"
    if value > 0:
        return f"+{text}%"
    return "0.00%"


def _search_roots() -> list[Path]:
    package_root = Path(__file__).resolve().parents[1]
    repo = package_root.parent if package_root.name == "crawler" else package_root
    roots = [repo, repo / "crawler", package_root]
    try:
        roots.append(db_root())
    except Exception:
        log.warning("random entry csv root unavailable")
    roots.append(Path.cwd())
    return roots


def random_entry_csv_path() -> Path | None:
    """First existing export. ``RANDOM_ENTRY_CSV`` wins, even if missing."""
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


def _parse_pct(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().replace("%", "").replace("−", "-").replace("–", "-").replace(",", "")
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _header_key(name: str) -> str:
    return name.strip().casefold().replace(" ", "")


def _pick(row: dict[str, str | None], *needles: str) -> float | None:
    for key, value in row.items():
        folded = _header_key(key)
        if any(needle in folded for needle in needles):
            parsed = _parse_pct(value)
            if parsed is not None:
                return parsed
    return None


def _from_csv(path: Path) -> tuple[float, float, float] | None:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error):
        log.warning("random entry csv unreadable")
        return None
    chosen: dict[str, str | None] | None = None
    for row in rows:
        blob = " ".join(str(value or "") for value in row.values()).casefold()
        if "tp10" in blob and ("sl5" in blob or "sl_5" in blob) and "h60" in blob:
            chosen = row
            break
    if chosen is None and len(rows) == 1:
        chosen = rows[0]
    if chosen is None:
        return None
    pre = _pick(chosen, "扣成本前", "pre_cost", "before_cost", "precost")
    cost = _pick(chosen, "扣1.2", "cost_1.2", "cost_120", "cost120")
    tier = _pick(chosen, "分档", "tier_cost", "tiercost")
    if pre is None or cost is None or tier is None:
        return None
    return pre, cost, tier


def random_entry_percents() -> tuple[float, float, float]:
    """Pre-cost, 1.2% cost, and tiered cost. Packaged when the CSV is absent."""
    path = random_entry_csv_path()
    if path is not None and path.is_file():
        loaded = _from_csv(path)
        if loaded is not None:
            return loaded
    return PACKAGED_PRE_COST, PACKAGED_COST_120, PACKAGED_TIER_COST


def random_entry_lines() -> tuple[str, str]:
    pre, cost, tier = random_entry_percents()
    return (
        (
            "比随机好多少 · TP10/SL5/h60 · "
            f"扣成本前 {_fmt_pct(pre)} · 扣1.2% {_fmt_pct(cost)} · "
            f"分档成本 {_fmt_pct(tier)} · {LOW_LIQ_SLICE}"
        ),
        f"来源 exports/{CSV_NAME}",
    )
