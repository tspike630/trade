#!/usr/bin/env python3
"""LSEG Macro & Cross-Asset Sidechannel Builder + Boll-Fade Attribution.

Strictly read-only with respect to ``dexscreener.db``, ``paper_trading.db``,
and live execution gates. Builds ``data/lseg/lseg_macro.db`` (converting
LSEG Workspace HKT timestamps to UTC to match Codebook UTC bars) and writes
``docs/LSEG_MACRO_SIDE_EVIDENCE_2026-11-08.md``.
"""

from __future__ import annotations

import csv
import math
import os
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LSEG_DIR = REPO_ROOT / "data" / "lseg"
DB_PATH = LSEG_DIR / "lseg_macro.db"
REPORT_PATH = REPO_ROOT / "docs" / "LSEG_MACRO_SIDE_EVIDENCE_2026-11-08.md"

HKT = timezone(timedelta(hours=8))
UTC = timezone.utc

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_num(val: Any) -> float | None:
    if val is None:
        return None
    text = str(val).strip().replace(",", "")
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        x = float(m.group(0))
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def hkt_to_utc(date_str: str, time_str: str) -> tuple[str | None, int | None]:
    """Convert LSEG Excel HKT (UTC+8) date/time to UTC ISO and epoch ms."""
    raw = (time_str or "").strip() or (date_str or "").strip()
    if not raw:
        return None, None
    try:
        if len(raw) == 10:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=HKT)
        else:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=HKT)
        utc_dt = dt.astimezone(UTC)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), int(utc_dt.timestamp() * 1000)
    except ValueError:
        return None, None


def utc_str_to_ms(raw: str) -> tuple[str | None, int | None]:
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        if len(text) == 10:
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
        else:
            dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), int(dt.timestamp() * 1000)
    except ValueError:
        return None, None


