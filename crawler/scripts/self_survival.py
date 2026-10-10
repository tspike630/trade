#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""self_survival.py — 自有 crawler DB 观测跨度存活（PRIOR 旁路，只读）

用法: python3 scripts/self_survival.py
输入: 主库 dexscreener.db（只读）pair_history
输出: data/prior/exports/self_survival_<YYYYMMDD>.md
      + merge baseline_params.json["self_db"]

口径（务必读）:
  - 单位: (chain_id, token_address)
  - first_seen = MIN(pair_history.recorded_at)
  - last_seen  = MAX(pair_history.recorded_at)
  - span      = last_seen - first_seen
  - 存活 t: span > t  （严格大于；活过 t 小时）
  - 这是「本 crawler 观测到该 token 的时间跨度」，不是链上真实寿命，
    也不是 GMGN OHLCV last_bar，更不是 RED-PUMP 24h 判定窗口。
  - 若 crawler 总观测窗口短于 7d，则 7d 存活必然 ≈0%——这是覆盖限制，须在报告中写明。

原则: 永不写入业务表；不改 INDICATORS_WEIGHT_*；不改 trading。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
# Prefer config.DB_PATH when importable; fall back to repo-root dexscreener.db.
# Optional: config.py is the crawler package, not always importable.
try:
    sys.path.insert(0, ROOT)
    from config import DB_PATH as _CFG_DB

    DB = _CFG_DB if os.path.isabs(_CFG_DB) else os.path.join(ROOT, _CFG_DB)
except Exception:
    DB = os.path.join(ROOT, "dexscreener.db")

PRIOR_BASE = os.path.join(ROOT, "data", "prior")
OUT = os.path.join(PRIOR_BASE, "exports")
os.makedirs(OUT, exist_ok=True)

# Report date in Asia/Hong_Kong (UTC+8) to match user zone / box clock
HK = timezone(timedelta(hours=8))
DATE = datetime.now(HK).strftime("%Y%m%d")

HORIZONS_H = (1, 4, 24, 168)  # 1h / 4h / 24h / 7d


def parse_ts(s: str):
    """Parse ISO recorded_at → aware UTC datetime. Returns None on failure."""
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


def pct(x, digits=1):
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def qtl(sorted_list, p):
    if not sorted_list:
        return None
    return sorted_list[min(len(sorted_list) - 1, int(p * len(sorted_list)))]


