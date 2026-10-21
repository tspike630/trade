#!/usr/bin/env python3
"""Append one row to exports/reports/supply_gaps.csv under the shared flock.

Usage:
  python crawler/scripts/supply_gaps_append.py --component execution_main_loop \
      --start-at 2026-10-20T18:03:17Z [--end-at ...] --reason db_repair --source execution
start_hkt/end_hkt are derived (+08:00). Lock: <csv>.lock, fcntl LOCK_EX (same as
heartbeat_gap_watch.py), so concurrent writers never interleave or clobber.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import heartbeat_gap_watch as hgw  # noqa: E402


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", default=str(root / "exports" / "reports" / "supply_gaps.csv"))
    p.add_argument("--component", required=True)
    p.add_argument("--start-at", required=True, help="ISO 8601, e.g. 2026-10-20T18:03:17Z")
    p.add_argument("--end-at", default="")
    p.add_argument("--reason", required=True)
    p.add_argument("--source", required=True)
    a = p.parse_args(argv)
    st = _ts(a.start_at)
    en = _ts(a.end_at) if a.end_at else None
    fields = [a.component, hgw.fmt_utc(st), hgw.fmt_utc(en) if en else "", hgw.fmt_hkt(st),
              hgw.fmt_hkt(en) if en else "", a.reason, a.source]
    csv = Path(a.csv)
    with hgw.csv_lock(csv):
        hgw.append_row_locked(csv, fields)
    print("APPENDED " + ",".join(fields))
    return 0


if __name__ == "__main__":
    sys.exit(main())
