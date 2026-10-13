"""Factor IC history (CSV in exports/reports) — multi-period IC stability.

Review still writes nothing outside exports/reports. The CSV is append-only
with upsert key (period_end | factor): re-running the same period overwrites
that row instead of duplicating.

  cd /workspace/dexscreener-crawler
  .venv/bin/python -m review.run --period 7d --report --factor-layers --ic-history exports/reports/factor_ic_history.csv
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any, Optional

_HDR = ["period_end", "factor", "ic", "monotonic", "n"]


def load_history(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "period_end": (r.get("period_end") or "").strip(),
                    "factor": (r.get("factor") or "").strip(),
                    "ic": _f(r.get("ic")),
                    "monotonic": (r.get("monotonic") or "").strip(),
                    "n": _i(r.get("n")),
                }
            )
    return rows


def _f(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _i(v: Optional[str]) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def upsert_row(
    path: str | Path,
    period_end: str,
    factor: str,
    ic: Optional[float],
    monotonic: str,
    n: int,
) -> None:
    """按 (period_end, factor) upsert 一行；文件不存在则建表头。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = load_history(path)
    rows = [r for r in rows if not (r["period_end"] == period_end and r["factor"] == factor)]
    rows.append(
        {
            "period_end": period_end,
            "factor": factor,
            "ic": ic,
            "monotonic": monotonic,
            "n": n,
        }
    )
    rows.sort(key=lambda r: (r["period_end"], r["factor"]))
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_HDR)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "period_end": r["period_end"],
                    "factor": r["factor"],
                    "ic": "" if r["ic"] is None else f"{r['ic']:.6f}",
                    "monotonic": r["monotonic"],
                    "n": "" if r["n"] is None else r["n"],
                }
            )


def ic_summary(rows: list[dict[str, Any]], factor: str) -> Optional[dict[str, Any]]:
    """某因子历史 IC 汇总：n_periods / ic_mean / ic_std / IR / 稳定性标注。"""
    ics = [r["ic"] for r in rows if r["factor"] == factor and r["ic"] is not None]
    if len(ics) < 2:
        return None
    mean = statistics.fmean(ics)
    std = statistics.pstdev(ics) if len(ics) > 1 else 0.0
    ir = (mean / std) if std > 0 else None
    # 稳定性：|IR|>=0.5 且各期 IC 与 mean 同号（方向一致）→ 稳定。
    # 变号序列即使 |IR| 刚过 0.5 仍记为噪音（如 [0.3, -0.2, 0.4]）。
    if mean > 0:
        same_dir = all(ic >= 0 for ic in ics)
    elif mean < 0:
        same_dir = all(ic <= 0 for ic in ics)
    else:
        same_dir = False
    if ir is not None and abs(ir) >= 0.5 and same_dir:
        stable = "稳定" if mean > 0 else "稳定(负向)"
    else:
        stable = "不稳定/噪音"
    return {
        "n_periods": len(ics),
        "ic_mean": mean,
        "ic_std": std,
        "ir": ir,
        "stability": stable,
    }


def summarize_all(
    rows: list[dict[str, Any]], factors: list[str]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in factors:
        s = ic_summary(rows, f)
        if s is not None:
            out[f] = s
    return out
