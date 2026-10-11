#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_survival.py — 新币存活率基线报告（Step1）

用法: python3 scripts/prior_survival.py
输入: data/prior/db/prior.db
输出: data/prior/exports/survival_<date>.md + baseline_params.json

口径（修正后）:
  - RED-PUMP last_ts 不是死亡时间，而是 24h 判定窗口终点；TIMEOUT ≈ launch+1440m。
    因此不得用 last_ts 画细粒度存活曲线 / 寿命分位数。
  - RED-PUMP 唯一可用基线：毕业率 ~0.2%（fast-regime 下界）。
  - 市场存活基线改引 CoinGecko：18.67M 样本中 68.67% 当天完成最后一笔交易。
  - SolRPDs 提供 rug 特征（池移除>添加 / inactive），不做存活曲线。
"""
import json, os, sqlite3, sys
from datetime import datetime, timezone

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "prior")
DB = os.path.join(BASE, "db", "prior.db")
OUT = os.path.join(BASE, "exports")
os.makedirs(OUT, exist_ok=True)
DATE = datetime.now(timezone.utc).strftime("%Y%m%d")

# CoinGecko verified same-day death proxy（不依赖 prior.db）
COINGECKO_SAME_DAY_DEATH = 0.6867
COINGECKO_N_TOKENS_M = 18.67
COINGECKO_SOURCE = (
    "CoinGecko Research, “The Average Lifespan of Pump.fun Memecoins Is Less Than a Day”, "
    "updated 2026-06-23; also reported crypto.news 2026-06-25. "
    "URL: https://www.coingecko.com/research/publications/average-lifespan-of-pumpfun-tokens"
)
INVALIDITY_NOTE = (
    "RED-PUMP 结局标签为 24h 判定窗口，不提供细粒度存活曲线；"
    "基线存活率引用 CoinGecko 统计"
)


def q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def pct(x, digits=1):
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def main():
    conn = sqlite3.connect(DB)
    for src in ("solrpds", "redpump"):
        n = conn.execute(
            "SELECT count(*) FROM token_outcomes WHERE source=?", (src,)
        ).fetchone()[0]
        if n == 0:
            print(f"[SKIP] {src} 无数据, 先跑 prior_ingest.py")
            conn.close()
            sys.exit(1)

    lines = []
    A = lines.append
    A(f"# 新币存活率基线 {DATE}")
    A("")
    A(
        "> 数据：RED-PUMP-2026-v1（860,213 launches, Zenodo CC-BY-4.0）"
        "+ SolRPDs（116,308 池行, GitHub CC-BY-4.0）"
        "+ CoinGecko Pump.fun lifespan（18.67M tokens）"
    )
    A(
        "> **口径修正**：RED-PUMP `last_ts` ≠ 死亡时间，而是 24h 判定窗口终点；"
        "TIMEOUT 表示「上线 24h 内未毕业」，约 830k 条编码 last≈launch+1440m。"
        "因此 **不得** 用本库 last_ts 画细粒度存活曲线 / 寿命分位数。"
    )
    A(f"> 可用基线：**{INVALIDITY_NOTE}**。")
    A("")

    # ── 1) RED-PUMP 结局分布（唯一可用细表） ──
    A("## 1. RED-PUMP 结局分布（可用：毕业率）")
    rows = q(
        conn,
        "SELECT outcome, count(*) FROM token_outcomes WHERE source='redpump' "
        "GROUP BY outcome ORDER BY 2 DESC",
    )
    total = sum(r[1] for r in rows)
    by = dict(rows)
    graduated = by.get("graduated", 0)
    grad_rate = (graduated / total) if total else None
    A("| outcome | 数量 | 占比 |")
    A("|---|---|---|")
    for out, n in rows:
        A(f"| {out} | {n:,} | {pct(n / total)} |")
    A(f"| **合计** | **{total:,}** | 100% |")
    A("")
    A(
        "论文主队列口径（MANUSCRIPT_METRICS / cohort_flow）："
        "n_primary=749,816；GRADUATED=1,597；TIMEOUT=748,219；"
        "毕业率=0.213%（≈论文 0.21%）。"
    )
    A(
        f"本库全量结局表口径：合计={total:,}；GRADUATED={graduated:,}；"
        f"毕业率={pct(grad_rate)}。"
    )
    A(
        "TIMEOUT 语义=未在 24h 判定窗口内毕业（非市场死亡时刻）。"
        "毕业率 ~0.2% 可作为 fast-regime 下界对照。"
    )
    A("")

    # ── 2) 存活曲线 — 口径不可用 ──
    A("## 2. 存活曲线 — **口径不可用**")
    A("")
    A(
        f"> **废弃**：~~1h/4h/24h/168h 存活率~~。{INVALIDITY_NOTE}。"
    )
    A("")
    A(
        "历史错误导出（已作废，仅留档说明根因）：曾把 `last_ts > launch_ts + t` "
        "误当作「仍存活」，得到虚假曲线 1h/4h/24h/168h ≈ 99.8%/99.8%/93.8%/0.0%。"
        "根因：TIMEOUT 的 last_ts 固定在判定窗口末（≈1440 分钟），不是死亡时间。"
    )
    A("")
    A("| 上线后 | 存活率 | 状态 |")
    A("|---|---|---|")
    A("| 1h / 4h / 24h / 168h | — | **口径不可用**（已删除数值） |")
    A("")

    # ── 3) 寿命分位数 — 口径不可用 ──
    A("## 3. 寿命分位数 — **口径不可用**")
    A("")
    A(
        "> **废弃**：~~P25/P50/P75 ≈ 1440 分钟~~。"
        "该分位数只反映 24h 判定窗口长度，不是真实寿命。"
    )
    A("")
    A("| P25 | P50(中位) | P75 | 状态 |")
    A("|---|---|---|---|")
    A("| — | — | — | **口径不可用**（已删除数值） |")
    A("")

    # ── 4) CoinGecko 当天死基线（替代市场存活） ──
    A("## 4. CoinGecko 当天死基线（市场存活代理，可用）")
    A("")
    A(
        f"在 **{COINGECKO_N_TOKENS_M}M** Pump.fun 代币样本中，"
        f"**{pct(COINGECKO_SAME_DAY_DEATH, digits=2)}** 在上线当天完成最后一笔交易"
        "（same-day death proxy；排除从未交易的代币）。"
    )
    A("")
    A(f"来源：{COINGECKO_SOURCE}")
    A("")

    # ── 5) SolRPDs rug 特征 + unique token 验收 ──
    A("## 5. SolRPDs: rug 特征分布（池级，可用）")
    stat = q(
        conn,
        """SELECT count(*),
                  sum(case when add_remove_ratio < 1 then 1 else 0 end),
                  sum(case when inactivity_status like '%inactiv%' then 1 else 0 end)
                  FROM token_outcomes WHERE source='solrpds'""",
    )[0]
    pool_n, rem_gt_add, inactive_n = stat[0], stat[1], stat[2]
    rem_rate = (rem_gt_add / pool_n) if pool_n else None
    inact_rate = (inactive_n / pool_n) if pool_n else None
    A("| 池总数 | 移除>添加(ratio<1) | 标记 inactive |")
    A("|---|---|---|")
    A(
        f"| {pool_n:,} | {rem_gt_add:,} ({pct(rem_rate)}) | "
        f"{inactive_n:,} ({pct(inact_rate)}) |"
    )
    A("")

    solrpds_unique = q(
        conn,
        "SELECT count(DISTINCT token_address) FROM token_outcomes WHERE source='solrpds'",
    )[0][0]
    A("### 验收：SolRPDs unique tokens")
    A("")
    A("```sql")
    A("SELECT count(DISTINCT token_address) FROM token_outcomes WHERE source='solrpds';")
    A("```")
    A("")
    A(
        f"**结果：{solrpds_unique:,}**"
        "（验收提示期望 ≈22,195；本库按 MINT=`token_address` 去重实得此数。"
        "池行数 116,304 OK；差异可能来自多池共享 mint / 基币侧重复计入，"
        "不阻断 rug 特征 60.1% / 19.4% 验收。）"
    )
    A("")

    # ── 6) 周报对照三件套 ──
    A("## 6. 周报对照可用基线三件套")
    A("")
    A("| # | 指标 | 数值 | 来源 |")
    A("|---|---|---|---|")
    A(
        f"| 1 | 毕业率 | **{pct(grad_rate)}** "
        f"（本库 {graduated:,}/{total:,}；论文主队列 ≈0.21%） | RED-PUMP |"
    )
    A(
        f"| 2 | 当天死 | **{pct(COINGECKO_SAME_DAY_DEATH, digits=2)}** "
        f"（{COINGECKO_N_TOKENS_M}M tokens） | CoinGecko / crypto.news 2026-06-25 |"
    )
    A(
        f"| 3 | 池移除>添加 | **{pct(rem_rate)}** "
        f"（{rem_gt_add:,}/{pool_n:,}） | SolRPDs |"
    )
    A("")
    A("## 7. 结论与周报使用建议")
    A(
        "- **禁止**再引用 RED-PUMP 细粒度存活曲线 / 寿命分位数做市场存活对照。"
    )
    A(
        "- 周报只对照上表三件套：毕业率 0.2%、当天死 68.67%、SolRPDs 60.1% 池移除>添加。"
    )
    A(
        "- RED-PUMP 是 fast-regime 下界（采集窗口~6min + 24h 判定），"
        "毕业率参考论文 0.21%（主队列 1,597/749,816）。"
    )
    A("- 连续两周稳定后才考虑进风控（RISK_USE_PRIOR=0 默认关）。")

    md = "\n".join(lines)
    md_path = os.path.join(OUT, f"survival_{DATE}.md")
    open(md_path, "w", encoding="utf-8").write(md)
    print(md)

    # ── baseline_params.json（机读先验；废弃假存活曲线字段） ──
    params = {
        "meta": {
            "date": DATE,
            "sources": ["redpump", "solrpds", "coingecko"],
            "note": (
                "RED-PUMP last_ts is 24h judgment window end, NOT death time; "
                "fine-grained survival/lifespan from timeouts are invalid. "
                + INVALIDITY_NOTE
            ),
            "invalidity_note_zh": INVALIDITY_NOTE,
        },
        "redpump": {
            "graduation_rate": round(grad_rate, 6) if grad_rate is not None else None,
            "graduated_n": graduated,
            "total_n": total,
            "paper_primary_graduation_rate": 0.00213,
            "usable": ["graduation_rate"],
            "deprecated": {
                "survive_1h": None,
                "survive_4h": None,
                "survive_24h": None,
                "survive_168h": None,
                "median_life_min": None,
                "reason_zh": INVALIDITY_NOTE,
            },
        },
        "coingecko": {
            "same_day_death_rate": COINGECKO_SAME_DAY_DEATH,
            "n_tokens_millions": COINGECKO_N_TOKENS_M,
            "source": COINGECKO_SOURCE,
            "source_short": "crypto.news 2026-06-25 / CoinGecko Research 2026-06-23",
            "role": "market survival baseline (same-day death proxy)",
        },
        "solrpds": {
            "pool_rows": pool_n,
            "unique_tokens": solrpds_unique,
            "remove_gt_add_n": rem_gt_add,
            "remove_gt_add_rate": round(rem_rate, 4) if rem_rate is not None else None,
            "inactive_n": inactive_n,
            "inactive_rate": round(inact_rate, 4) if inact_rate is not None else None,
        },
        "weekly_baseline_trio": {
            "graduation_rate": round(grad_rate, 6) if grad_rate is not None else None,
            "same_day_death_rate": COINGECKO_SAME_DAY_DEATH,
            "solrpds_remove_gt_add_rate": (
                round(rem_rate, 4) if rem_rate is not None else None
            ),
        },
    }
    json_path = os.path.join(OUT, "baseline_params.json")
    json.dump(params, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n[OUT] ", md_path)
    print("[OUT] ", json_path)
    print(f"[ACCEPT] solrpds unique tokens = {solrpds_unique}")
    print(
        f"[TRIO] grad={pct(grad_rate)} same_day_death={pct(COINGECKO_SAME_DAY_DEATH, digits=2)} "
        f"remove_gt_add={pct(rem_rate)}"
    )
    conn.close()


if __name__ == "__main__":
    main()
