#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEX scope watch: daily snapshot of listings visibility (read-only ccxt.db -> prior.db).

  cd /workspace/dexscreener-crawler
  .venv/bin/python scripts/cex_scope_watch.py --once          # 单跑/日快照
  .venv/bin/python scripts/cex_scope_watch.py --loop --interval 3600  # 挂常驻（每小时一轮，日快照去重）

Reads (mode=ro): ccxt.db -> ccxt_listings, ccxt_ticks
Writes: prior.db -> cex_scope_snapshots (1 row per UTC date, idempotent)
Never touches business tables. No weights/gates/decisions.

Note: ccxt_listings.first_seen_ts / ccxt_ticks.ts are epoch **ms** (Step1 实列);
snapshot window uses now_ms - 86400000. snapshot_ts itself stays epoch seconds.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger("cex_scope_watch")

DB_CCXT = str(_ROOT / "ccxt.db")
DB_PRIOR = str(_ROOT / "data" / "prior" / "db" / "prior.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cex_scope_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date TEXT NOT NULL UNIQUE,          -- UTC YYYY-MM-DD
    snapshot_ts INTEGER NOT NULL,
    n_total INTEGER NOT NULL DEFAULT 0,
    n_new_24h INTEGER NOT NULL DEFAULT 0,
    n_still_visible INTEGER NOT NULL DEFAULT 0,
    med_delta24h_new REAL,
    note TEXT
);
"""


def _conn_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CEX scope watch snapshot")
    parser.add_argument("--once", action="store_true", help="single pass")
    parser.add_argument("--loop", action="store_true", help="run in loop")
    parser.add_argument("--interval", type=int, default=3600, help="loop interval s")
    parser.add_argument("--db-ccxt", default=DB_CCXT)
    parser.add_argument("--db-prior", default=DB_PRIOR)
    args = parser.parse_args(argv)
    setup_logging()

    def _snapshot() -> None:
        now = int(time.time())
        now_ms = now * 1000
        window_ms = 86400 * 1000
        snap_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with _conn_ro(args.db_ccxt) as cc:
            n_total = cc.execute("SELECT COUNT(*) FROM ccxt_listings").fetchone()[0]
            n_new = cc.execute(
                "SELECT COUNT(*) FROM ccxt_listings WHERE first_seen_ts >= ?",
                (now_ms - window_ms,),
            ).fetchone()[0]
            # 仍在视野：该 symbol 最新 tick 距今 < 24h（ts 为 ms）
            visible = cc.execute(
                "SELECT COUNT(DISTINCT symbol) FROM ccxt_ticks WHERE ts >= ?",
                (now_ms - window_ms,),
            ).fetchone()[0]
            # 新进入者的 Δ24h 中位
            rows = cc.execute(
                "SELECT DISTINCT t.symbol, t.change_24h_pct FROM ccxt_ticks t"
                " JOIN ccxt_listings l ON l.symbol = t.symbol AND l.exchange = t.exchange"
                " WHERE l.first_seen_ts >= ? AND t.change_24h_pct IS NOT NULL",
                (now_ms - window_ms,),
            ).fetchall()
        med = None
        if rows:
            vals = sorted(r[1] for r in rows)
            med = vals[len(vals) // 2]
        with sqlite3.connect(args.db_prior) as out:
            out.executescript(_SCHEMA)
            out.execute(
                "INSERT INTO cex_scope_snapshots"
                "(snap_date, snapshot_ts, n_total, n_new_24h, n_still_visible,"
                " med_delta24h_new, note)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(snap_date) DO UPDATE SET"
                " snapshot_ts=excluded.snapshot_ts, n_total=excluded.n_total,"
                " n_new_24h=excluded.n_new_24h, n_still_visible=excluded.n_still_visible,"
                " med_delta24h_new=excluded.med_delta24h_new, note=excluded.note",
                (snap_date, now, n_total, n_new, visible, med, "scope watch"),
            )
            out.commit()
        log.info(
            "cex_scope_snapshot date=%s n_total=%s n_new_24h=%s visible=%s med_d24h=%s",
            snap_date, n_total, n_new, visible,
            f"{med:.2f}%" if med is not None else "None",
        )

    if args.loop:
        while True:
            try:
                _snapshot()
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不杀循环
                log.warning("snapshot failed: %s", exc)
            time.sleep(args.interval)
        return 0
    _snapshot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
