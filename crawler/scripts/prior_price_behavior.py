#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_price_behavior.py — 价格行为先验（拉升 / 收益 / 回撤）PRIOR_STEP7

用法:
  python3 scripts/prior_price_behavior.py

输入: 主库 dexscreener.db（只读）pair_history；可选 decision_log + prior.db.decision_prior_tags
输出:
  data/prior/exports/price_behavior.json
  data/prior/exports/price_behavior_YYYYMMDD.md
  可选 merge baseline_params.json["price_behavior"]（不删其它键）

口径（务必读）:
  - 单位: (chain_id, token_address)；每个 token 取 price_usd>0 点数最多的 pair_address
    （多池不混价，避免假拉升）
  - t0 = 该 pair 上第一条 price_usd>0 的 recorded_at；p0 = 该点价格
  - 收益 horizon∈{15m,1h,4h}:
      若 span = last−t0 ≥ horizon：在 [t0+horizon ± TOL] 内取最接近点；
      若容差内无点但 span≥horizon，则用 last 价（标注 used_last）
      ret = p_h / p0 − 1
  - 这是 crawler 观测收益，非交易所完美 OHLCV
  - 拉升（向上样本）: 窗口内 max/min ≥ UP_MULT（默认 1.05）
      multiple = max_p / min_p；duration = t(max_after_min) − t(min)（分钟）
  - 回撤（入场代理）: 自 p0 起 4h 窗内
      mdd_4h = max(0, (p0 − min_p_in_window) / p0)   # 正数，越大跌越深
  - JEV 交叉: decision_log 信号时刻就近价作 p0，算同类 ret/mdd；按 age_bucket 分桶
  - 短窗: crawler 总窗口 <48h → 全局标记 SHORT

原则: 主库 mode=ro；只写 prior exports / 可选 merge baseline_params；
      不改 INDICATORS_WEIGHT_*、trading、alerts、业务四库。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
try:
    sys.path.insert(0, ROOT)
    from config import DB_PATH as _CFG_DB

    DB = _CFG_DB if os.path.isabs(_CFG_DB) else os.path.join(ROOT, _CFG_DB)
except Exception:
    DB = os.path.join(ROOT, "dexscreener.db")

PRIOR_BASE = os.path.join(ROOT, "data", "prior")
PRIOR_DB = os.path.join(PRIOR_BASE, "db", "prior.db")
OUT = os.path.join(PRIOR_BASE, "exports")
os.makedirs(OUT, exist_ok=True)

HK = timezone(timedelta(hours=8))
DATE = datetime.now(HK).strftime("%Y%m%d")

# Horizons in seconds
HORIZONS = {
    "15m": 15 * 60,
    "1h": 3600,
    "4h": 4 * 3600,
}
# Snap tolerance around t0+horizon (hot cadence ≈6min → 20min is generous)
TOL_SEC = 20 * 60
# Minimum upward multiple to count as 拉升样本
UP_MULT = 1.05
# Min ticks for a usable series
MIN_TICKS = 2
# Window for pump / drawdown analyses
PUMP_WINDOWS = {
    "full": None,  # entire observation
    "1h": 3600,
    "4h": 4 * 3600,
}
MDD_HORIZON_SEC = 4 * 3600


def parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pctile(sorted_vals: list[float], p: float) -> Optional[float]:
    """p in [0,1]; nearest-rank on already-sorted list."""
    if not sorted_vals:
        return None
    if p <= 0:
        return sorted_vals[0]
    if p >= 1:
        return sorted_vals[-1]
    idx = min(len(sorted_vals) - 1, int(p * len(sorted_vals)))
    return sorted_vals[idx]


def summarize(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0, "median": None, "p10": None, "p25": None, "p75": None, "p90": None}
    s = sorted(vals)
    return {
        "n": len(s),
        "median": round(pctile(s, 0.50), 6),
        "p10": round(pctile(s, 0.10), 6),
        "p25": round(pctile(s, 0.25), 6),
        "p75": round(pctile(s, 0.75), 6),
        "p90": round(pctile(s, 0.90), 6),
    }


def fmt_pct(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def fmt_num(x: Optional[float], digits: int = 3) -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}f}"


