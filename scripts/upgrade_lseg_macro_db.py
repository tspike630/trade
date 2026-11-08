"""Upgrade data/lseg/lseg_macro.db with hidden microstructure & SmartEconomics fields from local CSVs.

Extracts:
1. ``macro_events``: ``min_forecast`` (Min), ``max_forecast`` (Max), ``economist_count`` (Count)
   from ``data/lseg/lseg_economic_events_merged.csv``.
2. ``cross_asset_bars``: ``bid``, ``ask``, ``spread_bps``, ``bid_nummov``, ``ask_nummov``,
   ``quote_imbalance``, ``vwap``, ``open_interest``, ``block_volume`` from
   ``data/lseg/lseg_all_quant_data.csv`` and ``data/lseg/lseg_carry_and_credit.csv``.
"""

from __future__ import annotations

import csv
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LSEG_DIR = REPO_ROOT / "data" / "lseg"
DB_PATH = LSEG_DIR / "lseg_macro.db"


def _f(val: str | None) -> float | None:
    if val is None:
        return None
    text = str(val).strip().replace(",", "")
    if not text:
        return None
    try:
        num = float(text)
    except ValueError:
        return None
    return num if math.isfinite(num) else None


def _ensure_col(conn: sqlite3.Connection, table: str, col: str, col_type: str) -> None:
    have = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def _parse_bar_utc_ms(raw: str | None) -> int | None:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def upgrade_macro_events(conn: sqlite3.Connection, csv_path: Path) -> int:
    for col, ctype in (
        ("min_forecast", "REAL"),
        ("max_forecast", "REAL"),
        ("economist_count", "INTEGER"),
    ):
        _ensure_col(conn, "macro_events", col, ctype)
    if not csv_path.is_file():
        return 0
    updates: list[tuple[float | None, float | None, int | None, str, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            min_v = _f(row.get("Min"))
            max_v = _f(row.get("Max"))
            cnt_v = _f(row.get("Count"))
            if min_v is None and max_v is None and cnt_v is None:
                continue
            ric = str(row.get("RIC") or "").strip()
            local_time = str(row.get("Local Time") or "").strip()
            if not ric or not local_time:
                continue
            updates.append(
                (
                    min_v,
                    max_v,
                    int(cnt_v) if cnt_v is not None else None,
                    ric,
                    local_time,
                )
            )
    if updates:
        conn.executemany(
            """
            UPDATE macro_events
            SET min_forecast = ?, max_forecast = ?, economist_count = ?
            WHERE ric = ? AND local_time_hkt = ?
            """,
            updates,
        )
        conn.commit()
    return len(updates)


def upgrade_cross_asset_bars(conn: sqlite3.Connection, csv_paths: list[Path]) -> int:
    for col in (
        "bid",
        "ask",
        "spread_bps",
        "bid_nummov",
        "ask_nummov",
        "quote_imbalance",
        "vwap",
        "open_interest",
        "block_volume",
    ):
        _ensure_col(conn, "cross_asset_bars", col, "REAL")

    batch: list[tuple[float | None, ...]] = []
    updated = 0
    for path in csv_paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ric = str(row.get("ric") or "").strip()
                interval = str(row.get("interval") or "").strip()
                utc_ms = _parse_bar_utc_ms(row.get("datetime"))
                if not ric or not interval or utc_ms is None:
                    continue
                bid = _f(row.get("BID"))
                ask = _f(row.get("ASK"))
                mid = _f(row.get("MID_PRICE"))
                if mid is None and bid is not None and ask is not None and ask >= bid > 0:
                    mid = 0.5 * (bid + ask)
                spread_bps = (
                    ((ask - bid) / mid) * 10000.0
                    if (bid is not None and ask is not None and mid is not None and mid > 0 and ask >= bid)
                    else None
                )
                bid_mv = _f(row.get("BID_NUMMOV"))
                ask_mv = _f(row.get("ASK_NUMMOV"))
                q_imb = (
                    (bid_mv - ask_mv) / (bid_mv + ask_mv)
                    if (bid_mv is not None and ask_mv is not None and (bid_mv + ask_mv) > 0)
                    else None
                )
                vwap = _f(row.get("VWAP"))
                oi = _f(row.get("OPINT_1")) or _f(row.get("TOTCNTROI"))
                blk = _f(row.get("BLKVOLUM"))
                if all(v is None for v in (bid, ask, spread_bps, bid_mv, ask_mv, q_imb, vwap, oi, blk)):
                    continue
                batch.append(
                    (
                        bid,
                        ask,
                        spread_bps,
                        bid_mv,
                        ask_mv,
                        q_imb,
                        vwap,
                        oi,
                        blk,
                        ric,
                        interval,
                        utc_ms,
                    )
                )
                if len(batch) >= 5000:
                    conn.executemany(
                        """
                        UPDATE cross_asset_bars
                        SET bid = ?, ask = ?, spread_bps = ?, bid_nummov = ?, ask_nummov = ?,
                            quote_imbalance = ?, vwap = ?, open_interest = ?, block_volume = ?
                        WHERE ric = ? AND interval = ? AND utc_ms = ?
                        """,
                        batch,
                    )
                    conn.commit()
                    updated += len(batch)
                    batch.clear()
    if batch:
        conn.executemany(
            """
            UPDATE cross_asset_bars
            SET bid = ?, ask = ?, spread_bps = ?, bid_nummov = ?, ask_nummov = ?,
                quote_imbalance = ?, vwap = ?, open_interest = ?, block_volume = ?
            WHERE ric = ? AND interval = ? AND utc_ms = ?
            """,
            batch,
        )
        conn.commit()
        updated += len(batch)
    return updated


def main() -> int:
    if not DB_PATH.is_file():
        print(f"lseg_macro.db not found at {DB_PATH}")
        return 1
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        ev_n = upgrade_macro_events(conn, LSEG_DIR / "lseg_economic_events_merged.csv")
        bar_n = upgrade_cross_asset_bars(
            conn,
            [
                LSEG_DIR / "lseg_all_quant_data.csv",
                LSEG_DIR / "lseg_carry_and_credit.csv",
            ],
        )
        print(f"Upgraded lseg_macro.db: macro_events_bounds={ev_n}, cross_asset_microstructure={bar_n}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
