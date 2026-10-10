# 新币先验数据 第二步方案（PRIOR_STEP2_PLAN）

> 目标：补入 **GMGN Solana Token Charts**（Kaggle，含完整 OHLCV + death_reason 标签）→ 用**真实 OHLCV** 算出「新币价格行为模式」：见顶时间分布、见顶涨幅、存活曲线（1h/4h/24h/7d）、归零特征——同时**修正第一步存活曲线口径错误**（RED-PUMP 的 last_ts 是 24h 判定窗口，不是死亡时间）。
> 定位：旁路研究，产物只做基线对照；不碰业务四库、不接决策权重（延续纪律）。
> 整理时间：2026-10-10。

---

## §0 执行总览

| # | 动作 | 产物 | 验收 |
|---|------|------|------|
| 0.1 | 获取 GMGN 数据（Kaggle） | `data/prior/raw/gmgn/` 下有 csv/zip | 文件存在，行数≈预期 |
| 0.2 | 建表扩展 | prior.db 加 `token_ohlcv` 表 | 表存在 |
| 0.3 | GMGN 清洗入库 | `scripts/prior_gmgn_ingest.py` | 行数≥验收线 |
| 0.4 | 价格行为分析 | `scripts/prior_ohlcv.py` | 输出 survival_v2 + params |
| 0.5 | 修正第一步报告 | 重生成 `survival_<date>_v2.md` | §2/§3 用真实 OHLCV 口径 |
| 0.6 | 核对 | 见顶中位数 vs 直觉/文献 | 见 §8 |

**硬约束**：
- 只写 `data/prior/` 与 `prior.db`，不碰业务四库
- GMGN 的 `death_reason` 语义是「监控终止原因」，**不等于归零**——报告必须标注，禁止当成死亡标签直接算存活率
- 脚本遇到字段不匹配 → 打印真实结构并退出，禁止静默 NULL
- 不注册定时任务、不启动常驻进程

---

## §1 目录
```bash
mkdir -p /workspace/dexscreener-crawler/data/prior/raw/gmgn
# 确认已有: data/prior/{db,exports,meta} (第一步已建)
```

## §2 获取 GMGN（Kaggle，需认证）

### 路径 A：VM 上有 Kaggle 凭据（~/.kaggle/kaggle.json）
```bash
cd /workspace/dexscreener-crawler/data/prior/raw/gmgn
pip install -q kaggle
kaggle datasets download -d moussaouallaf/gmgn-solana-token-charts
unzip -q gmgn-solana-token-charts.zip -d unpacked/
find unpacked/ -type f | sort | head -20     # 看真实文件与格式（csv? json?）
head -c 2000 unpacked/*.csv 2>/dev/null | head -5   # 看表头
```

### 路径 B：VM 没有凭据（大概率）
**让用户手动下载**：浏览器打开
`https://www.kaggle.com/datasets/moussaouallaf/gmgn-solana-token-charts`
→ 点 Download → 把 `gmgn-solana-token-charts.zip` 放进
`/workspace/dexscreener-crawler/data/prior/raw/gmgn/` → 再执行：
```bash
cd /workspace/dexscreener-crawler/data/prior/raw/gmgn
unzip -q gmgn-solana-token-charts.zip -d unpacked/
find unpacked/ -type f | sort | head -20
```
> 拿到文件后，**把表头和第一条记录的结构贴出来**（后面 §4/§5 脚本要按真实结构映射，本方案不假设列名）。

## §3 建表扩展 —— 在 `models/prior_tables.sql` 末尾追加（整段粘贴）

```sql
-- ===== 第二步: GMGN OHLCV =====
CREATE TABLE IF NOT EXISTS token_ohlcv (
    token_address   TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'gmgn',
    ticker          TEXT,
    discovery_ts    INTEGER,          -- 发现/上线时间（epoch 秒）
    death_reason    TEXT,             -- GMGN 监控终止原因（viable / non-viable...）
    ohlcv_json      TEXT,             -- 原始 OHLCV（数组）
    peak_ts         INTEGER,          -- 见顶时间（epoch 秒）
    peak_price      REAL,             -- 见顶价格
    peak_return     REAL,             -- 见顶相对首价涨幅（倍）
    first_price     REAL,             -- 首条 OHLCV 价
    last_price      REAL,             -- 末条 OHLCV 价
    n_bars          INTEGER,          -- OHLCV 条数
    ingested_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_token ON token_ohlcv(token_address);
```

## §4 GMGN 清洗入库 —— `scripts/prior_gmgn_ingest.py`（整文件替换）