def price_at_horizon(
    times: list[datetime],
    prices: list[float],
    t0: datetime,
    horizon_sec: float,
    tol_sec: float = TOL_SEC,
) -> tuple[Optional[float], str]:
    """Return (price, method) where method is 'nearest'|'last'|'none'."""
    target = t0 + timedelta(seconds=horizon_sec)
    last_t, last_p = times[-1], prices[-1]
    span = (last_t - t0).total_seconds()
    if span < horizon_sec - 1e-6:
        return None, "none"

    best_i = None
    best_dt = None
    for i, t in enumerate(times):
        dt = abs((t - target).total_seconds())
        if dt <= tol_sec and (best_dt is None or dt < best_dt):
            best_dt = dt
            best_i = i
    if best_i is not None:
        return prices[best_i], "nearest"
    # fallback: last price if span covers horizon
    return last_p, "last"


def window_slice(
    times: list[datetime],
    prices: list[float],
    t0: datetime,
    window_sec: Optional[float],
) -> tuple[list[datetime], list[float]]:
    if window_sec is None:
        return times, prices
    t1 = t0 + timedelta(seconds=window_sec)
    ts, ps = [], []
    for t, p in zip(times, prices):
        if t0 <= t <= t1:
            ts.append(t)
            ps.append(p)
    return ts, ps


def pump_stats(
    times: list[datetime], prices: list[float]
) -> Optional[dict[str, float]]:
    """Low-to-high multiple + duration (min) if upward enough."""
    if len(prices) < 2:
        return None
    min_p = min(prices)
    max_p = max(prices)
    if min_p <= 0 or max_p / min_p < UP_MULT:
        return None
    # first index of min, then first max strictly after that min
    i_min = prices.index(min_p)
    max_after = None
    i_max = None
    for i in range(i_min, len(prices)):
        if max_after is None or prices[i] > max_after:
            max_after = prices[i]
            i_max = i
    if i_max is None or i_max <= i_min or max_after is None or max_after / min_p < UP_MULT:
        # min is at the end → no low→high rally; try global max then preceding min
        i_max_g = prices.index(max_p)
        if i_max_g == 0:
            return None
        min_before = min(prices[: i_max_g + 1])
        if min_before <= 0 or max_p / min_before < UP_MULT:
            return None
        i_min = prices.index(min_before)  # first occurrence in prefix
        if i_min >= i_max_g:
            return None
        return {
            "multiple": max_p / min_before,
            "duration_min": (times[i_max_g] - times[i_min]).total_seconds() / 60.0,
        }
    return {
        "multiple": max_after / min_p,
        "duration_min": (times[i_max] - times[i_min]).total_seconds() / 60.0,
    }


def mdd_from_entry(
    times: list[datetime], prices: list[float], t0: datetime, p0: float, horizon_sec: float
) -> Optional[float]:
    """Max drawdown from entry p0 within [t0, t0+horizon]. Positive = loss depth."""
    if p0 <= 0:
        return None
    t1 = t0 + timedelta(seconds=horizon_sec)
    # Need at least one point in window after t0 (include t0)
    mins = [p for t, p in zip(times, prices) if t0 <= t <= t1]
    if len(mins) < 2:
        # if span short of horizon but we have points, still compute on available (label SHORT later)
        if len(mins) < 1:
            return None
    min_p = min(mins)
    return max(0.0, (p0 - min_p) / p0)


