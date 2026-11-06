#!/usr/bin/env python3
"""Upsert Binance futures LS/OI 5m bars into exports/crowding_futures.db.

Side-channel only: public fapi (no API key), does not touch dexscreener.db,
.env, live gates, or paper peers. Weight remains 0 until 复盘 IC + 杨令.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HK = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "exports" / "crowding_futures.db"
DEFAULT_META = Path("/tmp/crowding_seed_meta.json")


def fetch(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "grok-bot-collect/crowding-refresh"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--meta", type=Path, default=DEFAULT_META)
    ap.add_argument("--period", default="5m")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    syms = json.loads(args.meta.read_text())["sample"]
    con = sqlite3.connect(args.db)
    con.execute(
        """CREATE TABLE IF NOT EXISTS ls_ratio (
        symbol TEXT NOT NULL, ts TEXT NOT NULL, longShortRatio REAL,
        longAccount REAL, shortAccount REAL, ts_ms INTEGER NOT NULL,
        PRIMARY KEY (symbol, ts))"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS oi_hist (
        symbol TEXT NOT NULL, ts TEXT NOT NULL, sumOpenInterest REAL,
        sumOpenInterestValue REAL, ts_ms INTEGER NOT NULL,
        PRIMARY KEY (symbol, ts))"""
    )
    con.execute("CREATE TABLE IF NOT EXISTS seed_meta (key TEXT PRIMARY KEY, value TEXT)")

    ls_err = oi_err = 0
    t0 = time.time()
    for i, sym in enumerate(syms):
        try:
            for row in fetch(
                f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
                f"?symbol={sym}&period={args.period}&limit={args.limit}"
            ):
                ts_ms = int(row["timestamp"])
                con.execute(
                    "INSERT OR IGNORE INTO ls_ratio(symbol,ts,longShortRatio,longAccount,shortAccount,ts_ms)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        sym,
                        iso_utc(ts_ms),
                        float(row["longShortRatio"]),
                        float(row["longAccount"]),
                        float(row["shortAccount"]),
                        ts_ms,
                    ),
                )
        except Exception as e:  # noqa: BLE001 — side channel; count and continue
            ls_err += 1
            print(f"LS_ERR {sym} {e}")
        try:
            for row in fetch(
                f"https://fapi.binance.com/futures/data/openInterestHist"
                f"?symbol={sym}&period={args.period}&limit={args.limit}"
            ):
                ts_ms = int(row["timestamp"])
                con.execute(
                    "INSERT OR IGNORE INTO oi_hist(symbol,ts,sumOpenInterest,sumOpenInterestValue,ts_ms)"
                    " VALUES (?,?,?,?,?)",
                    (
                        sym,
                        iso_utc(ts_ms),
                        float(row["sumOpenInterest"]),
                        float(row["sumOpenInterestValue"]),
                        ts_ms,
                    ),
                )
        except Exception as e:  # noqa: BLE001
            oi_err += 1
            print(f"OI_ERR {sym} {e}")
        if (i + 1) % 10 == 0:
            con.commit()
            time.sleep(0.15)

    con.commit()
    ls_n, ls_min, ls_max = con.execute("select count(*), min(ts), max(ts) from ls_ratio").fetchone()
    oi_n, oi_min, oi_max = con.execute("select count(*), min(ts), max(ts) from oi_hist").fetchone()
    now = datetime.now(HK).strftime("%Y-%m-%d %H:%M:%S %Z")
    con.execute("INSERT OR REPLACE INTO seed_meta(key,value) VALUES ('last_refresh_hkt', ?)", (now,))
    con.execute(
        "INSERT OR REPLACE INTO seed_meta(key,value) VALUES ('last_refresh_mode', ?)",
        ("scripts/refresh_crowding_futures.py",),
    )
    con.execute(
        "INSERT OR REPLACE INTO seed_meta(key,value) VALUES ('last_refresh_errors', ?)",
        (json.dumps({"ls_err": ls_err, "oi_err": oi_err}),),
    )
    con.commit()
    con.close()
    print(
        json.dumps(
            {
                "ok": True,
                "elapsed_s": round(time.time() - t0, 1),
                "ls_err": ls_err,
                "oi_err": oi_err,
                "ls": {"n": ls_n, "min": ls_min, "max": ls_max},
                "oi": {"n": oi_n, "min": oi_min, "max": oi_max},
            },
            ensure_ascii=False,
        )
    )
    return 0 if ls_err == 0 and oi_err == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
