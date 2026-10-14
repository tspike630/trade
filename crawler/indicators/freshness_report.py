"""Read-only dump of technical freshness (供流正常 ≠ 下游能用).

  python -m indicators.freshness_report
  python -m indicators.freshness_report --db dexscreener.db --out /tmp/fresh.md

Writes exports/reports/indicators_freshness_YYYYMMDD.md.
Counts rows whose tech_updated_at age is ≤10 minutes, by tier, plus hot-tier
age p50/p90. Does not write the business database and does not fall back to
updated_at when tech_updated_at is absent.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DB_PATH
from indicators.base import parse_iso

_ROOT = Path(__file__).resolve().parents[1]
_REPORTS = _ROOT / "exports" / "reports"

# 2× the 5-minute hot interval. Downstream "usable" window, not "process is up".
HOT_TECH_FRESH_SEC = 600

_TIER_ORDER = ("hot", "warm", "cold")


def indicators_loop_pids() -> list[int]:
    """Optional hint: PIDs whose cmdline is `indicators.run --loop`. Empty if unknown."""
    root = Path("/proc")
    if not root.is_dir():
        return []
    found: list[int] = []
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        text = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if "indicators.run" in text and "--loop" in text:
            found.append(int(entry.name))
    return sorted(found)


def linear_percentile(values: list[float], pct: float) -> float | None:
    """Inclusive linear percentile. Empty input returns None."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    stamp = parse_iso(raw)
    if stamp is None:
        raise ValueError(f"unparsed --now {raw!r}")
    return stamp.astimezone(timezone.utc)


def resolve_db(path: str | None) -> Path:
    raw = Path(path) if path else Path(str(DB_PATH))
    if not raw.is_absolute():
        raw = _ROOT / raw
    return raw


def read_tech_rows(db_path: Path) -> dict[str, Any]:
    """Read tier + tech_updated_at only. Missing schema is a note, not a fallback."""
    result: dict[str, Any] = {
        "ok": False,
        "note": "",
        "rows": [],
    }
    if not db_path.is_file():
        result["note"] = "db_missing"
        return result
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        result["note"] = "db_open_failed"
        return result
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "token_indicators" not in tables:
            result["note"] = "token_indicators_missing"
            return result
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(token_indicators)")
        }
        if "tech_updated_at" not in cols:
            result["note"] = "tech_updated_at_absent"
            return result
        rows = []
        for tier, tech in conn.execute(
            "SELECT tier, tech_updated_at FROM token_indicators"
        ):
            label = str(tier).strip() if tier is not None else ""
            rows.append((label or "(blank)", tech if tech is None else str(tech)))
        result["ok"] = True
        result["note"] = "tech_updated_at"
        result["rows"] = rows
        return result
    except sqlite3.Error:
        result["note"] = "db_read_failed"
        return result
    finally:
        conn.close()


def _age_seconds(tech: str | None, now: datetime) -> float | None:
    stamp = parse_iso(tech)
    if stamp is None:
        return None
    return (now - stamp.astimezone(timezone.utc)).total_seconds()


def summarize(
    rows: list[tuple[str, str | None]],
    *,
    now: datetime,
    fresh_sec: int = HOT_TECH_FRESH_SEC,
) -> dict[str, Any]:
    """Count tech age ≤ fresh_sec by tier, and hot-tier age percentiles."""
    by_tier: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "fresh": 0, "stamped": 0, "missing": 0}
    )
    hot_ages: list[float] = []
    for tier, tech in rows:
        bucket = by_tier[tier]
        bucket["n"] += 1
        age = _age_seconds(tech, now)
        if age is None:
            bucket["missing"] += 1
            continue
        bucket["stamped"] += 1
        if 0.0 <= age <= float(fresh_sec):
            bucket["fresh"] += 1
        if tier == "hot":
            hot_ages.append(age)
    order = [name for name in _TIER_ORDER if name in by_tier]
    order.extend(sorted(name for name in by_tier if name not in _TIER_ORDER))
    return {
        "by_tier": {name: dict(by_tier[name]) for name in order},
        "hot_n_with_tech": len(hot_ages),
        "hot_age_p50_sec": linear_percentile(hot_ages, 50),
        "hot_age_p90_sec": linear_percentile(hot_ages, 90),
        "fresh_sec": fresh_sec,
    }