def open_ro(path: str) -> sqlite3.Connection:
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_best_pair_series(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """For each (chain, token), keep the pair with most valid price ticks.

    Returns map (chain, token) -> {pair, times[], prices[], n}
    """
    # First pass: count ticks per (chain, token, pair)
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in conn.execute(
        """
        SELECT chain_id, token_address, pair_address, COUNT(*) AS n
        FROM pair_history
        WHERE token_address IS NOT NULL AND token_address != ''
          AND price_usd IS NOT NULL AND price_usd > 0
        GROUP BY chain_id, token_address, pair_address
        """
    ):
        counts[(row["chain_id"], row["token_address"], row["pair_address"])] = row["n"]

    best_pair: dict[tuple[str, str], str] = {}
    best_n: dict[tuple[str, str], int] = {}
    for (chain, tok, pair), n in counts.items():
        key = (chain, tok)
        if key not in best_n or n > best_n[key]:
            best_n[key] = n
            best_pair[key] = pair

    # Build set of (chain, pair) we need
    needed_pairs = {(c, best_pair[(c, t)]) for (c, t) in best_pair}

    # Stream all rows for needed pairs — filter in Python by best pair per token
    series: dict[tuple[str, str], dict] = {}
    # Map pair -> token keys that selected it (usually 1)
    pair_to_tokens: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for (chain, tok), pair in best_pair.items():
        pair_to_tokens[(chain, pair)].append((chain, tok))

    for row in conn.execute(
        """
        SELECT chain_id, token_address, pair_address, recorded_at, price_usd
        FROM pair_history
        WHERE token_address IS NOT NULL AND token_address != ''
          AND price_usd IS NOT NULL AND price_usd > 0
        ORDER BY recorded_at ASC
        """
    ):
        chain = row["chain_id"]
        tok = row["token_address"]
        pair = row["pair_address"]
        key = (chain, tok)
        if best_pair.get(key) != pair:
            continue
        t = parse_ts(row["recorded_at"])
        if t is None:
            continue
        p = float(row["price_usd"])
        if key not in series:
            series[key] = {"pair": pair, "times": [], "prices": []}
        series[key]["times"].append(t)
        series[key]["prices"].append(p)

    return series


def analyze_all(series: dict[tuple[str, str], dict]) -> dict[str, Any]:
    returns: dict[str, list[float]] = {h: [] for h in HORIZONS}
    returns_method: dict[str, dict[str, int]] = {
        h: {"nearest": 0, "last": 0} for h in HORIZONS
    }
    n_eligible: dict[str, int] = {h: 0 for h in HORIZONS}

    pump_mult: dict[str, list[float]] = {w: [] for w in PUMP_WINDOWS}
    pump_dur: dict[str, list[float]] = {w: [] for w in PUMP_WINDOWS}

    mdd_vals: list[float] = []
    mdd_short_vals: list[float] = []  # span < 4h but has points

    n_tokens = 0
    n_skipped_short = 0
    spans_h: list[float] = []

    for key, ser in series.items():
        times, prices = ser["times"], ser["prices"]
        if len(times) < MIN_TICKS:
            n_skipped_short += 1
            continue
        n_tokens += 1
        t0, p0 = times[0], prices[0]
        span = (times[-1] - t0).total_seconds()
        spans_h.append(span / 3600.0)

        for hname, hsec in HORIZONS.items():
            if span >= hsec:
                n_eligible[hname] += 1
            ph, method = price_at_horizon(times, prices, t0, hsec)
            if ph is not None and p0 > 0 and method != "none":
                ret = ph / p0 - 1.0
                returns[hname].append(ret)
                returns_method[hname][method] = returns_method[hname].get(method, 0) + 1

        for wname, wsec in PUMP_WINDOWS.items():
            wt, wp = window_slice(times, prices, t0, wsec)
            # For timed windows, require span covering most of window (or enough points)
            if wsec is not None and span < wsec * 0.5:
                continue
            ps = pump_stats(wt, wp)
            if ps:
                pump_mult[wname].append(ps["multiple"])
                pump_dur[wname].append(ps["duration_min"])

        # MDD 4h
        if span >= MDD_HORIZON_SEC:
            m = mdd_from_entry(times, prices, t0, p0, MDD_HORIZON_SEC)
            if m is not None:
                mdd_vals.append(m)
        else:
            m = mdd_from_entry(times, prices, t0, p0, span if span > 0 else 1)
            if m is not None and len(prices) >= 2:
                mdd_short_vals.append(m)

    return {
        "n_tokens_analyzed": n_tokens,
        "n_skipped_lt_min_ticks": n_skipped_short,
        "span_h": summarize(spans_h),
        "returns": {
            h: {
                **summarize(returns[h]),
                "n_eligible_span": n_eligible[h],
                "method_counts": returns_method[h],
            }
            for h in HORIZONS
        },
        "pump": {
            w: {
                "multiple": summarize(pump_mult[w]),
                "duration_min": summarize(pump_dur[w]),
                "up_mult_threshold": UP_MULT,
            }
            for w in PUMP_WINDOWS
        },
        "drawdown": {
            "definition": (
                "mdd_4h = max(0, (p0 - min_price_in_[t0,t0+4h]) / p0); "
                "entry proxy from first observed price; positive = depth of loss"
            ),
            "mdd_4h": summarize(mdd_vals),
            "mdd_available_span_lt_4h": summarize(mdd_short_vals),
            "note": "Primary metric is mdd_4h on tokens with span≥4h; short-span set labeled separately",
        },
    }


def nearest_price_in_series(
    times: list[datetime], prices: list[float], target: datetime, tol_sec: float
) -> Optional[float]:
    best_p, best_dt = None, None
    for t, p in zip(times, prices):
        dt = abs((t - target).total_seconds())
        if dt <= tol_sec and (best_dt is None or dt < best_dt):
            best_dt = dt
            best_p = p
    return best_p


def analyze_jev(
    conn_main: sqlite3.Connection,
    series: dict[tuple[str, str], dict],
) -> dict[str, Any]:
    """Optional JEV cross: decision_log × tags vs all-token distributions."""
    out: dict[str, Any] = {
        "available": False,
        "n_signals": 0,
        "n_with_series": 0,
        "note": "",
        "by_age_bucket": {},
        "all_signals": {},
    }
    n_dec = conn_main.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
    out["n_signals"] = n_dec
    if n_dec == 0:
        out["note"] = "decision_log empty — skip JEV cross"
        return out

    tags: dict[str, dict] = {}
    if os.path.isfile(PRIOR_DB):
        try:
            pc = open_ro(PRIOR_DB)
            if pc.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_prior_tags'"
            ).fetchone():
                for r in pc.execute(
                    "SELECT decision_id, age_bucket, token_address, chain_id, signal_ts FROM decision_prior_tags"
                ):
                    tags[str(r["decision_id"])] = dict(r)
            pc.close()
        except Exception as e:
            out["note"] = f"prior.db tags unreadable: {e}"

    rows = conn_main.execute(
        """
        SELECT id, ts, chain_id, token_address,
               json_extract(raw_json, '$.pair_address') AS pair_address
        FROM decision_log
        """
    ).fetchall()

    per_bucket: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"ret_1h": [], "ret_4h": [], "mdd_4h": []}
    )
    all_ret_1h: list[float] = []
    all_ret_4h: list[float] = []
    all_mdd: list[float] = []
    n_with = 0
    n_no_series = 0
    n_no_snap = 0

    for r in rows:
        did = str(r["id"])
        chain, tok = r["chain_id"], r["token_address"]
        if not chain or not tok:
            n_no_series += 1
            continue
        key = (chain, tok)
        ser = series.get(key)
        if not ser or len(ser["times"]) < MIN_TICKS:
            n_no_series += 1
            continue
        t_sig = parse_ts(r["ts"])
        if t_sig is None:
            n_no_snap += 1
            continue
        # Prefer price near signal; else first price at/after signal; else first overall
        p0 = nearest_price_in_series(ser["times"], ser["prices"], t_sig, TOL_SEC)
        t0 = t_sig
        if p0 is None:
            # first tick at or after signal
            for t, p in zip(ser["times"], ser["prices"]):
                if t >= t_sig:
                    p0, t0 = p, t
                    break
        if p0 is None:
            # use series start if signal before first tick
            if ser["times"][0] >= t_sig - timedelta(seconds=TOL_SEC):
                p0, t0 = ser["prices"][0], ser["times"][0]
            else:
                n_no_snap += 1
                continue

        n_with += 1
        tag = tags.get(did) or {}
        bucket = tag.get("age_bucket") or "unknown"

        # Align series relative to t0 for horizon returns
        # Build sub-series from ticks >= t0 (or full if t0 is signal and we snapped)
        times, prices = ser["times"], ser["prices"]
        # Find index of tick used for p0 / nearest after t0
        # Recompute returns from t0 using full series
        ph1, m1 = price_at_horizon(times, prices, t0, HORIZONS["1h"])
        ph4, m4 = price_at_horizon(times, prices, t0, HORIZONS["4h"])
        if ph1 is not None and p0 > 0:
            ret1 = ph1 / p0 - 1.0
            per_bucket[bucket]["ret_1h"].append(ret1)
            all_ret_1h.append(ret1)
        if ph4 is not None and p0 > 0:
            ret4 = ph4 / p0 - 1.0
            per_bucket[bucket]["ret_4h"].append(ret4)
            all_ret_4h.append(ret4)

        span = (times[-1] - t0).total_seconds()
        if span >= MDD_HORIZON_SEC:
            mdd = mdd_from_entry(times, prices, t0, p0, MDD_HORIZON_SEC)
            if mdd is not None:
                per_bucket[bucket]["mdd_4h"].append(mdd)
                all_mdd.append(mdd)

    out["n_with_series"] = n_with
    out["n_no_series"] = n_no_series
    out["n_no_snap"] = n_no_snap
    out["available"] = n_with > 0
    out["all_signals"] = {
        "ret_1h": summarize(all_ret_1h),
        "ret_4h": summarize(all_ret_4h),
        "mdd_4h": summarize(all_mdd),
    }
    out["by_age_bucket"] = {
        b: {
            "ret_1h": summarize(v["ret_1h"]),
            "ret_4h": summarize(v["ret_4h"]),
            "mdd_4h": summarize(v["mdd_4h"]),
        }
        for b, v in sorted(per_bucket.items())
    }

    # Sparsity note
    n_124 = (per_bucket.get("1-24h") or {}).get("ret_1h") or []
    if n_with < 30:
        out["note"] = (
            f"JEV cross sparse: only {n_with} signals with usable series "
            f"(decision_log n={n_dec}); interpret with caution"
        )
    elif len(n_124) < 30:
        out["note"] = (
            f"age_bucket 1-24h ret_1h n={len(n_124)} — still small; "
            "tables shown, avoid strong claims vs all-token baseline (selection bias)"
        )
    else:
        out["note"] = "JEV cross computed; compare to all-token baseline carefully (selection bias)"
    return out