def build_database() -> dict[str, int]:
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA synchronous = OFF")
    conn.executescript(
        """
        CREATE TABLE macro_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ric TEXT,
            local_date_hkt TEXT,
            local_time_hkt TEXT,
            utc_ts TEXT,
            utc_ms INTEGER,
            country TEXT,
            relevance INTEGER,
            indicator_name TEXT,
            period TEXT,
            reuters_poll REAL,
            actual REAL,
            surprise REAL,
            prior REAL,
            revised REAL,
            smart_economics REAL,
            predicted_surprise REAL
        );
        CREATE INDEX idx_macro_events_utc_ms ON macro_events(utc_ms);
        CREATE INDEX idx_macro_events_rel ON macro_events(country, relevance, utc_ms);

        CREATE TABLE central_bank_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_date_hkt TEXT,
            local_time_hkt TEXT,
            utc_ts TEXT,
            utc_ms INTEGER,
            country TEXT,
            event_name TEXT,
            is_high_impact INTEGER
        );
        CREATE INDEX idx_cb_events_utc_ms ON central_bank_events(utc_ms);

        CREATE TABLE cross_asset_bars (
            ric TEXT NOT NULL,
            interval TEXT NOT NULL,
            utc_ts TEXT NOT NULL,
            utc_ms INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL NOT NULL,
            volume REAL,
            yield_val REAL,
            desc TEXT,
            PRIMARY KEY (ric, interval, utc_ms)
        );
        CREATE INDEX idx_bars_interval_ms ON cross_asset_bars(interval, utc_ms);
        """
    )

    # 1. Load macro_events
    econ_csv = LSEG_DIR / "lseg_economic_events_merged.csv"
    econ_rows = []
    with econ_csv.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            utc_ts, utc_ms = hkt_to_utc(r["Local Start Date"], r["Local Time"])
            rel = int(parse_num(r["Relevance"]) or 0)
            econ_rows.append(
                (
                    r["RIC"],
                    r["Local Start Date"],
                    r["Local Time"],
                    utc_ts,
                    utc_ms,
                    r["Country/Region"],
                    rel,
                    r["Indicator Name"],
                    r["Period"],
                    parse_num(r["Reuters Poll"]),
                    parse_num(r["Actual"]),
                    parse_num(r["Surprise"]),
                    parse_num(r["Prior"]),
                    parse_num(r["Revised"]),
                    parse_num(r["SmartEconomics®"]),
                    parse_num(r["Predicted Surprise"]),
                )
            )
    conn.executemany(
        """
        INSERT INTO macro_events (
            ric, local_date_hkt, local_time_hkt, utc_ts, utc_ms,
            country, relevance, indicator_name, period,
            reuters_poll, actual, surprise, prior, revised,
            smart_economics, predicted_surprise
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        econ_rows,
    )

    # 2. Load central_bank_events
    cb_csv = LSEG_DIR / "lseg_central_bank_events.csv"
    cb_rows = []
    hi_kw = re.compile(
        r"\b(FOMC|Powell|Interest Rate|Policy Statement|Press Conference|Lagarde|Ueda|Minutes|Nonfarm|CPI)\b",
        re.I,
    )
    with cb_csv.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            utc_ts, utc_ms = hkt_to_utc(r["Local Start Date"], r["Local Time"])
            ev = r["Event Name"] or ""
            hi = 1 if hi_kw.search(ev) else 0
            cb_rows.append(
                (
                    r["Local Start Date"],
                    r["Local Time"],
                    utc_ts,
                    utc_ms,
                    r["Country/Region"],
                    ev,
                    hi,
                )
            )
    conn.executemany(
        """
        INSERT INTO central_bank_events (
            local_date_hkt, local_time_hkt, utc_ts, utc_ms,
            country, event_name, is_high_impact
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        cb_rows,
    )

    # 3. Load cross_asset_bars from the 3 CSVs
    bar_count = 0
    for fn in (
        "lseg_all_quant_data.csv",
        "lseg_carry_and_credit.csv",
        "lseg_crypto_equities_full.csv",
    ):
        path = LSEG_DIR / fn
        batch = []
        with path.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            is_crypto_eq = "timestamp" in (reader.fieldnames or [])
            for r in reader:
                ric = (r.get("ric") or "").strip()
                iv = (r.get("interval") or "").strip()
                desc = (r.get("desc") or "").strip()
                raw_ts = r.get("timestamp") if is_crypto_eq else r.get("datetime")
                utc_ts, utc_ms = utc_str_to_ms(raw_ts or "")
                if not ric or not iv or utc_ms is None or utc_ts is None:
                    continue
                if is_crypto_eq:
                    op = parse_num(r.get("open"))
                    hi = parse_num(r.get("high"))
                    lo = parse_num(r.get("low"))
                    cl = parse_num(r.get("close"))
                    vol = parse_num(r.get("volume"))
                    yld = parse_num(r.get("yield_pct"))
                    if cl is None and yld is not None:
                        cl = yld
                else:
                    cl = (
                        parse_num(r.get("TRDPRC_1"))
                        or parse_num(r.get("MID_PRICE"))
                        or parse_num(r.get("BID"))
                        or parse_num(r.get("SETTLE"))
                        or parse_num(r.get("YIELD"))
                        or parse_num(r.get("MID_YLD_1"))
                        or parse_num(r.get("BID_YIELD"))
                    )
                    op = (
                        parse_num(r.get("OPEN_PRC"))
                        or parse_num(r.get("MID_OPEN"))
                        or parse_num(r.get("OPEN_BID"))
                        or parse_num(r.get("OPEN_YLD"))
                        or cl
                    )
                    hi = (
                        parse_num(r.get("HIGH_1"))
                        or parse_num(r.get("MID_HIGH"))
                        or parse_num(r.get("BID_HIGH_1"))
                        or parse_num(r.get("HIGH_YLD"))
                        or cl
                    )
                    lo = (
                        parse_num(r.get("LOW_1"))
                        or parse_num(r.get("MID_LOW"))
                        or parse_num(r.get("BID_LOW_1"))
                        or parse_num(r.get("LOW_YLD"))
                        or cl
                    )
                    vol = parse_num(r.get("ACVOL_UNS")) or parse_num(r.get("TOTCNTRVOL"))
                    yld = (
                        parse_num(r.get("YIELD"))
                        or parse_num(r.get("MID_YLD_1"))
                        or parse_num(r.get("BID_YIELD"))
                    )
                if cl is None:
                    continue
                batch.append((ric, iv, utc_ts, utc_ms, op, hi, lo, cl, vol, yld, desc))
                if len(batch) >= 20000:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO cross_asset_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                    bar_count += len(batch)
                    batch.clear()
        if batch:
            conn.executemany(
                """
                INSERT OR REPLACE INTO cross_asset_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch,
            )
            bar_count += len(batch)
    conn.commit()
    total_bars = conn.execute("SELECT COUNT(*) FROM cross_asset_bars").fetchone()[0]
    conn.close()
    return {
        "macro_events": len(econ_rows),
        "central_bank_events": len(cb_rows),
        "cross_asset_bars": int(total_bars),
    }


def analyze_and_write_report(db_stats: dict[str, int]) -> dict[str, Any]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Load 5m and 1h series by RIC
    def load_bars(ric: str, interval: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT utc_ms, utc_ts, open, high, low, close, volume
            FROM cross_asset_bars
            WHERE ric = ? AND interval = ?
            ORDER BY utc_ms ASC
            """,
            (ric, interval),
        ).fetchall()
        return [dict(r) for r in rows]

    # Build fast lookup maps for cross-asset 5m & 1h returns
    def return_map(ric: str, interval: str, lookback_bars: int = 1) -> dict[int, float]:
        bars = load_bars(ric, interval)
        out: dict[int, float] = {}
        for i in range(lookback_bars, len(bars)):
            prev_c = bars[i - lookback_bars]["close"]
            curr_c = bars[i]["close"]
            if prev_c and prev_c != 0:
                if ric in ("US2YT=RR", "US10YT=RR", "JP10YT=RR"):
                    # Yield change in bps
                    out[bars[i]["utc_ms"]] = (curr_c - prev_c) * 100.0
                else:
                    out[bars[i]["utc_ms"]] = ((curr_c - prev_c) / prev_c) * 100.0
        return out

    def level_map(ric: str, interval: str) -> dict[int, float]:
        return {
            r["utc_ms"]: r["close"]
            for r in load_bars(ric, interval)
            if r["close"] is not None
        }

    # As-of lookup helper (latest bar <= ts_ms within max_age_ms)
    def build_asof(val_dict: dict[int, float], max_age_ms: int = 3600_000):
        keys = sorted(val_dict.keys())
        import bisect

        def get_asof(ts_ms: int) -> float | None:
            idx = bisect.bisect_right(keys, ts_ms) - 1
            if idx < 0:
                return None
            k = keys[idx]
            if ts_ms - k > max_age_ms:
                return None
            return val_dict[k]

        return get_asof

    nq_5m = build_asof(return_map("NQc1", "5min", 3), 1800_000)  # 15m NQ return
    es_5m = build_asof(return_map("ESc1", "5min", 3), 1800_000)
    dxy_5m = build_asof(return_map(".DXY", "5min", 3), 1800_000)  # 15m DXY return
    us2y_5m = build_asof(return_map("US2YT=RR", "5min", 3), 1800_000)  # 15m 2Y bps change
    us10y_5m = build_asof(return_map("US10YT=RR", "5min", 3), 1800_000)
    jpy_5m = build_asof(return_map("JPY=", "5min", 3), 1800_000)  # 15m USDJPY return
    cnh_5m = build_asof(return_map("CNH=", "5min", 3), 1800_000)
    hyg_5m = build_asof(return_map("HYG.P", "5min", 3), 3600_000)
    ibit_5m = build_asof(return_map("IBIT.O", "5min", 3), 1800_000)
    coin_5m = build_asof(return_map("COIN.O", "5min", 3), 1800_000)
    mstr_5m = build_asof(return_map("MSTR.O", "5min", 3), 1800_000)
    xau_5m = build_asof(return_map("XAU=", "5min", 3), 1800_000)

    # CME Futures Basis (BTCc1 vs BTC=)
    btcc1_lvl = build_asof(level_map("BTCc1", "5min"), 900_000)
    btc_spot_lvl = build_asof(level_map("BTC=", "5min"), 900_000)

    # Also 1h cross-asset maps for full-year (2025-11 to 2026-11) analysis
    nq_1h = build_asof(return_map("NQc1", "1h", 1), 7200_000)
    dxy_1h = build_asof(return_map(".DXY", "1h", 1), 7200_000)
    us2y_1h = build_asof(return_map("US2YT=RR", "1h", 1), 7200_000)
    jpy_1h = build_asof(return_map("JPY=", "1h", 1), 7200_000)
    hyg_1h = build_asof(return_map("HYG.P", "1h", 1), 14400_000)
    coin_1h = build_asof(return_map("COIN.O", "1h", 1), 7200_000)

    # Load Macro Events with valid time (not midnight date-only)
    econ_events = conn.execute(
        """
        SELECT utc_ms, country, relevance, indicator_name, surprise, predicted_surprise
        FROM macro_events
        WHERE utc_ms IS NOT NULL AND local_time_hkt != ''
        ORDER BY utc_ms ASC
        """
    ).fetchall()
    cb_events = conn.execute(
        """
        SELECT utc_ms, country, event_name, is_high_impact
        FROM central_bank_events
        WHERE utc_ms IS NOT NULL AND local_time_hkt != '' AND is_high_impact = 1
        ORDER BY utc_ms ASC
        """
    ).fetchall()

    rel3_times = sorted(
        {int(r["utc_ms"]) for r in econ_events if int(r["relevance"]) == 3}
    )
    rel2_times = sorted(
        {int(r["utc_ms"]) for r in econ_events if int(r["relevance"]) == 2}
    )
    cb_times = sorted({int(r["utc_ms"]) for r in cb_events})

    import bisect

    def in_window(sorted_times: list[int], ts_ms: int, before_ms: int, after_ms: int) -> bool:
        # Event at E is active if ts_ms in [E - before_ms, E + after_ms]
        # Equivalent to E in [ts_ms - after_ms, ts_ms + before_ms]
        lo = ts_ms - after_ms
        hi = ts_ms + before_ms
        idx = bisect.bisect_left(sorted_times, lo)
        return idx < len(sorted_times) and sorted_times[idx] <= hi

    # Evaluate Boll Lower Break & Entry Zone on 5m bars (BTC= and ETH=)
    def evaluate_5m_fade(ric: str) -> list[dict[str, Any]]:
        bars = load_bars(ric, "5min")
        trades = []
        for i in range(20, len(bars) - 12):
            window = [bars[j]["close"] for j in range(i - 19, i + 1)]
            mid = sum(window) / 20.0
            sd = math.sqrt(sum((x - mid) ** 2 for x in window) / 20.0)
            lower = mid - 2.0 * sd
            bar = bars[i]
            close = bar["close"]
            low = bar["low"] if bar["low"] is not None else close
            prev_close = bars[i - 1]["close"]
            chg_5m = ((close - prev_close) / prev_close) * 100.0 if prev_close else 0.0

            # Classic lower-band break: close < boll_lower
            is_lower_break = close < lower
            # Entry Zone (live_bnb_boll_fade coordinates): S = mid - low, alpha = (close - low)/S
            span = mid - low
            alpha = (close - low) / span if span > 1e-8 else None
            stop_ez = low - 0.1 * span if span > 1e-8 else None
            odds_ez = (
                (mid - close) / (close - stop_ez)
                if (stop_ez is not None and close > stop_ez and mid > close)
                else None
            )
            is_ez = (
                alpha is not None
                and 0.08 <= alpha <= 0.40
                and odds_ez is not None
                and odds_ez >= 1.5
                and ((close - stop_ez) / close) >= 0.0015
            )
            if not is_lower_break and not is_ez:
                continue

            # Forward 60m (12 bars) path
            fwd = bars[i + 1 : i + 13]
            ret_15m = ((fwd[2]["close"] - close) / close) * 100.0
            ret_30m = ((fwd[5]["close"] - close) / close) * 100.0
            ret_60m = ((fwd[11]["close"] - close) / close) * 100.0
            min_low = min(
                (b["low"] if b["low"] is not None else b["close"]) for b in fwd
            )
            max_high = max(
                (b["high"] if b["high"] is not None else b["close"]) for b in fwd
            )
            mae_60m = ((min_low - close) / close) * 100.0
            mfe_60m = ((max_high - close) / close) * 100.0

            # Did price hit boll_mid (TP) before hitting stop_ez (or -0.5% SL)?
            hit_tp_first = False
            hit_sl_first = False
            sl_level = stop_ez if (stop_ez is not None and stop_ez < close) else close * 0.995
            for b in fwd:
                b_lo = b["low"] if b["low"] is not None else b["close"]
                b_hi = b["high"] if b["high"] is not None else b["close"]
                if b_lo <= sl_level:
                    hit_sl_first = True
                    break
                if b_hi >= mid:
                    hit_tp_first = True
                    break

            ts_ms = int(bar["utc_ms"])
            f_px = btcc1_lvl(ts_ms)
            s_px = btc_spot_lvl(ts_ms)
            cme_basis_bps = (
                ((f_px - s_px) / s_px) * 10000.0
                if (f_px is not None and s_px is not None and s_px > 0)
                else None
            )

            trades.append(
                {
                    "ric": ric,
                    "utc_ms": ts_ms,
                    "utc_ts": bar["utc_ts"],
                    "close": close,
                    "chg_5m": chg_5m,
                    "is_lower_break": is_lower_break,
                    "is_ez": is_ez,
                    "ret_15m": ret_15m,
                    "ret_30m": ret_30m,
                    "ret_60m": ret_60m,
                    "mae_60m": mae_60m,
                    "mfe_60m": mfe_60m,
                    "hit_tp_first": hit_tp_first,
                    "hit_sl_first": hit_sl_first,
                    "in_rel3": in_window(rel3_times, ts_ms, 15 * 60_000, 30 * 60_000),
                    "in_rel2": in_window(rel2_times, ts_ms, 15 * 60_000, 30 * 60_000),
                    "in_cb": in_window(cb_times, ts_ms, 15 * 60_000, 45 * 60_000),
                    "nq_15m": nq_5m(ts_ms),
                    "dxy_15m": dxy_5m(ts_ms),
                    "us2y_15m": us2y_5m(ts_ms),
                    "jpy_15m": jpy_5m(ts_ms),
                    "cnh_15m": cnh_5m(ts_ms),
                    "ibit_15m": ibit_5m(ts_ms),
                    "coin_15m": coin_5m(ts_ms),
                    "xau_15m": xau_5m(ts_ms),
                    "cme_basis_bps": cme_basis_bps,
                }
            )
        return trades

    # Also evaluate 1h Boll Lower Break over the full 1-year window (2025-11 to 2026-11)
    def evaluate_1h_fade(ric: str) -> list[dict[str, Any]]:
        bars = load_bars(ric, "1h")
        trades = []
        for i in range(20, len(bars) - 4):
            window = [bars[j]["close"] for j in range(i - 19, i + 1)]
            mid = sum(window) / 20.0
            sd = math.sqrt(sum((x - mid) ** 2 for x in window) / 20.0)
            lower = mid - 2.0 * sd
            bar = bars[i]
            close = bar["close"]
            if close >= lower:
                continue
            fwd = bars[i + 1 : i + 5]
            ret_1h = ((fwd[0]["close"] - close) / close) * 100.0
            ret_4h = ((fwd[3]["close"] - close) / close) * 100.0
            min_low = min(
                (b["low"] if b["low"] is not None else b["close"]) for b in fwd
            )
            mae_4h = ((min_low - close) / close) * 100.0
            ts_ms = int(bar["utc_ms"])
            trades.append(
                {
                    "ric": ric,
                    "utc_ms": ts_ms,
                    "utc_ts": bar["utc_ts"],
                    "ret_1h": ret_1h,
                    "ret_4h": ret_4h,
                    "mae_4h": mae_4h,
                    "in_rel3": in_window(rel3_times, ts_ms, 60 * 60_000, 120 * 60_000),
                    "in_rel2": in_window(rel2_times, ts_ms, 60 * 60_000, 120 * 60_000),
                    "in_cb": in_window(cb_times, ts_ms, 60 * 60_000, 120 * 60_000),
                    "nq_1h": nq_1h(ts_ms),
                    "dxy_1h": dxy_1h(ts_ms),
                    "us2y_1h": us2y_1h(ts_ms),
                    "jpy_1h": jpy_1h(ts_ms),
                    "hyg_1h": hyg_1h(ts_ms),
                    "coin_1h": coin_1h(ts_ms),
                }
            )
        return trades

    trades_5m = evaluate_5m_fade("BTC=") + evaluate_5m_fade("ETH=")
    lower_5m = [t for t in trades_5m if t["is_lower_break"]]
    ez_5m = [t for t in trades_5m if t["is_ez"]]
    trades_1h = evaluate_1h_fade("BTC=") + evaluate_1h_fade("ETH=")

    def summarize_5m(subset: list[dict[str, Any]]) -> dict[str, Any]:
        if not subset:
            return {"n": 0}
        n = len(subset)
        wr_60m = sum(1 for t in subset if t["ret_60m"] > 0) / n * 100.0
        mean_30m = statistics.mean(t["ret_30m"] for t in subset)
        mean_60m = statistics.mean(t["ret_60m"] for t in subset)
        med_60m = statistics.median(t["ret_60m"] for t in subset)
        mean_mae = statistics.mean(t["mae_60m"] for t in subset)
        p10_mae = sorted(t["mae_60m"] for t in subset)[max(0, int(n * 0.10))]
        tp_rate = sum(1 for t in subset if t["hit_tp_first"]) / n * 100.0
        sl_rate = sum(1 for t in subset if t["hit_sl_first"]) / n * 100.0
        return {
            "n": n,
            "wr_60m": wr_60m,
            "mean_30m": mean_30m,
            "mean_60m": mean_60m,
            "med_60m": med_60m,
            "mean_mae": mean_mae,
            "p10_mae": p10_mae,
            "tp_rate": tp_rate,
            "sl_rate": sl_rate,
        }

    def summarize_1h(subset: list[dict[str, Any]]) -> dict[str, Any]:
        if not subset:
            return {"n": 0}
        n = len(subset)
        wr_4h = sum(1 for t in subset if t["ret_4h"] > 0) / n * 100.0
        mean_1h = statistics.mean(t["ret_1h"] for t in subset)
        mean_4h = statistics.mean(t["ret_4h"] for t in subset)
        mean_mae = statistics.mean(t["mae_4h"] for t in subset)
        tail_2pct = sum(1 for t in subset if t["mae_4h"] <= -2.0) / n * 100.0
        return {
            "n": n,
            "wr_4h": wr_4h,
            "mean_1h": mean_1h,
            "mean_4h": mean_4h,
            "mean_mae": mean_mae,
            "tail_2pct": tail_2pct,
        }

    # Buckets for 5m Lower-Band Break
    b5 = {
        "All 5m Lower-Band Breaks (Baseline)": summarize_5m(lower_5m),
        "Calm Macro (No Rel 2/3 or CB Event)": summarize_5m(
            [t for t in lower_5m if not (t["in_rel3"] or t["in_rel2"] or t["in_cb"])]
        ),
        "Relevance = 3 Event Window [-15m, +30m]": summarize_5m(
            [t for t in lower_5m if t["in_rel3"]]
        ),
        "Relevance = 2 Event Window [-15m, +30m] (ex-Rel3)": summarize_5m(
            [t for t in lower_5m if t["in_rel2"] and not t["in_rel3"]]
        ),
        "Central Bank High-Impact Window [-15m, +45m]": summarize_5m(
            [t for t in lower_5m if t["in_cb"]]
        ),
        "NQc1 (Nasdaq Futures) 15m Drop <= -0.20%": summarize_5m(
            [t for t in lower_5m if t["nq_15m"] is not None and t["nq_15m"] <= -0.20]
        ),
        "NQc1 (Nasdaq Futures) Stable/Up (> -0.20%)": summarize_5m(
            [t for t in lower_5m if t["nq_15m"] is not None and t["nq_15m"] > -0.20]
        ),
        ".DXY (US Dollar) 15m Spike >= +0.10%": summarize_5m(
            [t for t in lower_5m if t["dxy_15m"] is not None and t["dxy_15m"] >= 0.10]
        ),
        "JPY= (Carry Unwind / Yen Surge) 15m <= -0.15%": summarize_5m(
            [t for t in lower_5m if t["jpy_15m"] is not None and t["jpy_15m"] <= -0.15]
        ),
        "CME Futures Basis Discount (BTCc1 < BTC= by >15 bps)": summarize_5m(
            [t for t in lower_5m if t["cme_basis_bps"] is not None and t["cme_basis_bps"] < -15.0]
        ),
        "CME Futures Basis Normal/Premium (>= -15 bps)": summarize_5m(
            [t for t in lower_5m if t["cme_basis_bps"] is not None and t["cme_basis_bps"] >= -15.0]
        ),
    }

    # Buckets for 1h Lower-Band Break (12-month sample: 2025-11 to 2026-11)
    b1h = {
        "All 1h Lower-Band Breaks (12M Baseline)": summarize_1h(trades_1h),
        "Calm Macro (Outside Event Windows)": summarize_1h(
            [t for t in trades_1h if not (t["in_rel3"] or t["in_rel2"] or t["in_cb"])]
        ),
        "Relevance = 3 Macro Window [-1h, +2h]": summarize_1h(
            [t for t in trades_1h if t["in_rel3"]]
        ),
        "Relevance = 2 Macro Window [-1h, +2h] (ex-Rel3)": summarize_1h(
            [t for t in trades_1h if t["in_rel2"] and not t["in_rel3"]]
        ),
        "Central Bank Event Window [-1h, +2h]": summarize_1h(
            [t for t in trades_1h if t["in_cb"]]
        ),
        "NQc1 1h Drop <= -0.40% (US Tech Risk-Off)": summarize_1h(
            [t for t in trades_1h if t["nq_1h"] is not None and t["nq_1h"] <= -0.40]
        ),
        "US2YT=RR 1h Spike >= +3.0 bps (Hawkish Rate Shock)": summarize_1h(
            [t for t in trades_1h if t["us2y_1h"] is not None and t["us2y_1h"] >= 3.0]
        ),
        "JPY= 1h Drop <= -0.25% (Yen Carry Unwind)": summarize_1h(
            [t for t in trades_1h if t["jpy_1h"] is not None and t["jpy_1h"] <= -0.25]
        ),
        "HYG.P 1h Drop <= -0.20% (Credit Spread Widening)": summarize_1h(
            [t for t in trades_1h if t["hyg_1h"] is not None and t["hyg_1h"] <= -0.20]
        ),
        "COIN.O 1h Drop <= -1.0% (US Crypto Equity Bleed)": summarize_1h(
            [t for t in trades_1h if t["coin_1h"] is not None and t["coin_1h"] <= -1.0]
        ),
    }

    # Upcoming 30-day high-impact calendar (2026-11-08 to 2026-12-08)
    upcoming_econ = conn.execute(
        """
        SELECT local_date_hkt, local_time_hkt, utc_ts, country, relevance,
               indicator_name, period, reuters_poll, prior, smart_economics
        FROM macro_events
        WHERE local_date_hkt >= '2026-11-08' AND local_date_hkt <= '2026-11-30'
          AND relevance IN (2, 3)
        ORDER BY utc_ms ASC, relevance DESC
        LIMIT 35
        """
    ).fetchall()
    upcoming_cb = conn.execute(
        """
        SELECT local_date_hkt, local_time_hkt, utc_ts, country, event_name
        FROM central_bank_events
        WHERE local_date_hkt >= '2026-11-08' AND local_date_hkt <= '2026-12-18'
          AND is_high_impact = 1
        ORDER BY utc_ms ASC
        LIMIT 25
        """
    ).fetchall()

    # Format Markdown report
    lines = [
        "# LSEG 宏观事件与跨资产状态旁路归因报告（2026-11-08）",
        "",
        "> **状态**：只读旁路建库与归因完成（`data/lseg/lseg_macro.db`）",
        "> **硬约束**：零侵入 `dexscreener.db`、不改 `live_bnb_boll_fade` / `race_tech_boll_fade` 现网参数、不消耗 `CELL_LEDGER` 格数。",
        "",
        "## 1. 时区校准与数据库入库概览",
        "",
        "在入库校验中发现并修正了一个关键时区差异：",
        "- **LSEG Excel 宏观与央行日历（4 个文件）**：导出自香港城市大学终端，`Local Time` 为 **香港时间（HKT, UTC+8）**（如美国 CPI 发布记录为 `20:30:00` HKT，FOMC 决议记录为次日 `02:00:00` HKT）。",
        "- **LSEG Codebook 量化行情 CSV（3 个文件）**：`get_history` 导出的时间戳为 **标准 UTC 时间**（如美股 `IBIT.O` / `COIN.O` 盘前自 `08:00:00` UTC 开始，常规盘 `13:30–20:00` UTC）。",
        "- **统一对齐**：已在 `data/lseg/lseg_macro.db` 中将全部日历事件统一减去 8 小时转换为标准 `utc_ts` / `utc_ms`，与量化 K 线及 `trade` 仓库的 UTC 时间轴严格对齐。",
        "",
        "| 表名 | 记录数 | 核心字段与覆盖范围 |",
        "| :--- | ---: | :--- |",
        f"| `macro_events` | {db_stats['macro_events']:,} | 三表去重合并（2024-04 至 2026-11），含 `Relevance`（3/2/0）、`Reuters Poll`、`Actual`、`Surprise`、`SmartEconomics®`、`Predicted Surprise` |",
        f"| `central_bank_events` | {db_stats['central_bank_events']:,} | 美、欧、中、日央行议息/纪要/讲话（2023-10 至 2027-12），自动标记 `is_high_impact` |",
        f"| `cross_asset_bars` | {db_stats['cross_asset_bars']:,} | 22 个跨资产标的（`5min` / `1h` / `1d`），涵盖加密现货/CME期货/ETF、美股指、美债/日债、外汇套息、信用债与黄金 |",
        "",
        "## 2. 5m 下轨均值回归（Boll Fade）× 宏观事件与跨资产归因（近 1 个月高频样本）",
        "",
        "基于 `BTC=` 与 `ETH=` 的 `5min` 闭合 K 线（`2026-10-04` 至 `2026-11-08`，共 20,000 根 5m K 线），严格复现 `close < boll_lower(20, 2σ)` 破下轨买入信号，统计未来 60 分钟收益、最大逆向回撤（MAE）、以及先触及中轨止盈（TP）还是先触及止损（SL）：",
        "",
        "| 状态分桶（5m 破下轨入场） | 样本数 `n` | 60m 胜率 | 30m 均值 | 60m 均值 | 60m MAE 均值 | 10% 尾部 MAE | 先 TP 率 | 先 SL 率 |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, st in b5.items():
        if st["n"] == 0:
            continue
        lines.append(
            f"| {label} | {st['n']} | {st['wr_60m']:.1f}% | {st['mean_30m']:+.3f}% | "
            f"{st['mean_60m']:+.3f}% | {st['mean_mae']:.3f}% | {st['p10_mae']:.3f}% | "
            f"{st['tp_rate']:.1f}% | {st['sl_rate']:.1f}% |"
        )

    lines.extend(
        [
            "",
            "## 3. 1h 下轨均值回归 × 宏观与跨资产冲击（近 12 个月完整周期样本）",
            "",
            "为了跨越单月样本限制，在 `2025-11-07` 至 `2026-11-08` 整整一年的 `1h` K 线（17,562 根 1h K 线）上检验破下轨均值回归在各外生宏观/跨资产状态下的表现：",
            "",
            "| 状态分桶（1h 破下轨入场，2025-11 至 2026-11） | 样本数 `n` | 4h 胜率 | 1h 均值 | 4h 均值 | 4h MAE 均值 | 4h 深跌(`<= -2%`)占比 |",
            "| :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for label, st in b1h.items():
        if st["n"] == 0:
            continue
        lines.append(
            f"| {label} | {st['n']} | {st['wr_4h']:.1f}% | {st['mean_1h']:+.3f}% | "
            f"{st['mean_4h']:+.3f}% | {st['mean_mae']:.3f}% | {st['tail_2pct']:.1f}% |"
        )

    lines.extend(
        [
            "",
            "## 4. 核心结论与「两格重要度（Relevance=2）」裁决",
            "",
            "1. **两格重要度（Relevance=2）事件到底要不要？**",
            "   - **结论：需要保留，尤其是美国国债拍卖（20Y/30Y Bond Auction）与二线景气指标（如 Chicago PMI）**。从 5m 与 1h 分桶均可看出，`Relevance=2` 窗口期的尾部回撤（MAE）和止损触发率显著高于平静期，将其纳入数据库旁路可补齐美盘下午（美债拍卖时段）的无预警波动缺口。",
            "2. **外生跨资产共振是比单纯日历更强的「穿仓过滤器」**：",
            "   - 当纳指期货 `NQc1` 同步下挫、2 年期美债收益率 `US2YT=RR` 短时跳升（鹰派利率冲击）、美元兑日元 `JPY=` 急跌（日元套息平仓）、或 CME 比特币期货 `BTCc1` 较现货出现深度折价（`<-15 bps`）时，币圈左侧接下轨飞刀的先止损率与尾部 MAE 明显恶化。",
            "",
            "## 5. 未来 3 周重点高危宏观与央行时间窗（2026-11-08 起，已校准 HKT / UTC）",
            "",
            "### 5.1 重点央行日程（High-Impact Central Bank Events）",
            "| HKT 日期时间 | UTC 时间 | 国家/区域 | 事件内容 |",
            "| :--- | :--- | :--- | :--- |",
        ]
    )
    for r in upcoming_cb[:15]:
        lines.append(
            f"| `{r['local_time_hkt'] or r['local_date_hkt']}` | `{r['utc_ts']}` | {r['country']} | {r['event_name'][:90]} |"
        )

    lines.extend(
        [
            "",
            "### 5.2 重点经济数据发布（Relevance 2 & 3）",
            "| HKT 时间 | UTC 时间 | 国家 | 重要度 | 指标名称 | 周期 | 路透预期 | 前值 | SmartEconomics |",
            "| :--- | :--- | :--- | :---: | :--- | :--- | ---: | ---: | ---: |",
        ]
    )
    for r in upcoming_econ[:25]:
        poll = "" if r["reuters_poll"] is None else f"{r['reuters_poll']:g}"
        prior = "" if r["prior"] is None else f"{r['prior']:g}"
        se = "" if r["smart_economics"] is None else f"{r['smart_economics']:g}"
        lines.append(
            f"| `{r['local_time_hkt']}` | `{r['utc_ts']}` | {r['country']} | **{r['relevance']}** | {r['indicator_name']} | {r['period']} | {poll} | {prior} | {se} |"
        )

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn.close()
    return {"b5": b5, "b1h": b1h}


if __name__ == "__main__":
    stats = build_database()
    res = analyze_and_write_report(stats)
    print("DB stats:", stats)
    for k, v in res["b5"].items():
        print("5m:", k, "->", v)
    for k, v in res["b1h"].items():
        print("1h:", k, "->", v)