def main():
    if not os.path.exists(DB):
        print(f"[FAIL] DB not found: {DB}")
        sys.exit(1)

    # Read-only connection
    uri = f"file:{os.path.abspath(DB)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    # Schema sanity
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pair_history)").fetchall()}
    need = {"chain_id", "token_address", "recorded_at"}
    if not need.issubset(cols):
        print(f"[FAIL] pair_history missing columns; have={sorted(cols)} need={sorted(need)}")
        conn.close()
        sys.exit(1)

    # Optional: also note token_indicators exists but we use pair_history spans
    has_ti = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_indicators'"
    ).fetchone()

    global_min, global_max, n_hist = conn.execute(
        "SELECT MIN(recorded_at), MAX(recorded_at), COUNT(*) FROM pair_history"
    ).fetchone()
    g0, g1 = parse_ts(global_min), parse_ts(global_max)
    crawler_window_h = None
    if g0 and g1:
        crawler_window_h = (g1 - g0).total_seconds() / 3600.0

    rows = conn.execute(
        """
        SELECT chain_id, token_address,
               MIN(recorded_at) AS first_seen,
               MAX(recorded_at) AS last_seen,
               COUNT(*) AS n_ticks
        FROM pair_history
        WHERE token_address IS NOT NULL AND token_address != ''
        GROUP BY chain_id, token_address
        """
    ).fetchall()
    conn.close()

    spans_sec = []
    ticks = []
    bad = 0
    for r in rows:
        a, b = parse_ts(r["first_seen"]), parse_ts(r["last_seen"])
        if a is None or b is None or b < a:
            bad += 1
            continue
        spans_sec.append((b - a).total_seconds())
        ticks.append(r["n_ticks"])

    n = len(spans_sec)
    spans_min = sorted(s / 60.0 for s in spans_sec)
    spans_h = sorted(s / 3600.0 for s in spans_sec)

    survive = {}
    survive_n = {}
    for h in HORIZONS_H:
        thr = h * 3600.0
        alive = sum(1 for s in spans_sec if s > thr)
        survive[f"{h}h"] = (alive / n) if n else None
        survive_n[f"{h}h"] = alive

    p25 = qtl(spans_min, 0.25)
    p50 = qtl(spans_min, 0.50)
    p75 = qtl(spans_min, 0.75)

    # Anti-假象 heuristics
    warnings = []
    s1 = survive.get("1h")
    s24 = survive.get("24h")
    s168 = survive.get("168h")
    if s1 is not None and s1 >= 0.995 and s24 is not None and s24 <= 0.01:
        warnings.append("疑似判定窗口假象（1h≈100% 且 24h≈0%）— 请核对口径，勿直接当寿命")
    if p50 is not None and abs(p50 - 1440.0) < 1.0:
        warnings.append("P50 span ≈1440m — 疑似 24h 窗口假象")
    if crawler_window_h is not None and crawler_window_h < 168:
        warnings.append(
            f"crawler 总观测窗口仅约 {crawler_window_h:.1f}h（{global_min} → {global_max}）；"
            "7d 存活必然受覆盖上限约束，≠ 链上真实 7d 死亡率"
        )
    if crawler_window_h is not None and crawler_window_h < 24:
        warnings.append("观测窗口 <24h：24h/7d 存活率几乎无信息量，仅作稀疏基线")
    single_tick = sum(1 for t in ticks if t < 2)
    if n and single_tick / n > 0.3:
        warnings.append(f"单点样本偏多（n_ticks<2 占 {single_tick/n*100:.1f}%）→ span=0 拉低存活率")

    # sparse flag
    sparse = False
    if crawler_window_h is not None and crawler_window_h < 48:
        sparse = True
    if n < 100:
        sparse = True

    L = []
    A = L.append
    A(f"# 自有 DB 观测跨度存活 {DATE}")
    A("")
    A("> **只读** `dexscreener.db.pair_history`；不写业务表。")
    A("> **口径**：对每个 `(chain_id, token_address)`，`first_seen=MIN(recorded_at)`，"
      "`last_seen=MAX(recorded_at)`；**存活 t ⇔ (last_seen − first_seen) > t**。")
    A("> 这是 crawler **观测跨度**，不是链上真实寿命 / GMGN OHLCV / RED-PUMP 判定窗口。")
    if has_ti:
        A("> `token_indicators` 存在但未用于本报告（其为最新快照，无 first/last 历史跨度）。")
    A("")

    A("## 1. 样本与 crawler 窗口")
    A(f"- DB: `{os.path.relpath(DB, ROOT)}`（mode=ro）")
    A(f"- pair_history 行数: {n_hist:,}")
    A(f"- unique tokens (chain_id+token_address): **{n:,}**" + (f"（解析失败 {bad}）" if bad else ""))
    A(f"- recorded_at 全局范围: `{global_min}` → `{global_max}`")
    if crawler_window_h is not None:
        A(f"- crawler 总观测窗口: **{crawler_window_h:.1f}h**（≈{crawler_window_h/24:.2f}d）")
    if sparse:
        A("- **稀疏/短窗标记**: YES — 下列存活率受观测覆盖限制，勿当市场寿命基线过度解读")
    else:
        A("- 稀疏/短窗标记: no")
    A("")

    A("## 2. 存活率（span > t）")
    A("| 上线后 | 存活率 | 样本(活过) |")
    A("|---|---|---|")
    for h in HORIZONS_H:
        key = f"{h}h"
        label = f"{h}h" if h < 168 else "7d (168h)"
        A(f"| {label} | {pct(survive[key])} | {survive_n[key]:,} |")
    A("")

    A("## 3. 观测跨度分布（分钟）")
    A("| P25 | P50 | P75 | max | n |")
    A("|---|---|---|---|---|")
    if spans_min:
        A(
            f"| {p25:.1f}m | {p50:.1f}m | {p75:.1f}m | {spans_min[-1]:.1f}m | {n:,} |"
        )
    else:
        A("| — | — | — | — | 0 |")
    A("")
    if spans_h:
        A(
            f"跨度（小时）: P25={qtl(spans_h,.25):.2f}h / P50={qtl(spans_h,.50):.2f}h / "
            f"P75={qtl(spans_h,.75):.2f}h / max={spans_h[-1]:.2f}h"
        )
        A("")

    A("## 4. 反假象 / 口径提醒")
    if warnings:
        for w in warnings:
            A(f"- ⚠ {w}")
    else:
        A("- 未见经典 99.8%/1440 假象模式")
    # Explicit anti-99.8/1440 check result
    fake_like = (
        (s1 is not None and s1 >= 0.995 and s24 is not None and abs(s1 - (s24 or 0)) < 0.02)
        or (p50 is not None and abs(p50 - 1440) < 1)
    )
    A(f"- 假象扫描（99.8%/1440 类）: {'FAIL 疑似' if fake_like else 'PASS'}")
    A("- 与 GMGN early-window 0% 不同：自有库若窗口≈1d，则 1h 存活可能很高而 7d≈0%，这反映 **crawler 覆盖**，须并排写明窗口长度。")
    A("")

    A("## 5. 周报使用建议")
    A("- 本表仅作 PRIOR 旁路自检；默认不进风控（RISK_USE_PRIOR=0）。")
    A("- 与 CoinGecko 当天死 / RED-PUMP 毕业率 / GMGN early-peak 并排，勿单独当寿命真值。")
    A("- 待 crawler 连续跑满 ≥7d 后再解读 7d 存活。")

    md = "\n".join(L) + "\n"
    md_path = os.path.join(OUT, f"self_survival_{DATE}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)

    # Merge baseline_params.json
    json_path = os.path.join(OUT, "baseline_params.json")
    existing = {}
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    meta = existing.get("meta") or {}
    sources = list(meta.get("sources") or [])
    if "self_db" not in sources:
        sources.append("self_db")
    meta["date"] = DATE
    meta["sources"] = sources
    existing["meta"] = meta

    existing["self_db"] = {
        "n": n,
        "n_history_rows": n_hist,
        "db": os.path.relpath(DB, ROOT),
        "table": "pair_history",
        "survive_basis": "last_seen - first_seen > t  (MIN/MAX recorded_at per chain_id+token_address)",
        "survive": {
            "1h": round(survive["1h"], 4) if survive["1h"] is not None else None,
            "4h": round(survive["4h"], 4) if survive["4h"] is not None else None,
            "24h": round(survive["24h"], 4) if survive["24h"] is not None else None,
            "168h": round(survive["168h"], 4) if survive["168h"] is not None else None,
        },
        "survive_n": {
            "1h": survive_n["1h"],
            "4h": survive_n["4h"],
            "24h": survive_n["24h"],
            "168h": survive_n["168h"],
        },
        "span_min": {
            "p25": round(p25, 1) if p25 is not None else None,
            "p50": round(p50, 1) if p50 is not None else None,
            "p75": round(p75, 1) if p75 is not None else None,
            "max": round(spans_min[-1], 1) if spans_min else None,
        },
        "crawler_window_h": round(crawler_window_h, 2) if crawler_window_h is not None else None,
        "recorded_at_range": [global_min, global_max],
        "sparse": sparse,
        "warnings": warnings,
        "note": "观测跨度≠链上寿命；短 crawler 窗口会压低 7d；anti-假象已扫描",
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print("[OUT]", md_path)
    print("[OUT]", json_path)
    print(
        f"[KEY] n={n} survive 1h/4h/24h/7d="
        f"{pct(survive['1h'])}/{pct(survive['4h'])}/{pct(survive['24h'])}/{pct(survive['168h'])} "
        f"span P25/P50/P75={p25:.1f}/{p50:.1f}/{p75:.1f}m"
        if spans_min
        else f"[KEY] n={n} (no spans)"
    )


if __name__ == "__main__":
    main()