def build_markdown(payload: dict[str, Any]) -> str:
    L: list[str] = []
    A = L.append
    meta = payload["meta"]
    A(f"# 价格行为先验（拉升） {meta['date']}")
    A("")
    A("> **只读** `dexscreener.db.pair_history`；可选 `decision_log` + `prior.db.decision_prior_tags`。")
    A("> **口径**：每 token 取点数最多的 pair；t0=首条 price_usd>0；"
      "ret=p_h/p0−1；拉升=窗内 max/min≥"
      f"{UP_MULT}；mdd_4h=max(0,(p0−min)/p0)。")
    A("> crawler 观测收益 ≠ 交易所完美 OHLCV；短窗见 SHORT 标记。")
    A("")

    A("## 0. 样本与窗口")
    A(f"- DB: `{meta['db']}`（mode=ro）")
    A(f"- pair_history 行数: {meta['n_history_rows']:,}")
    A(f"- recorded_at 范围: `{meta['recorded_at_range'][0]}` → `{meta['recorded_at_range'][1]}`")
    A(f"- crawler 总窗口: **{meta['crawler_window_h']:.1f}h** "
      f"{'→ **SHORT**' if meta['short'] else ''}")
    A(f"- 分析 token 数: **{payload['n_tokens_analyzed']:,}**"
      f"（跳过 ticks<{MIN_TICKS}: {payload['n_skipped_lt_min_ticks']:,}）")
    A(f"- 价格字段: `price_usd`（无 close 列）")
    A(f"- 容差 TOL: {TOL_SEC}s；horizon: 15m / 1h / 4h")
    A("")

    A("## A. 收益分布（crawler-observation returns）")
    A("| horizon | n | median | P10 | P25 | P75 | P90 | nearest/last |")
    A("|---|---:|---:|---:|---:|---:|---:|---|")
    for h in ("15m", "1h", "4h"):
        r = payload["returns"][h]
        mc = r.get("method_counts") or {}
        A(
            f"| {h} | {r['n']} | {fmt_pct(r['median'])} | {fmt_pct(r['p10'])} | "
            f"{fmt_pct(r['p25'])} | {fmt_pct(r['p75'])} | {fmt_pct(r['p90'])} | "
            f"{mc.get('nearest', 0)}/{mc.get('last', 0)} |"
        )
    A("")
    A("说明：仅 span≥horizon 的 token 计入；优先 t0+h±TOL 最近点，否则 last 价。")
    A("")

    A("## B. 拉升特征（向上样本 max/min ≥ "
      f"{UP_MULT}）")
    A("| 窗口 | n | multiple P25/P50/P75/P90 | duration_min P25/P50/P75 |")
    A("|---|---:|---|---|")
    for w in ("1h", "4h", "full"):
        p = payload["pump"][w]
        m, d = p["multiple"], p["duration_min"]
        A(
            f"| {w} | {m['n']} | "
            f"{fmt_num(m['p25'])}/{fmt_num(m['median'])}/{fmt_num(m['p75'])}/{fmt_num(m['p90'])} | "
            f"{fmt_num(d['p25'],1)}/{fmt_num(d['median'],1)}/{fmt_num(d['p75'],1)} |"
        )
    A("")
    A("duration = 窗内（低点之后的高点时间 − 低点时间），分钟。")
    A("")

    A("## C. 回撤（入场代理 MDD）")
    A(f"- 定义: `{payload['drawdown']['definition']}`")
    dd = payload["drawdown"]["mdd_4h"]
    A("| 指标 | n | P50 | P90 | P25 | P75 |")
    A("|---|---:|---:|---:|---:|---:|")
    A(
        f"| mdd_4h (span≥4h) | {dd['n']} | {fmt_pct(dd['median'])} | "
        f"{fmt_pct(dd['p90'])} | {fmt_pct(dd['p25'])} | {fmt_pct(dd['p75'])} |"
    )
    ds = payload["drawdown"]["mdd_available_span_lt_4h"]
    if ds["n"]:
        A(
            f"| mdd_可用窗 (span<4h, SHORT) | {ds['n']} | {fmt_pct(ds['median'])} | "
            f"{fmt_pct(ds['p90'])} | {fmt_pct(ds['p25'])} | {fmt_pct(ds['p75'])} |"
        )
    A("")

    A("## D. JEV 交叉（可选）")
    jev = payload.get("jev") or {}
    if not jev.get("available"):
        A(f"- 跳过/稀疏: {jev.get('note') or 'unavailable'}")
    else:
        A(f"- {jev.get('note', '')}")
        A(f"- signals: {jev['n_signals']}；有序列: {jev['n_with_series']}")
        a = jev["all_signals"]
        A("")
        A("### 全部 JEV 信号")
        A("| 指标 | n | median | P10 | P90 |")
        A("|---|---:|---:|---:|---:|")
        for name, key in (("ret_1h", "ret_1h"), ("ret_4h", "ret_4h"), ("mdd_4h", "mdd_4h")):
            s = a[key]
            A(
                f"| {name} | {s['n']} | {fmt_pct(s['median'])} | "
                f"{fmt_pct(s['p10'])} | {fmt_pct(s['p90'])} |"
            )
        A("")
        A("### 按 age_bucket")
        A("| bucket | ret_1h n/med | ret_4h n/med | mdd_4h n/P50 |")
        A("|---|---|---|---|")
        for b, v in (jev.get("by_age_bucket") or {}).items():
            r1, r4, md = v["ret_1h"], v["ret_4h"], v["mdd_4h"]
            A(
                f"| {b} | {r1['n']}/{fmt_pct(r1['median'])} | "
                f"{r4['n']}/{fmt_pct(r4['median'])} | "
                f"{md['n']}/{fmt_pct(md['median'])} |"
            )
    A("")

    A("## 反假象")
    for w in meta.get("warnings") or []:
        A(f"- ⚠ {w}")
    A("- 勿把 crawler 观测收益当成可交易 alpha；抽样偏 hot/warm 池。")
    A("- 多 pair token 只取最密 pair，避免跨池价差伪拉升。")
    A("- n 以实际计入为准，不夸大。")
    A("")
    A("## 不做")
    A("- 不改 INDICATORS_WEIGHT_* / trading / alerts / 业务四库")
    A("- 默认不进风控（RISK_USE_PRIOR=0）")
    A("")
    return "\n".join(L) + "\n"


