#!/usr/bin/env python3
"""CEX scope watch: daily snapshot of listings visibility (read-only ccxt.db -> prior.db).

Branch B only (QUANT Step 3). Branch A (cex_chain_deepdive / cex_prior_linkage)
is suspended: Step 2 match rate is dirty (symbol collisions), so the
on-chain → CEX chain is not verifiable yet.

  cd crawler   # 本仓库指标加工层；本机目录名 dexscreener-crawler
  .venv/bin/python scripts/cex_scope_watch.py --once
  .venv/bin/python scripts/cex_scope_watch.py --loop --interval 3600

Reads (mode=ro): ccxt.db -> ccxt_listings, ccxt_ticks
Writes: prior.db -> cex_scope_snapshots (1 row per UTC date, idempotent upsert)
Never touches business tables. No weights/gates/decisions. Default off
(not started by main.py / decision engine).

ccxt.db stores ts and first_seen_ts as epoch milliseconds
(sources/ccxt/core.py). The 24h window is compared in ms.
snapshot_ts on the snapshot row is unix seconds.
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
from sources.ccxt.config import DB_PATH as DB_CCXT  # noqa: E402

log = get_logger("cex_scope_watch")

DB_PRIOR = str(_ROOT / "data" / "prior" / "db" / "prior.db")
_WINDOW_SEC = 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cex_scope_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date TEXT NOT NULL UNIQUE,          -- UTC YYYY-MM-DD
    snapshot_ts INTEGER NOT NULL,            -- unix seconds
    n_total INTEGER NOT NULL DEFAULT 0,
    n_new_24h INTEGER NOT NULL DEFAULT 0,
    n_still_visible INTEGER NOT NULL DEFAULT 0,
    med_delta24h_new REAL,
    note TEXT
);
"""


def _conn_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _median_upper(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _snapshot(db_ccxt: str, db_prior: str) -> None:
    now_s = int(time.time())
    # ccxt ticks/listings timestamps are epoch ms, not seconds.
    window_ms = now_s * 1000 - _WINDOW_SEC * 1000
    snap_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cc = _conn_ro(db_ccxt)
    try:
        n_total = cc.execute("SELECT COUNT(*) FROM ccxt_listings").fetchone()[0]
        n_new = cc.execute(
            "SELECT COUNT(*) FROM ccxt_listings WHERE first_seen_ts >= ?",
            (window_ms,),
        ).fetchone()[0]
        # 仍在视野：该 symbol 最新 tick 距今 < 24h（ts 为 epoch ms）
        visible = cc.execute(
            "SELECT COUNT(DISTINCT symbol) FROM ccxt_ticks WHERE ts >= ?",
            (window_ms,),
        ).fetchone()[0]
        # 新进入者的 Δ24h 中位（change_24h_pct 已是百分比）
        rows = cc.execute(
            "SELECT DISTINCT t.symbol, t.change_24h_pct FROM ccxt_ticks t"
            " JOIN ccxt_listings l ON l.symbol = t.symbol AND l.exchange = t.exchange"
            " WHERE l.first_seen_ts >= ? AND t.change_24h_pct IS NOT NULL",
            (window_ms,),
        ).fetchall()
    finally:
        cc.close()

    med = _median_upper([r[1] for r in rows])
    prior_path = Path(db_prior)
    prior_path.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(db_prior)
    try:
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
            (snap_date, now_s, n_total, n_new, visible, med, "scope watch"),
        )
        out.commit()
    finally:
        out.close()
    log.info(
        "cex_scope_snapshot date=%s n_total=%s n_new_24h=%s visible=%s med_d24h=%s",
        snap_date,
        n_total,
        n_new,
        visible,
        f"{med:.2f}%" if med is not None else "None",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CEX scope watch snapshot")
    parser.add_argument("--once", action="store_true", help="single pass")
    parser.add_argument("--loop", action="store_true", help="run in loop")
    parser.add_argument("--interval", type=int, default=3600, help="loop interval s")
    parser.add_argument("--db-ccxt", default=DB_CCXT)
    parser.add_argument("--db-prior", default=DB_PRIOR)
    args = parser.parse_args(argv)
    setup_logging()

    if args.loop:
        while True:
            try:
                _snapshot(args.db_ccxt, args.db_prior)
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不杀循环
                log.warning("snapshot failed: %s", exc)
            time.sleep(args.interval)
    _snapshot(args.db_ccxt, args.db_prior)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
