"""Locked C4 order-flow evaluation (pre-registration 2026-10-21).

Implements /workspace/out/c4_prereg_eval_protocol_2026-10-21.md exactly.
All parameters are constants below and MUST NOT be changed after lock.

Usage (only after the window closes, 2026-10-24 07:00 HKT):
  python -m crawler.research.c4_eval --db dexscreener.db --segment train --cells 268 --out out/
  python -m crawler.research.c4_eval --db dexscreener.db --segment oos   --cells 268 --out out/ \
         --train-result out/c4_train_result.json

The DB is opened read-only (sqlite URI mode=ro). OOS can be run exactly once:
a marker file is created before any OOS computation and a second run refuses.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

# ---------------- locked parameters ----------------
WINDOW_START = "2026-10-20T23:08:00Z"   # 2026-10-21 07:08 HKT
WINDOW_END = "2026-10-23T23:00:00Z"     # 2026-10-24 07:00 HKT
BUY_SHARE_MIN = 0.65
TXNS_M5_MIN = 20
VOL_MULT = 3.0
LOOKBACK_SEC = 6 * 3600
MIN_PRIOR_SNAPS = 30          # prior-6h snapshots (any) and non-null volume_m5 values
LIQ_FLOOR = 50_000.0
DEDUP_SEC = 3600
HOLD_SEC = 3600
TP, SL = 0.10, 0.05
# tiered round-trip cost: liq > 1M -> 0.6%; 50k..1M -> 2%
COST_MULT_SENS = 2.0
RANDOM_SEED = 20261021
RANDOM_P = 0.05
# Fixed calendar cut shared by C4 and random baseline (locked, not data-derived):
# window start 2026-10-21 07:08 HKT + 60% of 71h52m (= 02:15:12), rounded by group to 02:15:00 HKT.
CUT_ISO = "2026-10-22T18:15:00Z"       # 2026-10-23 02:15:00 HKT
MAX_ENTRY_GAP_SEC = 600                # entry snapshot must be <= 10 min after trigger snapshot
GAP_WARN_FRAC = 0.05                   # invalid_gap / triggers above this -> WARNING flag
MIN_EVENTS = 30
MIN_TOKENS = 30
N_BOOT = 10_000
FAMILY_ALPHA = 0.05
# U/L/J (采集 bad_price_dislocation_20261021.md §4)
ULJ_PRIOR_SEC = 3600
ULJ_PRIOR_MAX = 12
U_MIN_PRICE = 1e-15
U_SUPPLY_DEV = 1.5
L_ABS = 1000.0
L_REL = 0.1
J_RATIO = 3.0
J_LIQ_FRAC = 0.25

EXITS = ("tp10_sl5_h60", "time60")


def _ts(s: str) -> int:
    t = s.strip().replace("Z", "+00:00")
    d = datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def _nan(x) -> float:
    return float("nan") if x is None else float(x)


# ---------------- data ----------------
@dataclass
class Pair:
    pair: str
    token: str
    t: np.ndarray
    px: np.ndarray
    liq: np.ndarray
    fdv: np.ndarray
    hot: np.ndarray
    buys: np.ndarray
    sells: np.ndarray
    vm5: np.ndarray


def open_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)


def load_pairs(con: sqlite3.Connection, start: str = WINDOW_START, end: str = WINDOW_END) -> list[Pair]:
    """Load snapshots in [start, end]. Data before start is never used."""
    rows = con.execute(
        """SELECT pair_address, token_address, recorded_at, price_usd, liquidity_usd, fdv, tier,
                  txns_m5_buys, txns_m5_sells, volume_m5
           FROM pair_history WHERE recorded_at >= ? AND recorded_at <= ?
           ORDER BY pair_address, recorded_at""",
        (start, end),
    ).fetchall()
    out: list[Pair] = []
    cur: list = []
    def flush():
        if not cur:
            return
        a = list(zip(*cur))
        out.append(Pair(cur[0][0], cur[0][1],
                        np.array([_ts(x) for x in a[2]], dtype=np.int64),
                        np.array([_nan(x) for x in a[3]]), np.array([_nan(x) for x in a[4]]),
                        np.array([_nan(x) for x in a[5]]), np.array([x == "hot" for x in a[6]]),
                        np.array([_nan(x) for x in a[7]]), np.array([_nan(x) for x in a[8]]),
                        np.array([_nan(x) for x in a[9]])))
    for r in rows:
        if cur and r[0] != cur[0][0]:
            flush(); cur = []
        cur.append(r)
    flush()
    return out


# ---------------- U/L/J (no lookahead) ----------------
def ulj_flags(p: Pair):
    n = len(p.t)
    U = np.zeros(n, bool); L = np.zeros(n, bool); Lrel = np.zeros(n, bool); J = np.zeros(n, bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        sup = np.where((p.px > 0) & (p.fdv > 0), p.fdv / p.px, np.nan)
    for i in range(n):
        lo = np.searchsorted(p.t, p.t[i] - ULJ_PRIOR_SEC)
        P = np.arange(max(lo, i - ULJ_PRIOR_MAX), i)
        px = p.px[i]; lq = p.liq[i]
        if not (px > 0) or px < U_MIN_PRICE:
            U[i] = True
        if math.isnan(lq) or lq < L_ABS:
            L[i] = True
        if len(P):
            s = sup[P]; s = s[~np.isnan(s)]
            if len(s) and not math.isnan(sup[i]) and abs(math.log(sup[i] / np.median(s))) > math.log(U_SUPPLY_DEV):
                U[i] = True
            q = p.liq[P]; q = q[~np.isnan(q)]
            if len(q) and not math.isnan(lq) and lq < L_REL * np.median(q):
                Lrel[i] = True
            P3 = P[-3:]
            pm = np.median(p.px[P3])
            q3 = p.liq[P3]; q3 = q3[~np.isnan(q3)]
            lm = np.median(q3) if len(q3) else float("nan")
            if pm > 0 and px > 0:
                r = math.log(px / pm)
                if abs(r) > math.log(J_RATIO):
                    lr = math.log(lq / lm) if (lm > 0 and lq > 0) else 0.0
                    if np.sign(lr) != np.sign(r) or abs(lr) < J_LIQ_FRAC * abs(r):
                        J[i] = True
    return U, (L | Lrel), Lrel, J


# ---------------- pool / trigger ----------------
def pool_mask(p: Pair) -> np.ndarray:
    """Same pool for C4 and random: hot, >=30 snapshots in [t-6h,t), liq>=50k at t."""
    n = len(p.t); m = np.zeros(n, bool)
    for i in range(n):
        lo = np.searchsorted(p.t, p.t[i] - LOOKBACK_SEC)
        m[i] = bool(p.hot[i]) and (i - lo) >= MIN_PRIOR_SNAPS and (p.liq[i] >= LIQ_FLOOR)
    return m


def c4_trigger(p: Pair) -> np.ndarray:
    n = len(p.t); m = np.zeros(n, bool)
    for i in range(n):
        b, s, v = p.buys[i], p.sells[i], p.vm5[i]
        if math.isnan(b) or math.isnan(s) or math.isnan(v):
            continue
        tot = b + s
        if tot <= 0 or tot < TXNS_M5_MIN:
            continue
        if b / tot < BUY_SHARE_MIN:
            continue
        lo = np.searchsorted(p.t, p.t[i] - LOOKBACK_SEC)
        hist = p.vm5[lo:i]; hist = hist[~np.isnan(hist)]
        if len(hist) < MIN_PRIOR_SNAPS:
            continue
        med = float(np.median(hist))
        if med <= 0:
            continue
        if v >= VOL_MULT * med:
            m[i] = True
    return m


# ---------------- trades ----------------
def tier_cost(liq: float) -> float:
    if liq > 1_000_000.0:
        return 0.006
    if liq >= LIQ_FLOOR:
        return 0.02
    return float("nan")  # unreachable: entry floor is 50k


def simulate(p: Pair, j: int, flags, end_ts: int) -> Optional[dict]:
    """Entry at snapshot j. Returns None if no full hold window inside data."""
    U, Lany, Lrel, J = flags
    bad = U | Lany | J
    e = p.px[j]; t0 = p.t[j]
    if t0 + HOLD_SEC > end_ts:
        return None
    res = {}
    # tp10/sl5/h60
    x = None; why = "hold"; last = e
    for k in range(j + 1, len(p.t)):
        if p.t[k] > t0 + HOLD_SEC:
            break
        if Lrel[k]:
            x, why = 0.0, "collapse"; break
        if bad[k]:
            continue
        last = p.px[k]
        if last <= e * (1 - SL):
            x, why = last, "sl"; break
        if last >= e * (1 + TP):
            x, why = e * (1 + TP), "tp"; break
    if x is None:
        x = last
    res["tp10_sl5_h60"] = (x / e - 1.0, why)
    # time60
    x = None; why = "time"; last = e
    for k in range(j + 1, len(p.t)):
        if p.t[k] > t0 + HOLD_SEC:
            break
        if Lrel[k]:
            x, why = 0.0, "collapse"; break
        if bad[k]:
            continue
        last = p.px[k]
    if x is None:
        x = last
    res["time60"] = (x / e - 1.0, why)
    return res


def build_events(pairs: list[Pair], kind: str, end_ts: int, seed: int = RANDOM_SEED) -> tuple[list[dict], dict]:
    """kind='c4' or 'random'. Applies: pool, trigger, next-snapshot entry, liq floor at entry,
    U/L/J entry skip, full hold window, then token 60-min dedup (earliest)."""
    rng = np.random.default_rng(seed)
    cand = []; stats = {"triggers": 0, "invalid_gap": 0, "ulj_skip": 0, "no_next": 0,
                        "entry_liq_fail": 0, "no_full_hold": 0}
    for p in sorted(pairs, key=lambda q: q.pair):
        if len(p.t) < 2:
            continue
        pm = pool_mask(p)
        if kind == "c4":
            trig = pm & c4_trigger(p)
        else:
            draw = rng.random(len(p.t)) < RANDOM_P
            trig = pm & draw
        idx = np.where(trig)[0]
        if not len(idx):
            continue
        stats["triggers"] += int(len(idx))
        flags = ulj_flags(p)
        bad = flags[0] | flags[1] | flags[3]
        for i in idx:
            j = i + 1
            if j >= len(p.t):
                stats["no_next"] += 1; continue
            if p.t[j] - p.t[i] > MAX_ENTRY_GAP_SEC:
                stats["invalid_gap"] += 1; continue
            if not (p.liq[j] >= LIQ_FLOOR):
                stats["entry_liq_fail"] += 1; continue
            if bad[j]:
                stats["ulj_skip"] += 1; continue
            r = simulate(p, j, flags, end_ts)
            if r is None:
                stats["no_full_hold"] += 1; continue
            cand.append(dict(token=p.token, pair=p.pair, trigger_ts=int(p.t[i]), entry_ts=int(p.t[j]),
                             entry_price=float(p.px[j]), entry_liq=float(p.liq[j]),
                             cost=tier_cost(float(p.liq[j])),
                             **{f"ret_{k}": v[0] for k, v in r.items()},
                             **{f"why_{k}": v[1] for k, v in r.items()}))
    cand.sort(key=lambda d: (d["token"], d["trigger_ts"]))
    out = []; last: dict = {}
    for d in cand:
        lt = last.get(d["token"])
        if lt is not None and d["trigger_ts"] - lt < DEDUP_SEC:
            continue
        last[d["token"]] = d["trigger_ts"]; out.append(d)
    out.sort(key=lambda d: d["trigger_ts"])
    return out, stats


def cut_ts() -> int:
    """Fixed calendar cut (2026-10-23 02:15:00 HKT), identical for C4 and random."""
    return _ts(CUT_ISO)


def gap_report(stats: dict) -> dict:
    trig = stats.get("triggers", 0); bad = stats.get("invalid_gap", 0)
    frac = (bad / trig) if trig else 0.0
    return {"triggers": trig, "invalid_gap": bad, "invalid_gap_frac": frac,
            "warning": bool(frac > GAP_WARN_FRAC)}


def segment(events: list[dict], cut: float, seg: str) -> list[dict]:
    """train: trigger_ts <= cut; oos: trigger_ts > cut."""
    return [e for e in events if (e["trigger_ts"] <= cut) == (seg == "train")]


# ---------------- stats ----------------
def net(e: dict, ex: str, mult: float = 1.0) -> float:
    return (1 + e[f"ret_{ex}"]) * (1 - mult * e["cost"]) - 1


def _cluster(events, vals):
    tok: dict = {}
    for e, v in zip(events, vals):
        s = tok.setdefault(e["token"], [0.0, 0]); s[0] += v; s[1] += 1
    a = np.array(list(tok.values()), dtype=float)
    return a[:, 0], a[:, 1]


def cluster_boot_means(events, vals, n_boot=N_BOOT, seed=RANDOM_SEED):
    s, c = _cluster(events, vals)
    rng = np.random.default_rng(seed); k = len(s)
    ix = rng.integers(0, k, size=(n_boot, k))
    return s[ix].sum(1) / c[ix].sum(1)


def cluster_ci(events, vals, n_boot=N_BOOT, seed=RANDOM_SEED):
    m = cluster_boot_means(events, vals, n_boot, seed)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))


def diff_pvalue(ev_a, va, ev_b, vb, n_boot=N_BOOT, seed=RANDOM_SEED):
    """One-sided p for mean(a) - mean(b) <= 0, token-clustered, independent resampling."""
    ma = cluster_boot_means(ev_a, va, n_boot, seed)
    mb = cluster_boot_means(ev_b, vb, n_boot, seed + 1)
    d = ma - mb
    return float((d <= 0).mean()), (float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5)))


def bonferroni_alpha(cells: int) -> float:
    if cells < 1:
        raise ValueError("cells must be >= 1")
    return FAMILY_ALPHA / cells


def valid(events) -> bool:
    return len(events) >= MIN_EVENTS and len({e["token"] for e in events}) >= MIN_TOKENS


def summarize(events, ex):
    if not events:
        return {"n": 0}
    pre = [e[f"ret_{ex}"] for e in events]
    n1 = [net(e, ex) for e in events]; n2 = [net(e, ex, COST_MULT_SENS) for e in events]
    srt = np.sort(n1); w = srt[:max(1, int(len(srt) * 0.05))]
    tokpnl: dict = {}
    for e, v in zip(events, n1):
        tokpnl[e["token"]] = tokpnl.get(e["token"], 0.0) + v
    absv = sorted((abs(v) for v in tokpnl.values()), reverse=True)
    return dict(n=len(events), tokens=len(tokpnl),
                pre_mean=float(np.mean(pre)), pre_median=float(np.median(pre)), pre_ci=cluster_ci(events, pre),
                net_mean=float(np.mean(n1)), net_median=float(np.median(n1)), net_ci=cluster_ci(events, n1),
                net2x_mean=float(np.mean(n2)), net2x_ci=cluster_ci(events, n2),
                worst5_mean=float(w.mean()), max_loss=float(srt[0]),
                collapse=int(sum(e[f"why_{ex}"] == "collapse" for e in events)),
                top5_share=float(sum(absv[:5]) / sum(absv)) if sum(absv) > 0 else float("nan"))


def evaluate(c4_seg, rnd_seg, seg: str, cells: int) -> dict:
    alpha = bonferroni_alpha(cells)
    res = {"segment": seg, "cells": cells, "alpha_one_sided": alpha,
           "valid_c4": valid(c4_seg), "n_c4": len(c4_seg), "n_random": len(rnd_seg), "exits": {}}
    for ex in EXITS:
        r = {"c4": summarize(c4_seg, ex), "random": summarize(rnd_seg, ex)}
        if c4_seg and rnd_seg:
            p, dci = diff_pvalue(c4_seg, [net(e, ex) for e in c4_seg], rnd_seg, [net(e, ex) for e in rnd_seg])
            r["diff_p_one_sided"] = p; r["diff_ci"] = dci
        if not res["valid_c4"]:
            r["pass"] = False; r["verdict"] = "INVALID (<30 events or tokens)"
        elif seg == "train":
            ok = r.get("diff_p_one_sided", 1.0) < alpha and r["c4"]["net_mean"] > 0
            r["pass"] = bool(ok); r["verdict"] = "TRAIN_PASS" if ok else "TRAIN_FAIL"
        else:
            c = r["c4"]
            ok = c["net_mean"] > 0 and c["net_ci"][0] > 0 and c["net2x_mean"] >= 0
            r["pass"] = bool(ok); r["verdict"] = "OOS_PASS" if ok else "OOS_FAIL"
        res["exits"][ex] = r
    return res


# ---------------- CLI ----------------
def run(db: str, seg: str, cells: int, out_dir: str, train_result: Optional[str] = None,
        start: str = WINDOW_START, end: str = WINDOW_END, skip_time_check: bool = False,
        now_ts: Optional[int] = None) -> dict:
    end_ts = _ts(end)
    now = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    if not skip_time_check and now < end_ts:
        raise SystemExit(f"refuse: window not closed (ends {end}); blinding until then")
    os.makedirs(out_dir, exist_ok=True)
    marker = os.path.join(out_dir, "c4_oos.lock")
    if seg == "oos":
        if os.path.exists(marker):
            raise SystemExit(f"refuse: OOS already run (marker {marker})")
        if not train_result or not os.path.exists(train_result):
            raise SystemExit("refuse: --train-result required for OOS")
        with open(train_result) as fh:
            tr = json.load(fh)
        if not any(v.get("pass") for v in tr["exits"].values()):
            raise SystemExit("refuse: no exit passed train; OOS must not be looked at")
        with open(marker, "x") as fh:
            fh.write(datetime.now(timezone.utc).isoformat())
    con = open_ro(db)
    try:
        pairs = load_pairs(con, start, end)
    finally:
        con.close()
    c4, s_c4 = build_events(pairs, "c4", end_ts)
    rnd, s_rnd = build_events(pairs, "random", end_ts)
    cut = cut_ts()
    if not c4:
        res = {"segment": seg, "verdict": "INVALID (no C4 events)", "exits": {}}
    else:
        res = evaluate(segment(c4, cut, seg), segment(rnd, cut, seg), seg, cells)
    res["cut_ts"] = cut
    res["cut_iso"] = datetime.fromtimestamp(cut, timezone.utc).isoformat()
    res["filter_stats"] = {"c4": s_c4, "random": s_rnd}
    gr = {"c4": gap_report(s_c4), "random": gap_report(s_rnd)}
    res["entry_gap"] = gr
    res["warnings"] = [f"WARNING: {k} invalid_gap {v['invalid_gap']}/{v['triggers']} = {v['invalid_gap_frac']:.2%} > {GAP_WARN_FRAC:.0%}"
                       for k, v in gr.items() if v["warning"]]
    path = os.path.join(out_dir, f"c4_{seg}_result.json")
    with open(path, "w") as fh:
        json.dump(res, fh, indent=2, default=float)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--segment", choices=["train", "oos"], required=True)
    ap.add_argument("--cells", type=int, required=True, help="cumulative cell count at lock (Bonferroni)")
    ap.add_argument("--out", default="out")
    ap.add_argument("--train-result")
    a = ap.parse_args(argv)
    res = run(a.db, a.segment, a.cells, a.out, a.train_result)
    print(json.dumps({k: res.get(k) for k in ("segment", "cut_iso", "alpha_one_sided", "n_c4", "n_random")}, default=float))
    for ex, r in res.get("exits", {}).items():
        print(ex, r.get("verdict"))
    print("entry_gap", json.dumps(res.get("entry_gap"), default=float))
    for w in res.get("warnings", []):
        print(w)


if __name__ == "__main__":
    main()
