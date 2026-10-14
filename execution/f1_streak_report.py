"""Read-only F-1 streak report.

    python -m execution.f1_streak_report --ledger execution_ledger.db
    python -m execution.f1_streak_report --ledger execution_ledger.db --paper paper_trading.db

Prints, per close sequence: longest loss streak, sample count, and
P(this close loses | the previous two closes lost). Ledger rows come from
``streak_events``. ``--paper`` replays closed ``positions`` the same way and
does not write either database.
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

from execution.config import f1_circuit_enabled  # noqa: E402
from execution.shadow_c3 import parse_meta  # noqa: E402
from execution.streak import circuit_rejects_open, summarize_groups  # noqa: E402


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _pnl(value: Any) -> float:
    try:
        return float(value if value is not None else 0.0)
    except (TypeError, ValueError):
        return 0.0


def load_ledger_groups(path: Path) -> dict[str, list[float]] | None:
    """None when the file is absent. Empty dict when the table is absent."""
    if not path.is_file():
        return None
    conn = _connect_ro(path)
    try:
        if "streak_events" not in _tables(conn):
            return {}
        rows = conn.execute(
            "SELECT strategy_id, pnl_usd FROM streak_events ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    groups: dict[str, list[float]] = {}
    for row in rows:
        sid = str(row["strategy_id"] or "unknown")
        groups.setdefault(sid, []).append(_pnl(row["pnl_usd"]))
    return groups


def load_paper_groups(path: Path) -> dict[str, list[float]] | None:
    """Closed paper positions, ordered by close time. None when the file is absent."""
    if not path.is_file():
        return None
    conn = _connect_ro(path)
    try:
        if "positions" not in _tables(conn):
            return {}
        rows = conn.execute(
            """
            SELECT id, pnl_usd, meta_json FROM positions
            WHERE status = 'closed' AND closed_at IS NOT NULL AND closed_at != ''
            ORDER BY closed_at ASC, rowid ASC
            """
        ).fetchall()
    finally:
        conn.close()
    groups: dict[str, list[float]] = {}
    for row in rows:
        meta = parse_meta(row["meta_json"])
        sid = str(meta.get("strategy_id") or "").strip() or "unknown"
        groups.setdefault(sid, []).append(_pnl(row["pnl_usd"]))
    return groups


def _section(groups: dict[str, list[float]] | None, *, missing: bool) -> dict[str, Any]:
    if missing or groups is None:
        summary = summarize_groups({})
        summary["missing"] = True
        return summary
    summary = summarize_groups(groups)
    summary["missing"] = False
    return summary


def build_report(
    *,
    ledger: Path | None,
    paper: Path | None,
) -> dict[str, Any]:
    ledger_groups = None if ledger is None else load_ledger_groups(ledger)
    paper_groups = None if paper is None else load_paper_groups(paper)
    return {
        "f1_circuit_env": f1_circuit_enabled(),
        "f1_circuit_wired": circuit_rejects_open("buy", 0),
        "rejects_orders": False,
        "ledger": _section(ledger_groups, missing=ledger_groups is None),
        "paper_crosscheck": (
            None
            if paper is None
            else _section(paper_groups, missing=paper_groups is None)
        ),
    }


def _format_rate(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate:.4f}"


def _format_section(title: str, section: dict[str, Any]) -> list[str]:
    lines = [
        f"{title}: samples={section['samples']} "
        f"longest_loss_streak={section['longest_loss_streak']} "
        f"cond_n={section['cond_n']} "
        f"P(loss | prior two losses)={_format_rate(section['p_third_loss_given_streak_ge2'])}"
    ]
    if section.get("missing"):
        lines.append(f"{title}: database missing")
    for row in section["by_strategy"]:
        lines.append(
            f"  {row['strategy_id']}: n={row['n']} max_streak={row['max_streak']} "
            f"current_streak={row['current_streak']} cond_n={row['cond_n']} "
            f"P={_format_rate(row['p_third_loss_given_streak_ge2'])}"
        )
    return lines


def format_report(report: dict[str, Any]) -> str:
    lines = [
        "F-1 streak report (record only; order path does not fuse)",
        f"EXECUTION_F1_CIRCUIT env_on={report['f1_circuit_env']} "
        f"wired={report['f1_circuit_wired']} rejects_orders={report['rejects_orders']}",
    ]
    lines.extend(_format_section("ledger", report["ledger"]))
    paper = report.get("paper_crosscheck")
    if paper is not None:
        lines.extend(_format_section("paper_crosscheck", paper))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only F-1 loss-streak report")
    parser.add_argument("--ledger", default=None, help="execution_ledger.db (read-only)")
    parser.add_argument(
        "--paper",
        default=None,
        help="paper_trading.db closed positions, cross-check only",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    ledger = Path(args.ledger).expanduser() if args.ledger else None
    paper = Path(args.paper).expanduser() if args.paper else None
    if ledger is not None and not ledger.is_absolute():
        ledger = (Path.cwd() / ledger).resolve()
    if paper is not None and not paper.is_absolute():
        paper = (Path.cwd() / paper).resolve()
    try:
        report = build_report(ledger=ledger, paper=paper)
    except sqlite3.Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
    else:
        print(format_report(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