def _fmt_age(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}"


def _fmt_pid(pids: list[int] | None) -> str:
    if not pids:
        return "—"
    return ", ".join(str(pid) for pid in pids)


def render_markdown(
    loaded: dict[str, Any],
    summary: dict[str, Any],
    *,
    now: datetime,
    db_path: Path,
    pids: list[int] | None,
) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# Indicators freshness {now.astimezone(timezone.utc).strftime('%Y%m%d')}",
        "",
        f"generated_at: `{stamp}`",
        f"db: `{db_path}` (read-only)",
        f"tech column: `{loaded.get('note')}`",
        f"loop_pid: `{_fmt_pid(pids)}`",
        f"fresh window: tech age ≤ {summary['fresh_sec']}s (10m)",
        "",
        "供流正常 ≠ 下游能用。本页只读 `token_indicators.tech_updated_at`。",
        "进程还在、或 funding/sentiment 的 `updated_at` 很新，都不算 hot tech 新鲜。",
        "权重不改，gate 不改。",
        "",
    ]
    if not loaded.get("ok"):
        lines.extend(
            [
                "## schema",
                "",
                f"未统计。原因 `{loaded.get('note')}`。",
                "缺 `tech_updated_at` 时不改用 `updated_at` 充数。",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "## tech ≤10m by tier",
            "",
            "| tier | n | tech≤10m | tech stamp | missing tech |",
            "|------|---|----------|------------|--------------|",
        ]
    )
    by_tier = summary["by_tier"]
    if not by_tier:
        lines.append("| — | 0 | 0 | 0 | 0 |")
    for tier, counts in by_tier.items():
        lines.append(
            f"| {tier} | {counts['n']} | {counts['fresh']} | "
            f"{counts['stamped']} | {counts['missing']} |"
        )
    lines.extend(
        [
            "",
            "## hot age",
            "",
            f"- n_hot_with_tech: {summary['hot_n_with_tech']}",
            f"- age_p50_sec: {_fmt_age(summary['hot_age_p50_sec'])}",
            f"- age_p90_sec: {_fmt_age(summary['hot_age_p90_sec'])}",
            "- percentile: linear, inclusive, on hot rows with a parsed tech_updated_at",
            "  (stale ages stay in the sample so a starved tail shows up in p90)",
            "",
        ]
    )
    return "\n".join(lines)


def default_report_path(now: datetime, reports_dir: Path | None = None) -> Path:
    base = reports_dir if reports_dir is not None else _REPORTS
    day = now.astimezone(timezone.utc).strftime("%Y%m%d")
    return base / f"indicators_freshness_{day}.md"


def write_report(
    db_path: Path,
    out_path: Path,
    *,
    now: datetime | None = None,
    pids: list[int] | None = None,
    scan_pids: bool = True,
) -> Path:
    clock = now or datetime.now(timezone.utc)
    loaded = read_tech_rows(db_path)
    summary = summarize(loaded["rows"], now=clock)
    if pids is None and scan_pids:
        pids = indicators_loop_pids()
    text = render_markdown(
        loaded,
        summary,
        now=clock,
        db_path=db_path,
        pids=pids,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only indicators freshness markdown (tech_updated_at)"
    )
    parser.add_argument("--db", default=None, help=f"SQLite path (default {DB_PATH})")
    parser.add_argument(
        "--out",
        default=None,
        help="Markdown path (default exports/reports/indicators_freshness_YYYYMMDD.md)",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="UTC clock for ages and the filename date (ISO-8601)",
    )
    parser.add_argument(
        "--no-pid",
        action="store_true",
        help="Skip the optional indicators.run --loop PID hint",
    )
    args = parser.parse_args(argv)
    try:
        now = _parse_now(args.now)
    except ValueError as exc:
        parser.error(str(exc))
    db_path = resolve_db(args.db)
    out_path = Path(args.out) if args.out else default_report_path(now)
    if not out_path.is_absolute():
        out_path = _ROOT / out_path
    written = write_report(
        db_path,
        out_path,
        now=now,
        pids=[] if args.no_pid else None,
        scan_pids=not args.no_pid,
    )
    print(written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
