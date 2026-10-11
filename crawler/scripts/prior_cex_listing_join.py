#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_cex_listing_join.py — CEX listings × 链上先验联合归因（QUANT/方案2 Step 2）

用法:
  .venv/bin/python scripts/prior_cex_listing_join.py
  .venv/bin/python scripts/prior_cex_listing_join.py --no-merge-baseline

输入 (全部只读):
  ccxt.db              → ccxt_listings / ccxt_ohlcv
  data/prior/db/prior.db → token_ohlcv / baseline_history (对照)
  dexscreener.db       → pairs / pair_history（symbol→mint 匹配）
  data/prior/exports/baseline_params.json → 基线对照（读）

输出 (只写 prior exports):
  data/prior/exports/cex_prior_join_YYYYMMDD.md
  data/prior/exports/cex_prior_join_YYYYMMDD.json
  可选 merge baseline_params.json["cex_listing_join"]（不删其它键）

口径:
  - CEX first_seen = ccxt_listings.first_seen_ts（ms）= 旁路首次见到该所 symbol，
    **≠ 交易所官方上线日**（侧信道冷启动会把存量主流也记成 first_seen）
  - base = symbol 的 '/' 左侧 upper；报价侧 TRY/BRL/USDC/USDT 等仅作 quote 标签
  - 链上匹配: pairs.raw_json.baseToken.symbol 精确 upper 相等；
    多撞车时取 liquidity.usd 最高的 pair 作 canonical，并报告 collision_n
  - prior.token_ohlcv: upper(ticker) 精确匹配（可多行，取 discovery_ts 最早）
  - 延迟: cex_first_seen_ms − pair_created_at_ms（或 discovery_ts*1000）
  - 上线后短窗收益:
      ccxt_ohlcv: 以 first_seen 为 t0，取 +15m/+1h/+4h close（容差 10min）
      pair_history: 以 matched pair 在 t0 后的 price_usd 作同类收益
  - 对照 baseline: graduation / same_day_death / price_behavior pump — 只并列，不调权

原则: 默认关（one-shot）；不写业务四库 / INDICATORS_WEIGHT_* / trading / alerts；
      不接 review 周报（留给复盘）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
try:
    sys.path.insert(0, ROOT)
    from config import DB_PATH as _CFG_DB

    DEX_DB = _CFG_DB if os.path.isabs(_CFG_DB) else os.path.join(ROOT, _CFG_DB)
except Exception:
    DEX_DB = os.path.join(ROOT, "dexscreener.db")

CCXT_DB = os.environ.get("CCXT_DB_PATH") or os.path.join(ROOT, "ccxt.db")
if not os.path.isabs(CCXT_DB):
    CCXT_DB = os.path.join(ROOT, CCXT_DB)

PRIOR_BASE = os.path.join(ROOT, "data", "prior")
PRIOR_DB = os.path.join(PRIOR_BASE, "db", "prior.db")
OUT = os.path.join(PRIOR_BASE, "exports")
BASELINE_JSON = os.path.join(OUT, "baseline_params.json")
os.makedirs(OUT, exist_ok=True)

HK = timezone(timedelta(hours=8))
DATE = datetime.now(HK).strftime("%Y%m%d")

# Quote / fiat / stable bases that are not "new token listing" thesis targets
QUOTE_LIKE = {
    "USDT",
    "USDC",
    "USD",
    "DAI",
    "FDUSD",
    "TUSD",
    "BUSD",
    "EUR",
    "TRY",
    "BRL",
    "GBP",
    "JPY",
}
# Known mega-caps / L1s — symbol collision with wrapped / meme clones is severe
MEGA_CAPS = {
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "DOGE",
    "TRX",
    "LINK",
    "DOT",
    "UNI",
    "AAVE",
    "NEAR",
    "SUI",
    "SEI",
    "TAO",
    "TIA",
    "WLD",
    "STRK",
    "ZEC",
    "XMR",
    "CFX",
    "HYPE",
}

HORIZONS = {
    "15m": 15 * 60,
    "1h": 3600,
    "4h": 4 * 3600,
}
OHLCV_TOL_MS = 10 * 60 * 1000  # 10 min
PAIR_HIST_TOL_SEC = 20 * 60


def open_ro(path: str) -> sqlite3.Connection:
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(HK).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def ms_to_iso(ms: Optional[int | float]) -> Optional[str]:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except Exception:
        return None


