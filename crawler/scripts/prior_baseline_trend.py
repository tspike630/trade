#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_baseline_trend.py — baseline 滚动历史（PRIOR 旁路，只写 prior.db）

用法:
  python3 scripts/prior_baseline_trend.py              # 从 baseline_params.json 追加一行
  python3 scripts/prior_baseline_trend.py --from-self-survival
  python3 scripts/prior_baseline_trend.py --list [N]   # 最近 N 行（默认 20）
  python3 scripts/prior_baseline_trend.py --since DAYS # 近 DAYS 天变化摘要 + 稳定检查

目的: 支持「稳定两周才进风控」——积累 survive_* / 三元组证据，不改权重、不进交易。
原则: 只追加 baseline_history；不覆盖历史；不写业务四库；不改 INDICATORS_WEIGHT_*。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PRIOR_BASE = os.path.join(ROOT, "data", "prior")
PRIOR_DB = os.path.join(PRIOR_BASE, "db", "prior.db")
BASELINE_JSON = os.path.join(PRIOR_BASE, "exports", "baseline_params.json")
SCHEMA_SQL = os.path.join(ROOT, "models", "prior_tables.sql")

HK = timezone(timedelta(hours=8))

# Stability gate for 「稳定两周」evidence (observational only; not wired to risk)
STABLE_WINDOW_DAYS = 14
# Absolute delta threshold on survive_4h over the window (fraction points)
STABLE_SURVIVE_4H_DELTA = 0.05


def now_iso() -> str:
    return datetime.now(HK).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def ensure_schema(conn: sqlite3.Connection) -> None:
    if os.path.isfile(SCHEMA_SQL):
        conn.executescript(open(SCHEMA_SQL, encoding="utf-8").read())
        conn.commit()
    else:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS baseline_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              recorded_at TEXT NOT NULL,
              source TEXT NOT NULL,
              graduation_rate REAL,
              same_day_death_rate REAL,
              solrpds_remove_gt_add REAL,
              survive_1h REAL,
              survive_4h REAL,
              survive_24h REAL,
              survive_7d REAL,
              peak_p50_min REAL,
              crawler_window_hours REAL,
              n_tokens INTEGER,
              params_json TEXT,
              note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bh_recorded ON baseline_history(recorded_at);
            """
        )
        conn.commit()


def load_baseline(path: Optional[str] = None) -> dict[str, Any]:
    path = path or BASELINE_JSON
    if not os.path.isfile(path):
        print(f"[FAIL] baseline_params.json not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_row(params: dict[str, Any], source: str, note: Optional[str] = None) -> dict[str, Any]:
    """Map baseline_params.json → baseline_history columns (append-only snapshot)."""
    trio = params.get("weekly_baseline_trio") or {}
    redpump = params.get("redpump") or {}
    cg = params.get("coingecko") or {}
    sol = params.get("solrpds") or {}
    gmgn = params.get("gmgn") or {}
    self_db = params.get("self_db") or {}
    survive = self_db.get("survive") or {}

    # Prefer self_db survive; fall back to gmgn if self_db missing
    if not survive and gmgn.get("survive"):
        survive = gmgn["survive"]

    graduation = trio.get("graduation_rate")
    if graduation is None:
        graduation = redpump.get("graduation_rate")

    same_day = trio.get("same_day_death_rate")
    if same_day is None:
        same_day = cg.get("same_day_death_rate")

    remove_gt = trio.get("solrpds_remove_gt_add_rate")
    if remove_gt is None:
        remove_gt = sol.get("remove_gt_add_rate")

    peak_p50 = gmgn.get("median_peak_min")
    # Prefer self_db n / window when source is self_survival
    n_tokens = self_db.get("n")
    if n_tokens is None:
        n_tokens = gmgn.get("n") or gmgn.get("acceptance_unique_tokens")
    window_h = self_db.get("crawler_window_h")

    if note is None:
        parts = []
        meta = params.get("meta") or {}
        if meta.get("date"):
            parts.append(f"meta.date={meta['date']}")
        if self_db.get("sparse"):
            parts.append("self_db.sparse=true")
        if self_db.get("warnings"):
            parts.append(f"warnings={len(self_db['warnings'])}")
        note = "; ".join(parts) if parts else None

    return {
        "recorded_at": now_iso(),
        "source": source,
        "graduation_rate": graduation,
        "same_day_death_rate": same_day,
        "solrpds_remove_gt_add": remove_gt,
        "survive_1h": survive.get("1h"),
        "survive_4h": survive.get("4h"),
        "survive_24h": survive.get("24h"),
        "survive_7d": survive.get("168h"),
        "peak_p50_min": peak_p50,
        "crawler_window_hours": window_h,
        "n_tokens": n_tokens,
        "params_json": json.dumps(params, ensure_ascii=False),
        "note": note,
    }


def append_row(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO baseline_history (
          recorded_at, source,
          graduation_rate, same_day_death_rate, solrpds_remove_gt_add,
          survive_1h, survive_4h, survive_24h, survive_7d,
          peak_p50_min, crawler_window_hours, n_tokens,
          params_json, note
        ) VALUES (
          :recorded_at, :source,
          :graduation_rate, :same_day_death_rate, :solrpds_remove_gt_add,
          :survive_1h, :survive_4h, :survive_24h, :survive_7d,
          :peak_p50_min, :crawler_window_hours, :n_tokens,
          :params_json, :note
        )
        """,
        row,
    )
    conn.commit()
    return int(cur.lastrowid)


