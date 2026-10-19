"""Append-only S2C history and the empty false-positive log.

Both files live under ``exports/`` (not the business DBs). Re-running the
same ``(period_end, layer)`` overwrites that key so one week is not counted
twice toward the two-week red rule. Other weeks are kept.

False-positive log (R1): header only. A later review appends a row when a
cell that was yellow reverses or disappears the next week. This module does
not invent those rows.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

HISTORY_COLUMNS = (
    "period_end",
    "layer",
    "candidate",
    "verdict",
    "direction",
    "n",
    "note",
)

FP_LOG_COLUMNS = (
    "period_end",
    "layer",
    "axis",
    "bucket",
    "n",
    "signal",
    "observed",
    "falsified",
    "note",
)

# 假阳性口径（空表）。全局一张，不按层拆文件。
FP_LOG_DEFINITION = (
    "假阳性：某格曾标黄且随后一周同格反向或信号消失。"
    "R1 只留空表与口径，不自动填行。"
)


def default_history_path(root: str | Path) -> Path:
    return Path(root) / "exports" / "structure_history.csv"


def default_fp_log_path(root: str | Path) -> Path:
    return Path(root) / "exports" / "structure_fp_log.csv"


def load_history(path: str | Path) -> list[dict[str, str]]:
    """Read the append table. Missing file → empty list."""
    target = Path(path)
    if not target.is_file():
        return []
    rows: list[dict[str, str]] = []
    with target.open("r", encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            rows.append({column: (raw.get(column) or "").strip() for column in HISTORY_COLUMNS})
    return rows


def upsert_layer_rows(path: str | Path, new_rows: list[dict[str, Any]]) -> None:
    """Keep every other week; replace rows with the same period_end and layer."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = load_history(target)
    keys = {
        ((row.get("period_end") or ""), (row.get("layer") or ""))
        for row in new_rows
    }
    kept = [
        row
        for row in rows
        if (row.get("period_end") or "", row.get("layer") or "") not in keys
    ]
    for row in new_rows:
        kept.append(
            {
                column: "" if row.get(column) is None else str(row.get(column))
                for column in HISTORY_COLUMNS
            }
        )
    kept.sort(key=lambda row: (row["period_end"], row["layer"]))
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(HISTORY_COLUMNS))
        writer.writeheader()
        for row in kept:
            writer.writerow(row)


def ensure_fp_log(path: str | Path) -> Path:
    """Create the global false-positive scaffold if it is missing. Never appends."""
    target = Path(path)
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(FP_LOG_COLUMNS))
    return target


def fp_log_row_count(path: str | Path) -> Optional[int]:
    """Data rows under the header. Missing file → None."""
    target = Path(path)
    if not target.is_file():
        return None
    with target.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return len(rows)