> 设计：**第一步先打印真实结构**（第一条记录的全部 key），再按 key 动态映射；OHLCV 字段解析失败就列出样例退出。

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_gmgn_ingest.py — GMGN Solana Token Charts 清洗入库（Step2）

用法: python3 scripts/prior_gmgn_ingest.py
输入: data/prior/raw/gmgn/unpacked/** (csv 或 json)
输出: prior.db 的 token_ohlcv 表 + provenance 更新
原则: 先打印结构, 再映射; 不匹配就退出。
"""
import csv, glob, json, os, sqlite3, sys
from datetime import datetime, timezone

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "prior")
DB = os.path.join(BASE, "db", "prior.db")
RAW = os.path.join(BASE, "raw", "gmgn", "unpacked")


def to_epoch(v):
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    try:
        return int(float(s))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s[:26].replace("Z", ""), fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def parse_ohlcv(obj):
    """obj 可能是 list[list] / list[dict] / dict{k:list}; 打印结构并尝试解析成 [(ts, price)]"""
    if isinstance(obj, list) and obj:
        first = obj[0]
        if isinstance(first, dict):
            keys = list(first.keys())
            print("[ohlcv] dict 条目标签:", keys)
            # 常见: {'time':..., 'open':...} 或 {'timestamp':..., 'price':...} 或 {'0':t,'1':o,'2':h,'3':l,'4':c}
            t = next((k for k in keys if "time" in k.lower() or "ts" in k.lower() or k in ("0",)), None)
            c = next((k for k in keys if "close" in k.lower() or "price" in k.lower() or k in ("1", "4")), None)
            if not t or not c:
                print("[FAIL] 无法定位 OHLCV 的 时间/价格 列; 样例第一条:", json.dumps(first)[:500])
                sys.exit(1)
            return [(to_epoch(r[t]) or 0, float(r[c])) for r in obj if r.get(c) is not None]
        # list[list]: [ [ts, open, high, low, close, vol], ... ]
        return [(to_epoch(r[0]) or 0, float(r[4])) for r in obj if len(r) >= 5 and r[4] is not None]
    if isinstance(obj, dict):
        # 可能是 {"ts":[...], "price":[...]} 或 {"times":..., "closes":...}
        tkey = next((k for k in obj if "time" in k.lower() or "ts" in k.lower()), None)
        ckey = next((k for k in obj if "close" in k.lower() or "price" in k.lower() or "open" in k.lower()), None)
        if tkey and ckey and isinstance(obj[tkey], list) and isinstance(obj[ckey], list):
            return [(to_epoch(t) or 0, float(p)) for t, p in zip(obj[tkey], obj[ckey]) if p is not None]
        print("[FAIL] dict 结构未识别; keys:", list(obj.keys())[:20])
        sys.exit(1)
    print("[FAIL] OHLCV 类型未识别:", type(obj))
    sys.exit(1)


def main():
    if not os.path.isdir(RAW):
        sys.exit("[FAIL] 先完成 §2 获取 GMGN 数据")
    files = []
    for ext in ("*.csv", "*.json", "*.jsonl"):
        files += glob.glob(os.path.join(RAW, "**", ext), recursive=True)
    if not files:
        sys.exit("[FAIL] unpacked/ 下无 csv/json; 检查下载与解压")
    print("[gmgn] 文件:", [os.path.relpath(f, RAW) for f in files])

    conn = sqlite3.connect(DB)
    conn.executescript(open(os.path.join(BASE, "..", "..", "models", "prior_tables.sql")).read())

    n = 0
    for fp in files:
        print(f"\n--- 处理 {os.path.relpath(fp, RAW)} ---")
        if fp.endswith(".csv"):
            with open(fp, newline="", encoding="utf-8", errors="replace") as f:
                rd = csv.DictReader(f)
                cols = rd.fieldnames
                print("[csv] 列名:", cols)
                def find(keys):
                    for k in cols:
                        kl = k.strip().lower()
                        if any(x in kl for x in keys):
                            return k
                    return None
                c_addr = find(["address", "mint", "token"])
                c_tick = find(["ticker", "symbol"])
                c_disc = find(["discovery", "first_seen", "created", "launch"])
                c_death = find(["death", "reason", "status", "label"])
                c_ohlcv = find(["ohlcv", "chart", "json", "history", "data"])
                if not c_addr or not c_ohlcv:
                    print(f"[FAIL] 缺列 address/ohlcv; 真实列: {cols}")
                    sys.exit(1)
                for row in rd:
                    try:
                        bars = parse_ohlcv(json.loads(row[c_ohlcv] or "[]"))
                    except Exception as e:
                        print(f"[FAIL] OHLCV 解析异常: {e}; 原值前200字符: {str(row[c_ohlcv])[:200]}")
                        sys.exit(1)
                    if not bars:
                        continue
                    bars_sorted = sorted(bars, key=lambda x: x[0])
                    first_p = bars_sorted[0][1]
                    peak = max(bars_sorted, key=lambda x: x[1])
                    last_p = bars_sorted[-1][1]
                    conn.execute(
                        """INSERT INTO token_ohlcv
                           (token_address, ticker, discovery_ts, death_reason, ohlcv_json,
                            peak_ts, peak_price, peak_return, first_price, last_price, n_bars, ingested_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (row[c_addr].strip(), (row.get(c_tick) or "").strip() if c_tick else "",
                         to_epoch(row.get(c_disc)) if c_disc else None,
                         (row.get(c_death) or "").strip() if c_death else None,
                         json.dumps(bars, ensure_ascii=False),
                         peak[0], peak[1], (peak[1] / first_p - 1) if first_p else None,
                         first_p, last_p, len(bars), datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                    n += 1
        elif fp.endswith((".json", ".jsonl")):
            with open(fp, encoding="utf-8", errors="replace") as f:
                data = json.load(f) if fp.endswith(".json") else [json.loads(l) for l in f if l.strip()]
            if isinstance(data, dict):
                data = [data]
            for row in data:
                keys = list(row.keys())
                if n == 0:
                    print("[json] 记录 key:", keys)
                addr = row.get("address") or row.get("mint") or row.get("token_address") or row.get("token")
                ohlcv = row.get("ohlcv") or row.get("chart") or row.get("history") or row.get("data")
                if not addr or not ohlcv:
                    print("[FAIL] json 记录缺 address/ohlcv; key:", keys)
                    sys.exit(1)
                bars = parse_ohlcv(ohlcv)
                if not bars:
                    continue
                bars_sorted = sorted(bars, key=lambda x: x[0])
                first_p = bars_sorted[0][1]
                peak = max(bars_sorted, key=lambda x: x[1])
                conn.execute(
                    """INSERT INTO token_ohlcv
                       (token_address, ticker, discovery_ts, death_reason, ohlcv_json,
                        peak_ts, peak_price, peak_return, first_price, last_price, n_bars, ingested_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(addr).strip(), str(row.get("ticker") or row.get("symbol") or "").strip(),
                     to_epoch(row.get("discovery_ts") or row.get("discovery") or row.get("first_seen")),
                     str(row.get("death_reason") or "").strip() or None,
                     json.dumps(bars, ensure_ascii=False),
                     peak[0], peak[1], (peak[1] / first_p - 1) if first_p else None,
                     first_p, bars_sorted[-1][1], len(bars),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                n += 1
    conn.commit()
    conn.execute("INSERT OR REPLACE INTO provenance VALUES (?,?,?,?,?,?)",
                 ("gmgn", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "https://www.kaggle.com/datasets/moussaouallaf/gmgn-solana-token-charts",
                  "Kaggle 条款", "gmgn-solana-token-charts.zip", "OHLCV+death_reason; death_reason=监控终止原因≠归零"))
    conn.commit()
    row = conn.execute("SELECT count(*), count(distinct token_address) FROM token_ohlcv").fetchone()
    print(f"\n[汇总] token_ohlcv: 行={row[0]} 唯一token={row[1]}")
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
```

## §5 价格行为分析 —— `scripts/prior_ohlcv.py`（整文件替换）

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_ohlcv.py — 新币价格行为模式 + 真实存活曲线（Step2 核心）

用法: python3 scripts/prior_ohlcv.py
输入: prior.db 的 token_ohlcv
输出: data/prior/exports/survival_<date>_v2.md + baseline_params.json（gmgn 段）
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
    A("> 数据：GMGN Solana Token Charts（Kaggle）。death_reason=监控终止原因（≠归零）。")
    A("> 存活口径：OHLCV 最后一条时间 > 上线后 t → 视为活过 t。")
    A("")

    # ── 1) 样本与 death_reason 分布 ──
    A("## 1. 样本与 death_reason")
    rows = conn.execute("SELECT death_reason, count(*) FROM token_ohlcv GROUP BY death_reason ORDER BY 2 DESC").fetchall()
    tot = sum(r[1] for r in rows)
    A("| death_reason | 数量 | 占比 |")
    A("|---|---|---|")
    for r, c in rows:
        A(f"| {r or '(空)'} | {c:,} | {pct(c/tot)} |")
    A(f"| 合计 | {tot:,} | 100% |")
    A("")

    # ── 2) 存活曲线（真实 OHLCV 口径） ──
    A("## 2. 存活曲线（OHLCV 最后时间口径）")
    with_ts = conn.execute("SELECT count(*) FROM token_ohlcv WHERE discovery_ts IS NOT NULL AND peak_ts IS NOT NULL").fetchone()[0]
    A(f"有完整时间戳样本: {with_ts:,}")
    A("| 上线后 | 存活率 | 样本 |")
    A("|---|---|---|")
    for h in (1, 4, 24, 168):
        alive = conn.execute(
            "SELECT count(*) FROM token_ohlcv WHERE discovery_ts IS NOT NULL AND peak_ts IS NOT NULL AND peak_ts > discovery_ts + ?",
            (h * 3600,)).fetchone()[0]
        A(f"| {h}h | {pct(alive/with_ts)} | {alive:,} |")
    A("")

    # ── 3) 见顶时间分布（上线→最高价, 分钟） ──
    A("## 3. 见顶时间分布（discovery→peak, 分钟）")
    mins = [r[0] for r in conn.execute(
        "SELECT (peak_ts-discovery_ts)/60.0 FROM token_ohlcv WHERE discovery_ts IS NOT NULL AND peak_ts > discovery_ts") ]
    if mins:
        mins.sort()
        A(f"| P25 | P50(中位) | P75 | 样本 |")
        A(f"| {qtl(mins,.25):.1f}m | {qtl(mins,.50):.1f}m | {qtl(mins,.75):.1f}m | {len(mins):,} |")
        A("")
        # 分桶
        buckets = Counter(int(m) for m in mins)
        A("分桶（分钟）:")
        for b in sorted(buckets):
            if buckets[b] > 0:
                A(f"- {b}min: {buckets[b]:,}")
        A("")

    # ── 4) 见顶涨幅分布（倍） ──
    A("## 4. 见顶涨幅（peak/first-1）")
    rets = [r[0] for r in conn.execute("SELECT peak_return FROM token_ohlcv WHERE peak_return IS NOT NULL")]
    if rets:
        rets.sort()
        A(f"| P25 | P50 | P75 | 样本 |")
        A(f"| {qtl(rets,.25):.2f}x | {qtl(rets,.50):.2f}x | {qtl(rets,.75):.2f}x | {len(rets):,} |")
        A("")

    # ── 5) 分层: viable vs non-viable 见顶特征 ──
    A("## 5. 分层对照（death_reason 分组）")
    for grp in ("viable", "non-viable"):
        gmins = [r[0] for r in conn.execute(
            "SELECT (peak_ts-discovery_ts)/60.0 FROM token_ohlcv WHERE death_reason LIKE ? AND discovery_ts IS NOT NULL AND peak_ts > discovery_ts",
            (f"%{grp}%",))]
        if gmins:
            gmins.sort()
            A(f"- **{grp}**（n={len(gmins):,}）: 见顶中位 {qtl(gmins,.50):.1f}m | P75 {qtl(gmins,.75):.1f}m")
    A("")

    A("## 6. 结论与周报使用建议")
    A("- 见顶时间/涨幅/存活曲线均为**基线参考**；GMGN 的 death_reason 是监控终止原因，viable 未必能持续存活。")
    A("- 与第一步 RED-PUMP 毕业率 0.2% / CoinGecko 当天死 68.67% 并排使用。")
    A("- 连续两周稳定后才考虑进风控（RISK_USE_PRIOR=0 默认关）。")

    md = "\n".join(L)
    md_path = os.path.join(OUT, f"survival_{DATE}_v2.md")
    open(md_path, "w", encoding="utf-8").write(md)
    print(md)

    params = {"meta": {"date": DATE, "sources": ["gmgn"], "note": "death_reason=监控终止≠归零"}}
    params["gmgn"] = {
        "n": tot,
        "survive": {f"{h}h": (round(alive/with_ts, 4) if with_ts else None) for h, alive in [
            (1, conn.execute("SELECT count(*) FROM token_ohlcv WHERE discovery_ts IS NOT NULL AND peak_ts > discovery_ts + 3600").fetchone()[0]),
            (4, conn.execute("SELECT count(*) FROM token_ohlcv WHERE discovery_ts IS NOT NULL AND peak_ts > discovery_ts + 14400").fetchone()[0]),
            (24, conn.execute("SELECT count(*) FROM token_ohlcv WHERE discovery_ts IS NOT NULL AND peak_ts > discovery_ts + 86400").fetchone()[0]),
        ]},
    }
    if mins:
        params["gmgn"]["median_peak_min"] = round(qtl(mins, .50), 1)
        params["gmgn"]["peak_min_p25"] = round(qtl(mins, .25), 1)
        params["gmgn"]["peak_min_p75"] = round(qtl(mins, .75), 1)
    if rets:
        params["gmgn"]["median_peak_return_x"] = round(qtl(rets, .50), 2)
    json_path = os.path.join(OUT, "baseline_params.json")
    json.dump(params, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n[OUT]", md_path)
    print("[OUT]", json_path)
    conn.close()


if __name__ == "__main__":
    main()
```

## §6 一条龙运行顺序

```bash
cd /workspace/dexscreener-crawler

# 0.1 目录
mkdir -p data/prior/raw/gmgn

# 0.2 获取 GMGN（路径 A 或 B，见 §2）→ 解压到 data/prior/raw/gmgn/unpacked/

# 0.3 追加建表（§3 SQL 追加到 models/prior_tables.sql 末尾后）
python3 scripts/prior_gmgn_ingest.py

# 0.4 分析
python3 scripts/prior_ohlcv.py

# 0.5 核对
cat data/prior/exports/survival_*_v2.md
```

## §7 provenance 更新
在 `data/prior/meta/provenance.md` 追加一行：
```markdown
| gmgn | 2026-10-10 | https://www.kaggle.com/datasets/moussaouallaf/gmgn-solana-token-charts | Kaggle 条款 | gmgn-solana-token-charts.zip | OHLCV+death_reason；death_reason=监控终止原因≠归零 |
```

## §8 验收线（全部满足才算完成）

| 检查 | 期望 | 实测入口 |
|------|------|----------|
| GMGN 文件 | unpacked/ 下有 csv 或 json | `find data/prior/raw/gmgn/unpacked/ -type f` |
| token_ohlcv 入库 | 行数>0；唯一 token>1000 | `python3 -c "import sqlite3;c=sqlite3.connect('data/prior/db/prior.db');print(c.execute('select count(*),count(distinct token_address) from token_ohlcv').fetchone())"` |
| 存活曲线 | 1h/4h/24h 为合理小数（0.05~0.95），**不是 99.8%/0% 这种判定窗口假象** | survival_v2 §2 |
| 见顶时间分布 | P50 为真实分钟数（非 1440 整点） | survival_v2 §3 |
| 分层对照 | viable vs non-viable 两组都有值 | survival_v2 §5 |
| baseline_params.json | 含 gmgn 段（survive/median_peak_min/median_peak_return_x） | `python3 -m json.tool data/prior/exports/baseline_params.json` |
| 隔离 | 业务四库 mtime 未变 | `ls -l dexscreener.db execution_ledger.db paper_trading.db news_events.db` |

## §9 排查清单

| 症状 | 原因 | 处理 |
|------|------|------|
| Kaggle 下载 403 | 未登录/无 token | 走 §2 路径 B（手动下载） |
| unpacked/ 是嵌套 zip | 数据打包多层 | 递归解压：`find unpacked/ -name '*.zip' -exec unzip -o -d unpacked/ {} \;` |
| 脚本报「OHLCV 结构未识别」 | 数据集结构与示例不同 | 按脚本打印的样例，扩展 §4 `parse_ohlcv` 的映射分支 |
| 存活率出现 99.8%/0% | 又把判定窗口当寿命 | 停止，检查 discovery_ts 与 peak_ts 是否真实；必要时打印 5 条原始记录人工核对 |
| 唯一 token 过少 | 数据是子集/截断 | 核对下载完整性（zip 大小） |
| viable/non-viable 大小写不同 | 标签口径 | §5 用 LIKE 匹配，仍空则打印全部 death_reason 值人工对齐 |

## §10 本步不做
- ❌ 不把 GMGN 数据写进业务四库/不接决策权重
- ❌ 不做模型训练（本步只出统计先验；二分类实验放第三步）
- ❌ 不注册定时任务
- ❌ 第一步的 RED-PUMP 数据**不删除**（毕业率 0.2% 仍有效），只修正报告口径

---
*执行完 §8 验收后，把 survival_<date>_v2.md 发我。若 GMGN 需要手动下载，先把 zip 放进 raw/gmgn/ 再继续。*