def fmt_pct(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def fmt_num(x: Optional[float], digits: int = 4) -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}f}"


def cmd_append(source: str, note: Optional[str] = None) -> None:
    params = load_baseline()
    row = extract_row(params, source=source, note=note)
    conn = sqlite3.connect(PRIOR_DB)
    ensure_schema(conn)
    rid = append_row(conn, row)
    # Fetch last row for smoke display
    last = conn.execute(
        "SELECT * FROM baseline_history WHERE id=?", (rid,)
    ).fetchone()
    cols = [d[1] for d in conn.execute("PRAGMA table_info(baseline_history)")]
    conn.close()
    d = dict(zip(cols, last))
    print(f"[OK] appended baseline_history id={rid} source={source}")
    print(
        f"  recorded_at={d['recorded_at']}  "
        f"grad={fmt_num(d['graduation_rate'])}  "
        f"same_day_death={fmt_pct(d['same_day_death_rate'])}  "
        f"remove_gt_add={fmt_pct(d['solrpds_remove_gt_add'])}"
    )
    print(
        f"  survive 1h/4h/24h/7d="
        f"{fmt_pct(d['survive_1h'])}/{fmt_pct(d['survive_4h'])}/"
        f"{fmt_pct(d['survive_24h'])}/{fmt_pct(d['survive_7d'])}  "
        f"peak_p50={fmt_num(d['peak_p50_min'], 1)}m  "
        f"window={fmt_num(d['crawler_window_hours'], 2)}h  "
        f"n={d['n_tokens']}"
    )
    if d.get("note"):
        print(f"  note={d['note']}")
    # Always print a quick stability hint after append
    print_stability(PRIOR_DB)


def cmd_list(n: int) -> None:
    conn = sqlite3.connect(PRIOR_DB)
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT id, recorded_at, source,
               graduation_rate, same_day_death_rate, solrpds_remove_gt_add,
               survive_1h, survive_4h, survive_24h, survive_7d,
               peak_p50_min, crawler_window_hours, n_tokens, note
        FROM baseline_history
        ORDER BY id DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM baseline_history").fetchone()[0]
    conn.close()
    print(f"[LIST] last {len(rows)} of {total} rows (newest first)")
    print(
        "id | recorded_at | source | grad | death | rem>add | "
        "s1h | s4h | s24h | s7d | peak_p50 | window_h | n"
    )
    for r in rows:
        print(
            f"{r[0]} | {r[1]} | {r[2]} | "
            f"{fmt_num(r[3])} | {fmt_pct(r[4])} | {fmt_pct(r[5])} | "
            f"{fmt_pct(r[6])} | {fmt_pct(r[7])} | {fmt_pct(r[8])} | {fmt_pct(r[9])} | "
            f"{fmt_num(r[10], 1)} | {fmt_num(r[11], 2)} | {r[12]}"
        )


def _parse_recorded(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=HK)
        return dt
    except Exception:
        return None


def print_stability(db_path: str = PRIOR_DB, days: int = STABLE_WINDOW_DAYS) -> None:
    """Simple stability check: survive_4h delta over last `days` if enough points."""
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT id, recorded_at, survive_4h, survive_1h, n_tokens, crawler_window_hours
        FROM baseline_history
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()
    if not rows:
        print(f"[STABILITY] no rows yet — need ≥2 points spanning ~{days}d")
        return

    cutoff = datetime.now(HK) - timedelta(days=days)
    in_win = []
    for r in rows:
        dt = _parse_recorded(r[1])
        if dt is None:
            continue
        if dt.astimezone(HK) >= cutoff:
            in_win.append(r)

    # Prefer windowed points; if empty use all
    sample = in_win if len(in_win) >= 2 else rows
    s4 = [r[2] for r in sample if r[2] is not None]
    print(f"[STABILITY] window={days}d  points_in_window={len(in_win)}  "
          f"used={len(sample)}  total={len(rows)}")
    if len(s4) < 2:
        print(
            f"  survive_4h: insufficient points ({len(s4)}) — "
            f"need ≥2 snapshots over ~{days}d before「稳定两周」gate"
        )
        print("  verdict: INSUFFICIENT_DATA (observational only; RISK_USE_PRIOR stays off)")
        return

    first, last = s4[0], s4[-1]
    delta = last - first
    mn, mx = min(s4), max(s4)
    span = mx - mn
    ok = abs(delta) <= STABLE_SURVIVE_4H_DELTA and span <= STABLE_SURVIVE_4H_DELTA
    # Also require calendar span ≈ window when using all rows as fallback
    first_dt = _parse_recorded(sample[0][1])
    last_dt = _parse_recorded(sample[-1][1])
    calendar_days = None
    if first_dt and last_dt:
        calendar_days = (last_dt - first_dt).total_seconds() / 86400.0

    print(
        f"  survive_4h first→last: {fmt_pct(first)} → {fmt_pct(last)}  "
        f"Δ={delta:+.4f}  min/max span={span:.4f}  "
        f"(gate |Δ|≤{STABLE_SURVIVE_4H_DELTA} and span≤{STABLE_SURVIVE_4H_DELTA})"
    )
    if calendar_days is not None:
        print(f"  calendar span of sample: {calendar_days:.1f}d")
        if calendar_days < days * 0.8:
            print(
                f"  note: sample spans <{days}d — not yet eligible for「稳定两周」"
            )
            ok = False
    if len(in_win) < 2:
        print("  note: fewer than 2 points inside window; using all history")
        ok = False
    verdict = "STABLE" if ok else "UNSTABLE_OR_SHORT"
    print(f"  verdict: {verdict} (observational only; zero strategy/weight impact)")


