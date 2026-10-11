#!/usr/bin/env python3
"""CEX scope vs onchain prior comparison (read-only, needs >=2 snapshots).

  cd crawler
  .venv/bin/python scripts/cex_scope_compare.py

Reads (ro): prior.db.cex_scope_snapshots + baseline_params.json#self_db
Writes: exports/reports/cex_scope_compare_YYYYMMDD.md
Never touches business tables; no weights/gates.
Fewer than --min-rows snapshots exits 0 and logs "data accruing" (no report).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger("cex_scope_compare")
DB_PRIOR = str(_ROOT / "data" / "prior" / "db" / "prior.db")
BASELINE = _ROOT / "data" / "prior" / "exports" / "baseline_params.json"
OUT = _ROOT / "exports" / "reports"


def _na(value: object) -> str:
    if value is None:
        return "N/A"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CEX scope vs on-chain prior comparison (observe-only)"
    )
    parser.add_argument("--db-prior", default=DB_PRIOR)
    parser.add_argument("--baseline", default=str(BASELINE))
    parser.add_argument("--min-rows", type=int, default=2)
    parser.add_argument(
        "--out",
        default=str(OUT),
        help="directory for cex_scope_compare_YYYYMMDD.md",
    )
    args = parser.parse_args(argv)
    setup_logging()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.db_prior}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    rows = conn.execute(
        "SELECT * FROM cex_scope_snapshots ORDER BY snap_date ASC"
    ).fetchall()
    conn.close()

    if len(rows) < args.min_rows:
        log.info(
            "cex_scope_compare skip: rows=%s < min_rows=%s (data accruing)",
            len(rows), args.min_rows,
        )
        return 0

    baseline: dict = {}
    baseline_path = Path(args.baseline)
    if baseline_path.exists():
        try:
            with baseline_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                baseline = loaded
            else:
                log.warning("baseline is not an object; survive rates N/A")
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("baseline unreadable (%s); survive rates N/A", exc)
    self_db = baseline.get("self_db") or {}
    if not isinstance(self_db, dict):
        self_db = {}
    survive_1h = self_db.get("survive_1h_pct")
    survive_4h = self_db.get("survive_4h_pct")

    latest = rows[-1]
    seq = [
        {
            "date": r["snap_date"],
            "n_new_24h": r["n_new_24h"],
            "still_visible": r["n_still_visible"],
            "med_delta24h": r["med_delta24h_new"],
        }
        for r in rows
    ]
    med_new = latest["med_delta24h_new"]
    n_total = latest["n_total"] or 0
    still_rate = (
        latest["n_still_visible"] / n_total
        if n_total else None
    )

    out_path = out_dir / f"cex_scope_compare_{datetime.now():%Y%m%d}.md"
    with out_path.open("w", encoding="utf-8") as f:
        f.write("# CEX 视野 vs 链上先验 对照（只观察）\n\n")
        f.write(
            f"快照行数: {len(rows)}（{rows[0]['snap_date']} → {latest['snap_date']}）\n\n"
        )
        f.write("| 日期 | 新进 24h | 仍在视野 | 新进者 Δ24h 中位 |\n")
        f.write("|---|---:|---:|---:|\n")
        for s in seq:
            f.write(
                f"| {s['date']} | {s['n_new_24h']} | {s['still_visible']} | "
                f"{s['med_delta24h']} |\n"
            )
        f.write("\n## 对照\n\n")
        f.write("| 维度 | CEX 视野 | 链上先验(self_survival) |\n|---|---|---|\n")
        f.write(
            f"| 新币短期表现 | 新进者 Δ24h 中位 = {_na(med_new)} | — |\n"
        )
        f.write(f"| 存活/留存 | 视野留存 ≈ {_na(still_rate)} | — |\n")
        f.write(f"| 链上 1h 存活 | — | {_na(survive_1h)} |\n")
        f.write(f"| 链上 4h 存活 | — | {_na(survive_4h)} |\n")
        f.write(
            "\n> 只观察不调权；CEX 视野抽样偏主流/高流动性，与 memecoin prior 不同总体；"
            "结论待 ≥5 行快照后再下。\n"
        )
    log.info("wrote %s (rows=%s)", out_path, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
