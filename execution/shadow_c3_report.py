"""Read-only C3/F-3 giveback summary, grouped by actual exit reason.

    python -m execution.shadow_c3_report --ledger execution_ledger.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from execution.shadow_c3 import summarize_giveback  # noqa: E402


def load_rows(path: Path) -> list[dict[str, Any]] | None:
    """None when the file is absent."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "shadow_c3" not in names:
            return []
        rows = conn.execute(
            """
            SELECT actual_reason, giveback_from_peak_pct, peak_pnl_pct, actual_pnl_usd
            FROM shadow_c3
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def format_report(buckets: list[dict[str, Any]], *, missing: bool) -> str:
    lines = ["C3/F-3 shadow giveback (triggers unchanged)"]
    if missing:
        lines.append("ledger: database missing")
    if not buckets:
        lines.append("no shadow rows")
    for row in buckets:
        lines.append(
            f"  {row['actual_reason']}: n={row['n']} "
            f"mean_giveback_from_peak_pct={row['mean_giveback_from_peak_pct']:.4f} "
            f"mean_peak_pnl_pct={row['mean_peak_pnl_pct']:.4f} "
            f"mean_actual_pnl_usd={row['mean_actual_pnl_usd']:.4f}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only C3 giveback report")
    parser.add_argument("--ledger", required=True, help="execution_ledger.db (read-only)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.ledger).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    try:
        rows = load_rows(path)
    except sqlite3.Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    missing = rows is None
    buckets = summarize_giveback(rows or [])
    if args.json:
        print(
            json.dumps(
                {"missing": missing, "by_reason": buckets},
                ensure_ascii=False,
            )
        )
    else:
        print(format_report(buckets, missing=missing), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