def cmd_since(days: int) -> None:
    conn = sqlite3.connect(PRIOR_DB)
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT id, recorded_at, source,
               graduation_rate, same_day_death_rate, solrpds_remove_gt_add,
               survive_1h, survive_4h, survive_24h, survive_7d,
               peak_p50_min, crawler_window_hours, n_tokens
        FROM baseline_history
        ORDER BY id ASC
        """
    ).fetchall()
    conn.close()

    cutoff = datetime.now(HK) - timedelta(days=days)
    in_win = []
    for r in rows:
        dt = _parse_recorded(r[1])
        if dt is not None and dt.astimezone(HK) >= cutoff:
            in_win.append(r)

    print(f"[SINCE] last {days}d — {len(in_win)} row(s) (of {len(rows)} total)")
    if not in_win:
        print("  (no rows in window)")
        print_stability(PRIOR_DB, days=days)
        return

    first, last = in_win[0], in_win[-1]
    labels = [
        ("graduation_rate", 3),
        ("same_day_death_rate", 4),
        ("solrpds_remove_gt_add", 5),
        ("survive_1h", 6),
        ("survive_4h", 7),
        ("survive_24h", 8),
        ("survive_7d", 9),
        ("peak_p50_min", 10),
        ("crawler_window_hours", 11),
        ("n_tokens", 12),
    ]
    print(f"  first id={first[0]} @ {first[1]}  →  last id={last[0]} @ {last[1]}")
    for name, idx in labels:
        a, b = first[idx], last[idx]
        if a is None and b is None:
            delta_s = "—"
        elif a is None or b is None:
            delta_s = f"{a} → {b}"
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            delta_s = f"{a} → {b}  (Δ={b - a:+.6g})"
        else:
            delta_s = f"{a} → {b}"
        print(f"  {name}: {delta_s}")
    print_stability(PRIOR_DB, days=days)


def main() -> None:
    global PRIOR_DB, BASELINE_JSON
    ap = argparse.ArgumentParser(
        description="Append / list / summarize baseline_history (prior.db only)"
    )
    ap.add_argument(
        "--list",
        nargs="?",
        const=20,
        type=int,
        metavar="N",
        help="List last N rows (default 20)",
    )
    ap.add_argument(
        "--since",
        type=int,
        metavar="DAYS",
        help="Summarize changes over last DAYS + stability check",
    )
    ap.add_argument(
        "--from-self-survival",
        action="store_true",
        help="Tag source=self_survival (for hook after self_survival.py)",
    )
    ap.add_argument(
        "--source",
        default=None,
        help="Override source tag (default: self_survival if --from-self-survival else manual)",
    )
    ap.add_argument(
        "--note",
        default=None,
        help="Optional note column",
    )
    ap.add_argument(
        "--db",
        default=PRIOR_DB,
        help=f"prior.db path (default {PRIOR_DB})",
    )
    ap.add_argument(
        "--baseline-json",
        default=BASELINE_JSON,
        help=f"baseline_params.json path (default {BASELINE_JSON})",
    )
    args = ap.parse_args()

    # Allow CLI path overrides (module-level used by helpers)
    if args.db:
        PRIOR_DB = args.db
    if args.baseline_json:
        BASELINE_JSON = args.baseline_json

    if args.list is not None:
        cmd_list(args.list)
        return
    if args.since is not None:
        cmd_since(args.since)
        return

    source = args.source
    if source is None:
        source = "self_survival" if args.from_self_survival else "manual"
    cmd_append(source=source, note=args.note)


if __name__ == "__main__":
    main()