def sec_to_iso(sec: Optional[int | float]) -> Optional[str]:
    if sec is None:
        return None
    try:
        return datetime.fromtimestamp(float(sec), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except Exception:
        return None


def parse_iso(s: Optional[str]) -> Optional[datetime]:
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


def pctile(sorted_vals: list[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if p <= 0:
        return sorted_vals[0]
    if p >= 1:
        return sorted_vals[-1]
    idx = min(len(sorted_vals) - 1, int(p * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[idx]


def summarize(vals: list[float], nd: int = 4) -> dict[str, Any]:
    if not vals:
        return {"n": 0, "median": None, "p25": None, "p75": None, "p90": None, "mean": None}
    s = sorted(vals)

    def r(x: Optional[float]) -> Optional[float]:
        return None if x is None else round(float(x), nd)

    return {
        "n": len(s),
        "median": r(pctile(s, 0.50)),
        "p25": r(pctile(s, 0.25)),
        "p75": r(pctile(s, 0.75)),
        "p90": r(pctile(s, 0.90)),
        "mean": r(sum(s) / len(s)),
    }


def normalize_symbol(symbol: str) -> dict[str, Any]:
    sym = (symbol or "").strip()
    parts = sym.split("/")
    base = parts[0].upper().strip() if parts else ""
    quote = parts[1].upper().strip() if len(parts) > 1 else ""
    # strip common leverage / settle suffixes on base (e.g. none for spot)
    base_clean = base
    for suf in (":USDT", ":USD", "-SWAP"):
        if base_clean.endswith(suf):
            base_clean = base_clean[: -len(suf)]
    is_quote_like = base_clean in QUOTE_LIKE
    is_mega = base_clean in MEGA_CAPS
    return {
        "symbol": sym,
        "base": base_clean,
        "quote": quote,
        "is_quote_like_base": is_quote_like,
        "is_mega_cap": is_mega,
        "match_eligible": bool(base_clean) and not is_quote_like,
    }


def load_listings(ccxt: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = ccxt.execute(
        """
        SELECT id, exchange, symbol, first_seen_ts, first_price, note
        FROM ccxt_listings
        ORDER BY first_seen_ts ASC, id ASC
        """
    ).fetchall()
    out = []
    for r in rows:
        norm = normalize_symbol(r["symbol"])
        out.append(
            {
                "listing_id": r["id"],
                "exchange": r["exchange"],
                "symbol": r["symbol"],
                "first_seen_ts": int(r["first_seen_ts"]),
                "first_seen_iso": ms_to_iso(r["first_seen_ts"]),
                "first_price": r["first_price"],
                "note": r["note"],
                **norm,
            }
        )
    return out


def build_pair_symbol_index(dex: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Map UPPER(baseToken.symbol) → list of pair candidates."""
    rows = dex.execute(
        """
        SELECT chain_id, pair_address, token_address, pair_created_at,
               json_extract(raw_json, '$.baseToken.symbol') AS sym,
               json_extract(raw_json, '$.baseToken.name') AS name,
               CAST(json_extract(raw_json, '$.liquidity.usd') AS REAL) AS liq,
               CAST(json_extract(raw_json, '$.priceUsd') AS REAL) AS price_usd
        FROM pairs
        WHERE json_extract(raw_json, '$.baseToken.symbol') IS NOT NULL
        """
    ).fetchall()
    idx: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sym = (r["sym"] or "").strip().upper()
        if not sym:
            continue
        idx.setdefault(sym, []).append(
            {
                "chain_id": r["chain_id"],
                "pair_address": r["pair_address"],
                "token_address": r["token_address"],
                "pair_created_at": r["pair_created_at"],
                "name": r["name"],
                "liquidity_usd": r["liq"],
                "price_usd": r["price_usd"],
                "symbol": sym,
            }
        )
    return idx


def pick_canonical_pair(cands: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not cands:
        return None
    # Prefer highest liquidity; tie-break earliest pair_created_at
    def key(c: dict[str, Any]) -> tuple:
        liq = c.get("liquidity_usd")
        liq_key = float(liq) if liq is not None else -1.0
        created = c.get("pair_created_at")
        created_key = int(created) if created is not None else 2**63 - 1
        return (-liq_key, created_key)

    return sorted(cands, key=key)[0]


def match_prior_ohlcv(
    prior: sqlite3.Connection, base: str
) -> tuple[Optional[dict[str, Any]], int]:
    rows = prior.execute(
        """
        SELECT token_address, ticker, discovery_ts, death_reason,
               peak_return, first_price, last_price, n_bars, peak_ts
        FROM token_ohlcv
        WHERE upper(ticker) = ?
        ORDER BY discovery_ts ASC NULLS LAST
        """,
        (base,),
    ).fetchall()
    if not rows:
        return None, 0
    r = rows[0]
    return (
        {
            "token_address": r["token_address"],
            "ticker": r["ticker"],
            "discovery_ts": r["discovery_ts"],
            "discovery_iso": sec_to_iso(r["discovery_ts"]),
            "death_reason": r["death_reason"],
            "peak_return": r["peak_return"],
            "first_price": r["first_price"],
            "last_price": r["last_price"],
            "n_bars": r["n_bars"],
            "peak_ts": r["peak_ts"],
        },
        len(rows),
    )


def ccxt_forward_returns(
    ccxt: sqlite3.Connection,
    exchange: str,
    symbol: str,
    t0_ms: int,
) -> dict[str, Any]:
    """Short-horizon returns from ccxt_ohlcv using bar nearest to t0 as p0."""
    bars = ccxt.execute(
        """
        SELECT ts, open, high, low, close, volume
        FROM ccxt_ohlcv
        WHERE exchange = ? AND symbol = ?
        ORDER BY ts ASC
        """,
        (exchange, symbol),
    ).fetchall()
    if not bars:
        return {"available": False, "reason": "no_ohlcv", "returns": {}}

    # p0: bar closest to t0 (prefer at/before)
    best = None
    best_dt = None
    for b in bars:
        dt = abs(int(b["ts"]) - t0_ms)
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = b
    if best is None or best["close"] is None or best["close"] <= 0:
        return {"available": False, "reason": "no_p0", "returns": {}}

    p0 = float(best["close"])
    t0_bar = int(best["ts"])
    out_rets: dict[str, Any] = {}
    n_after = sum(1 for b in bars if int(b["ts"]) > t0_ms)

    for name, sec in HORIZONS.items():
        target = t0_ms + sec * 1000
        cand = None
        cand_dt = None
        for b in bars:
            ts = int(b["ts"])
            if ts < t0_ms:
                continue
            dt = abs(ts - target)
            if dt <= OHLCV_TOL_MS and (cand_dt is None or dt < cand_dt):
                cand_dt = dt
                cand = b
        if cand is None:
            # fallback: last bar at/after t0 if span covers
            after = [b for b in bars if int(b["ts"]) >= t0_ms]
            if after and (int(after[-1]["ts"]) - t0_ms) >= sec * 1000 - 1:
                cand = after[-1]
                method = "last"
            else:
                out_rets[name] = {
                    "ret": None,
                    "method": "none",
                    "note": "insufficient post-listing bars",
                }
                continue
        else:
            method = "nearest"
        px = float(cand["close"]) if cand["close"] is not None else None
        if px is None or px <= 0:
            out_rets[name] = {"ret": None, "method": "none"}
            continue
        out_rets[name] = {
            "ret": round(px / p0 - 1.0, 6),
            "method": method,
            "p0": p0,
            "p_h": px,
            "t0_bar_ts": t0_bar,
            "h_bar_ts": int(cand["ts"]),
        }

    return {
        "available": True,
        "p0": p0,
        "t0_bar_ts": t0_bar,
        "t0_bar_delta_ms": best_dt,
        "n_bars": len(bars),
        "n_bars_after_listing": n_after,
        "returns": out_rets,
        "note": (
            "ccxt_ohlcv often ends at/before first_seen when sidechannel just started; "
            "forward n may be 0"
            if n_after == 0
            else None
        ),
    }


def pair_history_forward_returns(
    dex: sqlite3.Connection,
    pair_address: str,
    t0_ms: int,
) -> dict[str, Any]:
    rows = dex.execute(
        """
        SELECT recorded_at, price_usd
        FROM pair_history
        WHERE lower(pair_address) = lower(?)
          AND price_usd IS NOT NULL AND price_usd > 0
        ORDER BY recorded_at ASC
        """,
        (pair_address,),
    ).fetchall()
    if not rows:
        return {"available": False, "reason": "no_pair_history", "returns": {}}

    points: list[tuple[datetime, float]] = []
    for r in rows:
        dt = parse_iso(r["recorded_at"])
        if dt is None:
            continue
        points.append((dt, float(r["price_usd"])))
    if not points:
        return {"available": False, "reason": "unparseable", "returns": {}}

    t0 = datetime.fromtimestamp(t0_ms / 1000.0, tz=timezone.utc)

    # p0: nearest within tol, prefer at/before t0; else first after
    p0 = None
    t_p0 = None
    best_before = None
    best_before_dt = None
    best_any = None
    best_any_dt = None
    for t, p in points:
        dt = abs((t - t0).total_seconds())
        if t <= t0 and (best_before_dt is None or dt < best_before_dt):
            best_before_dt = dt
            best_before = (t, p)
        if best_any_dt is None or dt < best_any_dt:
            best_any_dt = dt
            best_any = (t, p)
    if best_before is not None and best_before_dt is not None and best_before_dt <= PAIR_HIST_TOL_SEC:
        t_p0, p0 = best_before
    elif best_any is not None and best_any_dt is not None and best_any_dt <= PAIR_HIST_TOL_SEC * 3:
        t_p0, p0 = best_any
    else:
        # no price near listing — cannot attribute post-listing return
        after = [(t, p) for t, p in points if t >= t0]
        if not after:
            return {
                "available": False,
                "reason": "no_price_near_or_after_listing",
                "n_points": len(points),
                "returns": {},
            }
        t_p0, p0 = after[0]

    out_rets: dict[str, Any] = {}
    for name, sec in HORIZONS.items():
        target = t0 + timedelta(seconds=sec)
        cand = None
        cand_dt = None
        for t, p in points:
            if t < t0:
                continue
            dt = abs((t - target).total_seconds())
            if dt <= PAIR_HIST_TOL_SEC and (cand_dt is None or dt < cand_dt):
                cand_dt = dt
                cand = (t, p)
        method = "nearest"
        if cand is None:
            after = [(t, p) for t, p in points if t >= t0]
            if after and (after[-1][0] - t0).total_seconds() >= sec - 1:
                cand = after[-1]
                method = "last"
            else:
                out_rets[name] = {"ret": None, "method": "none"}
                continue
        out_rets[name] = {
            "ret": round(cand[1] / p0 - 1.0, 6),
            "method": method,
            "p0": p0,
            "p_h": cand[1],
            "t0_iso": t_p0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "h_iso": cand[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    return {
        "available": True,
        "p0": p0,
        "t0_iso": t_p0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_points": len(points),
        "returns": out_rets,
    }


def load_baseline_refs() -> dict[str, Any]:
    if not os.path.isfile(BASELINE_JSON):
        return {}
    try:
        with open(BASELINE_JSON, encoding="utf-8") as f:
            bp = json.load(f)
    except Exception:
        return {}
    trio = bp.get("weekly_baseline_trio") or {}
    pb = bp.get("price_behavior") or {}
    gmgn = bp.get("gmgn") or {}
    return {
        "graduation_rate": trio.get("graduation_rate"),
        "same_day_death_rate": trio.get("same_day_death_rate"),
        "solrpds_remove_gt_add_rate": trio.get("solrpds_remove_gt_add_rate"),
        "gmgn_median_peak_return_x": gmgn.get("median_peak_return_x"),
        "gmgn_median_peak_min": gmgn.get("median_peak_min"),
        "price_behavior_ret_1h_median": (pb.get("returns") or {}).get("1h", {}).get("median"),
        "price_behavior_pump_4h_multiple_p50": (pb.get("pump_4h") or {}).get("multiple_p50"),
        "source_file": os.path.relpath(BASELINE_JSON, ROOT),
    }


def run_join(
    ccxt_path: str,
    dex_path: str,
    prior_path: str,
) -> dict[str, Any]:
    warnings: list[str] = []
    for label, path in (
        ("ccxt.db", ccxt_path),
        ("dexscreener.db", dex_path),
        ("prior.db", prior_path),
    ):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"{label} not found: {path}")

    ccxt = open_ro(ccxt_path)
    dex = open_ro(dex_path)
    prior = open_ro(prior_path)

    listings = load_listings(ccxt)
    n_listings = len(listings)
    if n_listings <= 500:
        warnings.append(
            f"small n={n_listings} ccxt_listings；统计仅观察，勿外推为上市溢价先验"
        )
    warnings.append(
        "ccxt_listings.first_seen_ts = 旁路首次见到，≠ CEX 官方上线日；"
        "冷启动会把 BTC/ETH 等存量也记入"
    )
    warnings.append(
        "symbol 精确匹配存在严重撞车（同 ticker 多 mint）；canonical = 最高流动性 pair"
    )

    print("[INFO] building pair symbol index…")
    pair_idx = build_pair_symbol_index(dex)
    print(f"[INFO] distinct pair symbols indexed: {len(pair_idx)}")

    rows_out: list[dict[str, Any]] = []
    matched_pair = 0
    matched_prior = 0
    matched_either = 0
    unmatched: list[dict[str, Any]] = []
    skipped_quote_like = 0

    latencies_h_pair: list[float] = []
    latencies_h_prior: list[float] = []
    collision_ns: list[int] = []

    cex_ret_collect: dict[str, list[float]] = {h: [] for h in HORIZONS}
    pair_ret_collect: dict[str, list[float]] = {h: [] for h in HORIZONS}

    for L in listings:
        base = L["base"]
        rec: dict[str, Any] = {
            "listing_id": L["listing_id"],
            "exchange": L["exchange"],
            "symbol": L["symbol"],
            "base": base,
            "quote": L["quote"],
            "first_seen_ts": L["first_seen_ts"],
            "first_seen_iso": L["first_seen_iso"],
            "is_quote_like_base": L["is_quote_like_base"],
            "is_mega_cap": L["is_mega_cap"],
            "match_eligible": L["match_eligible"],
        }

        if not L["match_eligible"]:
            skipped_quote_like += 1
            rec["match_status"] = "skipped_quote_like_base"
            rows_out.append(rec)
            unmatched.append(
                {
                    "exchange": L["exchange"],
                    "symbol": L["symbol"],
                    "base": base,
                    "reason": "quote_like_base",
                }
            )
            continue

        cands = pair_idx.get(base) or []
        collision_n = len(cands)
        unique_mints = len({(c["chain_id"], c["token_address"]) for c in cands})
        rec["pair_collision_n"] = collision_n
        rec["pair_unique_mints"] = unique_mints
        if collision_n:
            collision_ns.append(collision_n)

        canon = pick_canonical_pair(cands)
        if canon:
            matched_pair += 1
            rec["pair_match"] = {
                "chain_id": canon["chain_id"],
                "pair_address": canon["pair_address"],
                "token_address": canon["token_address"],
                "name": canon["name"],
                "liquidity_usd": canon["liquidity_usd"],
                "pair_created_at": canon["pair_created_at"],
                "pair_created_iso": ms_to_iso(canon["pair_created_at"]),
                "price_usd": canon["price_usd"],
            }
            if canon["pair_created_at"] is not None:
                lat_ms = L["first_seen_ts"] - int(canon["pair_created_at"])
                lat_h = lat_ms / 3_600_000.0
                rec["latency_pair_h"] = round(lat_h, 4)
                rec["latency_pair_d"] = round(lat_h / 24.0, 4)
                latencies_h_pair.append(lat_h)
        else:
            rec["pair_match"] = None

        prior_hit, prior_n = match_prior_ohlcv(prior, base)
        rec["prior_ohlcv_collision_n"] = prior_n
        if prior_hit:
            matched_prior += 1
            rec["prior_ohlcv_match"] = prior_hit
            if prior_hit.get("discovery_ts") is not None:
                lat_ms = L["first_seen_ts"] - int(prior_hit["discovery_ts"]) * 1000
                lat_h = lat_ms / 3_600_000.0
                rec["latency_prior_h"] = round(lat_h, 4)
                latencies_h_prior.append(lat_h)
        else:
            rec["prior_ohlcv_match"] = None

        if canon or prior_hit:
            matched_either += 1
            rec["match_status"] = "matched"
        else:
            rec["match_status"] = "unmatched"
            unmatched.append(
                {
                    "exchange": L["exchange"],
                    "symbol": L["symbol"],
                    "base": base,
                    "reason": "no_pair_no_prior_ticker",
                }
            )

        # Returns
        rec["cex_ohlcv_returns"] = ccxt_forward_returns(
            ccxt, L["exchange"], L["symbol"], L["first_seen_ts"]
        )
        for h, info in (rec["cex_ohlcv_returns"].get("returns") or {}).items():
            if info.get("ret") is not None:
                cex_ret_collect[h].append(float(info["ret"]))

        if canon:
            rec["pair_history_returns"] = pair_history_forward_returns(
                dex, canon["pair_address"], L["first_seen_ts"]
            )
            for h, info in (rec["pair_history_returns"].get("returns") or {}).items():
                if info.get("ret") is not None:
                    pair_ret_collect[h].append(float(info["ret"]))
        else:
            rec["pair_history_returns"] = {"available": False, "reason": "no_pair_match"}

        rows_out.append(rec)

    eligible = n_listings - skipped_quote_like
    match_rate_pair = (matched_pair / eligible) if eligible else 0.0
    match_rate_prior = (matched_prior / eligible) if eligible else 0.0
    match_rate_either = (matched_either / eligible) if eligible else 0.0
    match_rate_all = (matched_either / n_listings) if n_listings else 0.0

    baseline_refs = load_baseline_refs()

    # Qualitative compare notes (observe only)
    compare_notes = [
        "CEX 旁路 listings 当前以主流/高流动性现货为主，与 memecoin prior "
        f"(graduation≈{baseline_refs.get('graduation_rate')}, "
        f"same_day_death≈{baseline_refs.get('same_day_death_rate')}) "
        "不在同一抽样总体；并列对照即可，不可直接做 uplift。",
        "链上同 ticker 撞车严重时，latency(pair_created→cex) 对 mega-cap 无因果意义"
        "（wrapped/clone pair 的 created_at ≠ 资产诞生）。",
        "post-listing 短窗收益依赖 sidechannel 持续拉取；冷启动当下 forward bars 常为 0。",
    ]

    payload: dict[str, Any] = {
        "meta": {
            "date": DATE,
            "generated_at": now_iso(),
            "step": "QUANT/方案2 Step 2",
            "observe_only": True,
            "default_off": True,
            "no_weight_change": True,
            "ccxt_db": os.path.relpath(ccxt_path, ROOT),
            "dex_db": os.path.relpath(dex_path, ROOT),
            "prior_db": os.path.relpath(prior_path, ROOT),
            "symbol_match": "upper(base of CEX symbol) == upper(pairs.baseToken.symbol)",
            "canonical_pair": "max liquidity.usd, tie-break earliest pair_created_at",
            "warnings": warnings,
        },
        "summary": {
            "n_listings": n_listings,
            "n_eligible_non_quote_base": eligible,
            "n_skipped_quote_like_base": skipped_quote_like,
            "n_matched_pair": matched_pair,
            "n_matched_prior_ohlcv": matched_prior,
            "n_matched_either": matched_either,
            "n_unmatched_eligible": eligible - matched_either,
            "match_rate_pair_eligible": round(match_rate_pair, 4),
            "match_rate_prior_eligible": round(match_rate_prior, 4),
            "match_rate_either_eligible": round(match_rate_either, 4),
            "match_rate_either_all_listings": round(match_rate_all, 4),
            "unique_bases": sorted({L["base"] for L in listings}),
            "exchanges": sorted({L["exchange"] for L in listings}),
        },
        "latency": {
            "pair_created_to_cex_h": summarize(latencies_h_pair),
            "prior_discovery_to_cex_h": summarize(latencies_h_prior),
            "unit": "hours (positive => chain earlier than cex first_seen)",
            "caveat": "mega-cap / collision 样本 latency 不可解释为上市延迟",
        },
        "collisions": {
            "pair_collision_n": summarize([float(x) for x in collision_ns], nd=2),
            "note": "collision_n = #pairs with same baseToken.symbol",
        },
        "returns_after_cex_first_seen": {
            "from_ccxt_ohlcv": {h: summarize(cex_ret_collect[h], nd=6) for h in HORIZONS},
            "from_pair_history": {h: summarize(pair_ret_collect[h], nd=6) for h in HORIZONS},
            "horizons_sec": HORIZONS,
        },
        "baseline_compare": {
            "refs": baseline_refs,
            "notes": compare_notes,
        },
        "unmatched": unmatched,
        "rows": rows_out,
    }

    ccxt.close()
    dex.close()
    prior.close()
    return payload


def build_markdown(payload: dict[str, Any]) -> str:
    s = payload["summary"]
    lat = payload["latency"]
    rets_c = payload["returns_after_cex_first_seen"]["from_ccxt_ohlcv"]
    rets_p = payload["returns_after_cex_first_seen"]["from_pair_history"]
    meta = payload["meta"]
    L: list[str] = []
    A = L.append

    A(f"# CEX listings × 链上先验联合归因（Step 2）— {meta['date']}")
    A("")
    A(f"- 生成: `{meta['generated_at']}` (HK)")
    A("- **observe-only / 默认关 / 不调权**")
    A(f"- 输入: `{meta['ccxt_db']}` × `{meta['prior_db']}` × `{meta['dex_db']}`")
    A("")
    A("## 口径")
    A(f"- symbol 匹配: `{meta['symbol_match']}`")
    A(f"- canonical pair: `{meta['canonical_pair']}`")
    A("- `first_seen_ts` = CCXT 旁路首次见到，**≠** 交易所官方上线日")
    A("- quote-like base（USDT/USDC/TRY…）不计为可匹配新币")
    A("")
    A("## 匹配率")
    A("")
    A("| 指标 | 值 |")
    A("|------|----|")
    A(f"| n_listings | {s['n_listings']} |")
    A(f"| eligible (非 quote-like base) | {s['n_eligible_non_quote_base']} |")
    A(f"| skipped quote-like | {s['n_skipped_quote_like_base']} |")
    A(f"| matched → pairs | {s['n_matched_pair']} ({s['match_rate_pair_eligible']*100:.1f}% eligible) |")
    A(f"| matched → prior.token_ohlcv | {s['n_matched_prior_ohlcv']} ({s['match_rate_prior_eligible']*100:.1f}% eligible) |")
    A(f"| matched either | {s['n_matched_either']} ({s['match_rate_either_eligible']*100:.1f}% eligible) |")
    A(f"| unmatched eligible | {s['n_unmatched_eligible']} |")
    A(f"| exchanges | {', '.join(s['exchanges'])} |")
    A("")
    A("### Unmatched / skipped")
    A("")
    for u in payload.get("unmatched") or []:
        A(f"- `{u['exchange']}` `{u['symbol']}` base=`{u['base']}` — {u['reason']}")
    if not payload.get("unmatched"):
        A("- （无）")
    A("")
    A("## 延迟（链上 → CEX first_seen）")
    A("")
    lp = lat["pair_created_to_cex_h"]
    lo = lat["prior_discovery_to_cex_h"]
    A("| 来源 | n | median (h) | p25 | p75 | p90 |")
    A("|------|---|------------|-----|-----|-----|")
    A(
        f"| pair_created_at → cex | {lp['n']} | {lp['median']} | {lp['p25']} | {lp['p75']} | {lp['p90']} |"
    )
    A(
        f"| prior discovery_ts → cex | {lo['n']} | {lo['median']} | {lo['p25']} | {lo['p75']} | {lo['p90']} |"
    )
    A("")
    A(f"- caveat: {lat['caveat']}")
    A("")
    col = payload["collisions"]["pair_collision_n"]
    A(
        f"- pair symbol collision: n={col['n']} median={col['median']} "
        f"p90={col['p90']} (同 ticker 池数)"
    )
    A("")
    A("## 上线后短窗收益")
    A("")
    A("### ccxt_ohlcv")
    A("")
    A("| horizon | n | median | p25 | p75 |")
    A("|---------|---|--------|-----|-----|")
    for h in ("15m", "1h", "4h"):
        r = rets_c[h]
        A(f"| {h} | {r['n']} | {r['median']} | {r['p25']} | {r['p75']} |")
    A("")
    A("### pair_history（matched canonical pair）")
    A("")
    A("| horizon | n | median | p25 | p75 |")
    A("|---------|---|--------|-----|-----|")
    for h in ("15m", "1h", "4h"):
        r = rets_p[h]
        A(f"| {h} | {r['n']} | {r['median']} | {r['p25']} | {r['p75']} |")
    A("")
    A("## 与 prior 基线对照（只并列）")
    A("")
    refs = payload["baseline_compare"]["refs"]
    A(f"- graduation_rate: `{refs.get('graduation_rate')}`")
    A(f"- same_day_death_rate: `{refs.get('same_day_death_rate')}`")
    A(f"- gmgn median_peak_return_x: `{refs.get('gmgn_median_peak_return_x')}`")
    A(f"- price_behavior ret_1h median: `{refs.get('price_behavior_ret_1h_median')}`")
    A(f"- price_behavior pump_4h multiple p50: `{refs.get('price_behavior_pump_4h_multiple_p50')}`")
    A("")
    for n in payload["baseline_compare"]["notes"]:
        A(f"- {n}")
    A("")
    A("## Warnings")
    A("")
    for w in meta.get("warnings") or []:
        A(f"- ⚠ {w}")
    A("")
    A("## 不做 / 默认关")
    A("- 不写业务四库、不改 INDICATORS_WEIGHT_*、不接 trading / alerts")
    A("- 不自动挂 review 周报（§6b 仍只做 CEX 市场基准；本 join 留给复盘决定是否旁挂）")
    A("- 验证「链上新币 → CEX 上线 → 拉升」需更大 n + 真上线事件源后再谈加权")
    A("")
    return "\n".join(L) + "\n"


def merge_baseline(summary: dict[str, Any]) -> str:
    path = BASELINE_JSON
    existing: dict[str, Any] = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    meta = existing.get("meta") or {}
    sources = list(meta.get("sources") or [])
    if "cex_listing_join" not in sources:
        sources.append("cex_listing_join")
    meta["sources"] = sources
    meta["date"] = DATE
    existing["meta"] = meta
    existing["cex_listing_join"] = summary
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="CEX listings × prior joint attribution (observe-only)")
    ap.add_argument("--ccxt-db", default=CCXT_DB)
    ap.add_argument("--dex-db", default=DEX_DB)
    ap.add_argument("--prior-db", default=PRIOR_DB)
    ap.add_argument(
        "--no-merge-baseline",
        action="store_true",
        help="do not write cex_listing_join into baseline_params.json",
    )
    args = ap.parse_args()

    print("[INFO] running CEX × prior join (read-only DBs)…")
    payload = run_join(args.ccxt_db, args.dex_db, args.prior_db)

    json_path = os.path.join(OUT, f"cex_prior_join_{DATE}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    md = build_markdown(payload)
    md_path = os.path.join(OUT, f"cex_prior_join_{DATE}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    s = payload["summary"]
    compact = {
        "date": DATE,
        "n_listings": s["n_listings"],
        "n_eligible": s["n_eligible_non_quote_base"],
        "match_rate_pair_eligible": s["match_rate_pair_eligible"],
        "match_rate_prior_eligible": s["match_rate_prior_eligible"],
        "match_rate_either_eligible": s["match_rate_either_eligible"],
        "n_matched_pair": s["n_matched_pair"],
        "n_matched_prior_ohlcv": s["n_matched_prior_ohlcv"],
        "latency_pair_h_median": payload["latency"]["pair_created_to_cex_h"]["median"],
        "latency_prior_h_median": payload["latency"]["prior_discovery_to_cex_h"]["median"],
        "cex_ret_1h_n": payload["returns_after_cex_first_seen"]["from_ccxt_ohlcv"]["1h"]["n"],
        "cex_ret_1h_median": payload["returns_after_cex_first_seen"]["from_ccxt_ohlcv"]["1h"][
            "median"
        ],
        "pair_ret_1h_n": payload["returns_after_cex_first_seen"]["from_pair_history"]["1h"]["n"],
        "pair_ret_1h_median": payload["returns_after_cex_first_seen"]["from_pair_history"]["1h"][
            "median"
        ],
        "export_json": os.path.relpath(json_path, ROOT),
        "export_md": os.path.relpath(md_path, ROOT),
        "observe_only": True,
        "default_off": True,
        "no_weight_change": True,
        "weekly_hook": "deferred_to_review",
        "note": "ccxt_listings.first_seen≠official listing; small-n; symbol collision",
    }

    baseline_path = None
    if not args.no_merge_baseline:
        baseline_path = merge_baseline(compact)

    print(md)
    print("[OUT]", json_path)
    print("[OUT]", md_path)
    if baseline_path:
        print("[OUT]", baseline_path, "(merged cex_listing_join)")
    print(
        f"[KEY] listings={s['n_listings']} eligible={s['n_eligible_non_quote_base']} "
        f"pair_match={s['n_matched_pair']} ({s['match_rate_pair_eligible']*100:.1f}%) "
        f"prior_match={s['n_matched_prior_ohlcv']} ({s['match_rate_prior_eligible']*100:.1f}%) "
        f"either={s['n_matched_either']} ({s['match_rate_either_eligible']*100:.1f}%) "
        f"lat_pair_med_h={payload['latency']['pair_created_to_cex_h']['median']} "
        f"cex_ret1h_n={compact['cex_ret_1h_n']} pair_ret1h_n={compact['pair_ret_1h_n']}"
    )


if __name__ == "__main__":
    main()
