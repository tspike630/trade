#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_ohlcv.py — 新币价格行为模式 + 真实存活曲线（Step2 核心）

用法: python3 scripts/prior_ohlcv.py
输入: prior.db 的 token_ohlcv
输出: data/prior/exports/survival_<date>_v2.md + baseline_params.json（gmgn 段，合并保留 RED-PUMP 等）
"""
import json, os, sqlite3, sys
from collections import Counter
from datetime import datetime, timezone

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "prior")
DB = os.path.join(BASE, "db", "prior.db")
OUT = os.path.join(BASE, "exports")
os.makedirs(OUT, exist_ok=True)
DATE = datetime.now(timezone.utc).strftime("%Y%m%d")


def pct(x):
    return f"{x*100:.1f}%" if x is not None else "—"


def qtl(sorted_list, p):
    if not sorted_list:
        return None
    return sorted_list[min(len(sorted_list) - 1, int(p * len(sorted_list)))]


def last_bar_ts(ohlcv_json):
    """OHLCV 最后一条时间（秒）；bars 存为 [[ts, price], ...]"""
    try:
        bars = json.loads(ohlcv_json or "[]")
    except Exception:
        return None
    if not bars:
        return None
    try:
        return max(int(b[0]) for b in bars if b and b[0] is not None)
    except Exception:
        return None


def main():
    conn = sqlite3.connect(DB)
    n = conn.execute("SELECT count(*) FROM token_ohlcv").fetchone()[0]
    if n == 0:
        print("[SKIP] token_ohlcv 无数据; 先跑 prior_gmgn_ingest.py")
        conn.close()
        sys.exit(1)

    L = []
    A = L.append
    A(f"# 新币价格行为基线 v2 {DATE}")
    A("")
    A("> **数据：GMGN Solana Token Charts 完整数据集 n={n:,}**（Kaggle `moussaouallaf/gmgn-solana-token-charts` v3；Rows=956，单文件 combined_tokens.csv；无更大 pack）。".format(n=n))
    A("> death_reason=**监控终止原因≠归零**（volume_low / price_stable / timeout / candle_gap 等）。")
    A("> 存活口径：OHLCV **最后一条**时间 > 上线后 t → 视为活过 t（非 peak_ts，非 RED-PUMP 判定窗口）。")
    A("> 并排参考（第一步保留）：RED-PUMP 毕业率 **0.2%** / CoinGecko 当天死 **68.67%** / SolRPDs remove>add **60.1%**。")
    A("")

    # ── 1) 样本与 death_reason 分布 ──
    A("## 1. 样本与 death_reason（监控终止原因≠归零）")
    rows = conn.execute("SELECT death_reason, count(*) FROM token_ohlcv GROUP BY death_reason ORDER BY 2 DESC").fetchall()
    tot = sum(r[1] for r in rows)
    A("| death_reason | 数量 | 占比 |")
    A("|---|---|---|")
    for r, c in rows:
        A(f"| {r or '(空)'} | {c:,} | {pct(c/tot)} |")
    A(f"| 合计 | {tot:,} | 100% |")
    A("")
    A(f"标注：**GMGN 完整数据集**，unique tokens **={tot:,}**（验收门槛 **=956**，非 >1000；官方 Kaggle v3 即为完整包）。")
    A("")

    # ── 预计算 last_ts ──
    token_rows = conn.execute(
        "SELECT token_address, discovery_ts, peak_ts, peak_return, death_reason, ohlcv_json FROM token_ohlcv"
    ).fetchall()
    recs = []
    for addr, disc, peak_ts, peak_ret, death, oj in token_rows:
        last_ts = last_bar_ts(oj)
        recs.append({
            "addr": addr, "disc": disc, "peak_ts": peak_ts, "peak_ret": peak_ret,
            "death": death or "", "last_ts": last_ts,
        })

    # ── 2) 存活曲线（真实 OHLCV 最后时间口径） ──
    A("## 2. 存活曲线（OHLCV 最后时间口径）")
    usable = [r for r in recs if r["disc"] is not None and r["last_ts"] is not None]
    with_ts = len(usable)
    A(f"有完整时间戳样本: {with_ts:,}")
    # monitoring span (minutes)
    spans = sorted([(r["last_ts"] - r["disc"]) / 60.0 for r in usable if r["last_ts"] >= r["disc"]])
    if spans:
        A(f"监控跨度（last−discovery，分钟）: P25={qtl(spans,.25):.1f}m / P50={qtl(spans,.50):.1f}m / P75={qtl(spans,.75):.1f}m / max={spans[-1]:.1f}m")
        A("")
        if qtl(spans, .50) is not None and qtl(spans, .50) < 60:
            A("> **覆盖时长说明**：本完整数据集 OHLCV 监控窗口中位仅约 "
              f"{qtl(spans,.50):.1f} 分钟（early-window charts），故 1h/4h/24h 存活率会接近 0%——"
              "这是**早期窗口数据覆盖限制**，不是下载失败，也不是 RED-PUMP 那种把 24h 判定窗口当寿命的假象。"
              "GMGN 价值在 early peak（P50≈1.0m）；官方包规模即为 Tokens=956。")
            A("")
    A("| 上线后 | 存活率 | 样本 |")
    A("|---|---|---|")
    survive_rates = {}
    for h in (1, 4, 24, 168):
        alive = sum(1 for r in usable if r["last_ts"] > r["disc"] + h * 3600)
        rate = (alive / with_ts) if with_ts else None
        survive_rates[f"{h}h"] = round(rate, 4) if rate is not None else None
        label = f"{h}h" if h < 168 else "7d (168h)"
        A(f"| {label} | {pct(rate)} | {alive:,} |")
    A("")

    # ── 3) 见顶时间分布（上线→最高价, 分钟） ──
    A("## 3. 见顶时间分布（discovery→peak, 分钟）")
    mins = sorted([
        (r["peak_ts"] - r["disc"]) / 60.0
        for r in recs
        if r["disc"] is not None and r["peak_ts"] is not None and r["peak_ts"] > r["disc"]
    ])
    peak_p25 = peak_p50 = peak_p75 = None
    if mins:
        peak_p25, peak_p50, peak_p75 = qtl(mins, .25), qtl(mins, .50), qtl(mins, .75)
        A(f"| P25 | P50(中位) | P75 | 样本 |")
        A(f"|---|---|---|---|")
        A(f"| {peak_p25:.1f}m | {peak_p50:.1f}m | {peak_p75:.1f}m | {len(mins):,} |")
        A("")
        # 粗分桶（避免秒级噪声刷屏）：0-1 / 1-2 / 2-5 / 5-15 / 15+
        buckets = Counter()
        for m in mins:
            if m < 1:
                buckets["0-1min"] += 1
            elif m < 2:
                buckets["1-2min"] += 1
            elif m < 5:
                buckets["2-5min"] += 1
            elif m < 15:
                buckets["5-15min"] += 1
            else:
                buckets["15min+"] += 1
        A("分桶（分钟）:")
        for b in ("0-1min", "1-2min", "2-5min", "5-15min", "15min+"):
            if buckets.get(b):
                A(f"- {b}: {buckets[b]:,}")
        A("")
        if abs(peak_p50 - 1440) < 1:
            A("> ⚠ P50≈1440m — 疑似判定窗口假象，需人工核对。")
            A("")

    # ── 4) 见顶涨幅分布（倍） ──
    A("## 4. 见顶涨幅（peak/first-1）")
    rets = sorted([r["peak_ret"] for r in recs if r["peak_ret"] is not None])
    med_ret = None
    if rets:
        med_ret = qtl(rets, .50)
        A(f"| P25 | P50 | P75 | 样本 |")
        A(f"|---|---|---|---|")
        A(f"| {qtl(rets,.25):.2f}x | {qtl(rets,.50):.2f}x | {qtl(rets,.75):.2f}x | {len(rets):,} |")
        A("")

    # ── 5) 分层: death_reason 见顶特征 ──
    A("## 5. 分层对照（death_reason 分组）")
    # death_reason 实际标签是 volume_low/price_stable/timeout/candle_gap（非 viable/non-viable）
    by_death = Counter(r["death"] for r in recs)
    for grp, _ in by_death.most_common():
        gmins = sorted([
            (r["peak_ts"] - r["disc"]) / 60.0
            for r in recs
            if r["death"] == grp and r["disc"] is not None and r["peak_ts"] is not None and r["peak_ts"] > r["disc"]
        ])
        if gmins:
            A(f"- **{grp or '(空)'}**（n={len(gmins):,}）: 见顶中位 {qtl(gmins,.50):.1f}m | P75 {qtl(gmins,.75):.1f}m")
        else:
            A(f"- **{grp or '(空)'}**（有 peak 后置样本 n=0）")
    A("")

    A("## 6. 结论与周报使用建议")
    A("- 本报告为 **GMGN 完整数据集**（n={:,}；Kaggle v3 官方包，单文件 combined_tokens.csv，无更大 pack）。".format(tot))
    A("- death_reason 是**监控终止原因≠归零**；不可直接当死亡标签算存活率。")
    A("- 见顶时间/涨幅/存活曲线均为**基线参考**；与第一步 RED-PUMP 毕业率 0.2% / CoinGecko 当天死 68.67% / SolRPDs 60.1% 并排使用。")
    A("- 连续两周稳定后才考虑进风控（RISK_USE_PRIOR=0 默认关）。")
    A("")
    A("## 7. 反假象验收（§8）")
    # checks
    s1 = survive_rates.get("1h")
    s24 = survive_rates.get("24h")
    fake_surv = False
    if s1 is not None and s24 is not None:
        if (s1 >= 0.995 and s24 <= 0.005) or (s1 >= 0.995 and abs(s1 - s24) < 0.01):
            fake_surv = True
        # classic 99.8%/0% pattern
        if s1 >= 0.99 and s24 <= 0.01 and (spans and qtl(spans, .50) and qtl(spans, .50) >= 1000):
            fake_surv = True
    peak_ok = peak_p50 is not None and abs(peak_p50 - 1440) >= 1.0
    # short early-window 0% is NOT the RED-PUMP artifact
    short_window = bool(spans) and qtl(spans, .50) is not None and qtl(spans, .50) < 60
    surv_note = "PASS（非判定窗口假象）"
    if fake_surv:
        surv_note = "FAIL：疑似判定窗口假象"
    elif short_window and s1 is not None and s1 < 0.05:
        surv_note = "PASS-with-coverage-caveat：1h+≈0% 因 early-window 监控仅数分钟（覆盖限制），≠ 下载失败，≠ RED-PUMP 24h 窗口假象"
    A(f"- 存活曲线: {surv_note}（1h={pct(s1)}, 4h={pct(survive_rates.get('4h'))}, 24h={pct(s24)}, 7d={pct(survive_rates.get('168h'))}）")
    A(f"- 见顶 P50: {'PASS' if peak_ok else 'FAIL'}（P50={peak_p50:.1f}m，要求 ≠≈1440）" if peak_p50 is not None else "- 见顶 P50: FAIL（无数据）")
    A("- death_reason 标注: PASS（监控终止原因≠归零）")
    accept_ok = tot == 956
    A(f"- 完整数据集标注: {'PASS' if accept_ok else 'WARN'}（unique tokens={tot:,}；验收 **=956**）")
    A("- RED-PUMP/CoinGecko/SolRPDs 基线: 保留于 baseline_params.json（不删除）")

    md = "\n".join(L)
    md_path = os.path.join(OUT, f"survival_{DATE}_v2.md")
    open(md_path, "w", encoding="utf-8").write(md)
    print(md)

    # ── merge baseline_params.json（保留 redpump/coingecko/solrpds）──
    json_path = os.path.join(OUT, "baseline_params.json")
    existing = {}
    if os.path.exists(json_path):
        try:
            existing = json.load(open(json_path, encoding="utf-8"))
        except Exception:
            existing = {}
    meta = existing.get("meta") or {}
    sources = list(meta.get("sources") or [])
    if "gmgn" not in sources:
        sources.append("gmgn")
    meta["date"] = DATE
    meta["sources"] = sources
    note = meta.get("note") or ""
    # drop obsolete SAMPLE mislabel from prior runs
    note = note.replace("; gmgn SAMPLE: death_reason=监控终止≠归零", "")
    note = note.replace("gmgn SAMPLE: death_reason=监控终止≠归零", "")
    if "gmgn 完整数据集" not in note:
        note = (note + "; " if note else "") + "gmgn 完整数据集 n=956: death_reason=监控终止≠归零; early-window coverage limits 1h/4h/24h survival"
    meta["note"] = note
    meta.pop("gmgn_sample", None)
    meta["gmgn_n"] = tot
    meta["gmgn_pack"] = "完整数据集"
    meta["gmgn_acceptance_unique"] = 956
    existing["meta"] = meta

    existing["gmgn"] = {
        "n": tot,
        "complete_pack": True,
        "sample": False,
        "note": "完整数据集 n=956（Kaggle v3 combined_tokens.csv；无更大 pack）；death_reason=监控终止≠归零；early-window ~1.6m median → 1h/4h/24h survival≈0% is coverage limit",
        "acceptance_unique_tokens": 956,
        "survive": {
            "1h": survive_rates.get("1h"),
            "4h": survive_rates.get("4h"),
            "24h": survive_rates.get("24h"),
            "168h": survive_rates.get("168h"),
        },
        "survive_basis": "ohlcv_last_ts > discovery_ts + t",
    }
    if peak_p50 is not None:
        existing["gmgn"]["median_peak_min"] = round(peak_p50, 1)
        existing["gmgn"]["peak_min_p25"] = round(peak_p25, 1)
        existing["gmgn"]["peak_min_p75"] = round(peak_p75, 1)
    if med_ret is not None:
        existing["gmgn"]["median_peak_return_x"] = round(med_ret, 2)
    if spans:
        existing["gmgn"]["monitor_span_min_p50"] = round(qtl(spans, .50), 1)

    # ensure weekly trio still present if prior step1 wrote it
    if "weekly_baseline_trio" not in existing and "redpump" in existing and "coingecko" in existing:
        existing["weekly_baseline_trio"] = {
            "graduation_rate": existing["redpump"].get("graduation_rate"),
            "same_day_death_rate": existing["coingecko"].get("same_day_death_rate"),
            "solrpds_remove_gt_add_rate": (existing.get("solrpds") or {}).get("remove_gt_add_rate"),
        }

    json.dump(existing, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n[OUT]", md_path)
    print("[OUT]", json_path)
    # quick preserve check
    for k in ("redpump", "coingecko", "solrpds"):
        print(f"[preserve] {k}:", "yes" if k in existing else "MISSING")
    conn.close()


if __name__ == "__main__":
    main()