def merge_baseline(summary: dict[str, Any]) -> str:
    path = os.path.join(OUT, "baseline_params.json")
    existing: dict[str, Any] = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    meta = existing.get("meta") or {}
    sources = list(meta.get("sources") or [])
    if "price_behavior" not in sources:
        sources.append("price_behavior")
    meta["sources"] = sources
    meta["date"] = DATE
    existing["meta"] = meta
    existing["price_behavior"] = summary
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return path


def main() -> None:
    if not os.path.exists(DB):
        print(f"[FAIL] DB not found: {DB}", file=sys.stderr)
        sys.exit(1)

    conn = open_ro(DB)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pair_history)")}
    need = {"chain_id", "token_address", "pair_address", "recorded_at", "price_usd"}
    if not need.issubset(cols):
        print(f"[FAIL] pair_history missing cols; have={sorted(cols)}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    global_min, global_max, n_hist = conn.execute(
        "SELECT MIN(recorded_at), MAX(recorded_at), COUNT(*) FROM pair_history"
    ).fetchone()
    g0, g1 = parse_ts(global_min), parse_ts(global_max)
    crawler_window_h = (g1 - g0).total_seconds() / 3600.0 if g0 and g1 else None
    short = bool(crawler_window_h is not None and crawler_window_h < 48)

    warnings = []
    if short:
        warnings.append(
            f"crawler 总窗口仅约 {crawler_window_h:.1f}h → 标记 SHORT；"
            "4h 收益/回撤可用但勿外推为长期市场先验"
        )
    if crawler_window_h is not None and crawler_window_h < 168:
        warnings.append("窗口 <7d：无 7d 收益先验（本 step 也不计算）")

    print("[INFO] loading best-pair series…")
    series = load_best_pair_series(conn)
    print(f"[INFO] tokens with series: {len(series)}")

    print("[INFO] analyzing returns / pump / drawdown…")
    stats = analyze_all(series)

    print("[INFO] JEV cross…")
    jev = analyze_jev(conn, series)
    conn.close()

    meta = {
        "date": DATE,
        "generated_at": datetime.now(HK).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "db": os.path.relpath(DB, ROOT),
        "table": "pair_history",
        "price_field": "price_usd",
        "n_history_rows": n_hist,
        "recorded_at_range": [global_min, global_max],
        "crawler_window_h": round(crawler_window_h, 2) if crawler_window_h else None,
        "short": short,
        "tol_sec": TOL_SEC,
        "up_mult_threshold": UP_MULT,
        "unit": "(chain_id, token_address) via densest pair_address",
        "warnings": warnings,
        "definitions": {
            "return": "p(t0+h)/p0 - 1; nearest within TOL else last if span>=h",
            "pump_multiple": f"max/min within window among tokens with max/min>={UP_MULT}",
            "pump_duration_min": "minutes from low to subsequent high within window",
            "mdd_4h": "max(0, (p0 - min_in_[t0,t0+4h])/p0) from first observed price",
        },
    }

    payload = {
        "meta": meta,
        **stats,
        "jev": jev,
    }

    json_path = os.path.join(OUT, "price_behavior.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    md = build_markdown(payload)
    md_path = os.path.join(OUT, f"price_behavior_{DATE}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    # Compact summary for baseline_params merge
    summary = {
        "date": DATE,
        "short": short,
        "crawler_window_h": meta["crawler_window_h"],
        "n_tokens": stats["n_tokens_analyzed"],
        "returns": {
            h: {
                "n": payload["returns"][h]["n"],
                "median": payload["returns"][h]["median"],
                "p10": payload["returns"][h]["p10"],
                "p90": payload["returns"][h]["p90"],
            }
            for h in ("15m", "1h", "4h")
        },
        "pump_4h": {
            "n": payload["pump"]["4h"]["multiple"]["n"],
            "multiple_p50": payload["pump"]["4h"]["multiple"]["median"],
            "duration_min_p50": payload["pump"]["4h"]["duration_min"]["median"],
        },
        "mdd_4h": {
            "n": payload["drawdown"]["mdd_4h"]["n"],
            "p50": payload["drawdown"]["mdd_4h"]["median"],
            "p90": payload["drawdown"]["mdd_4h"]["p90"],
        },
        "jev_n_with_series": jev.get("n_with_series"),
        "export_json": os.path.relpath(json_path, ROOT),
        "export_md": os.path.relpath(md_path, ROOT),
        "note": "crawler-observation; see price_behavior.json for full口径",
    }
    baseline_path = merge_baseline(summary)

    print(md)
    print("[OUT]", json_path)
    print("[OUT]", md_path)
    print("[OUT]", baseline_path, "(merged price_behavior)")
    r1 = payload["returns"]["1h"]
    r4 = payload["returns"]["4h"]
    dd = payload["drawdown"]["mdd_4h"]
    print(
        f"[KEY] n={stats['n_tokens_analyzed']} "
        f"ret1h med={fmt_pct(r1['median'])} n={r1['n']} "
        f"ret4h med={fmt_pct(r4['median'])} n={r4['n']} "
        f"mdd4h P50/P90={fmt_pct(dd['median'])}/{fmt_pct(dd['p90'])} n={dd['n']} "
        f"SHORT={short}"
    )


if __name__ == "__main__":
    main()
