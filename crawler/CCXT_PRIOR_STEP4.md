# CCXT × Prior Step 4 — 视野对照（B 深化）+ 精确上线事件（A 解锁事件表）

> 状态：落地 2026-10-12  
> 对应：QUANT/方案2 Step 4  
> **严格旁路**：只读 `ccxt.db` / baseline；只写 `prior.db`（`cex_scope_compare` 产物 + `cex_listing_events`）  
> **默认关 / 不调权**：不进评分、决策、告警、业务四库  
> 前置：Step 3（`cex_scope_snapshots` 日快照，§6d）

---

## 1. 范围

| 件 | 状态 | 说明 |
|----|------|------|
| **①** 对照分析 `cex_scope_compare` | ✅ | ≥2 行快照才出 md；当前 1 行 →「data accruing」exit 0 |
| **②** 精确上线事件 `ccxt_listing_watch` | ✅ | `load_markets()` 全量 diff → `cex_listing_events` |
| **分支 A 深挖** | ⏸ **未启** | 只建事件表；待与链上 join 匹配干净（同 symbol 归一）后再启 per-token 深挖 |

---

## 2. 交付物

| 路径 | 作用 |
|------|------|
| `scripts/cex_scope_compare.py` | 视野 vs 链上先验对照（ro → md） |
| `scripts/ccxt_listing_watch.py` | 全量 markets diff → `cex_listing_events` |
| `models/cex_listing_events.sql` | 表结构文档副本（脚本内联建表） |
| `review/data.py` → `load_cex_scope_compare` / `load_cex_listing_events` | 周报只读加载 |
| `review/report.py` → §6e / §6f | 缺数据不失败 |
| 本文 | 口径 / 用法 / 验收 |

---

## 3. 口径

### ① compare
1. 只读 `prior.db.cex_scope_snapshots` + `baseline_params.json#self_db`  
2. `< min_rows`(默认 2) → 日志 `rows=N < min_rows=2 (data accruing)`，exit 0，**不写** md  
3. ≥2 行 → `exports/reports/cex_scope_compare_YYYYMMDD.md`（序列表 + 对照表）  
4. 链上存活优先读 `self_db.survive['1h'|'4h']`（fraction）；缺则 N/A  
5. **只观察不调权**；CEX 视野 ≠ memecoin 总体

### ② listing_watch
1. `ccxt.<exchange>().load_markets()` **全量** symbol（非 top50）  
2. diff vs `cex_listing_events` 已知 → 新 symbol INSERT；`UNIQUE(exchange, symbol)` 幂等  
3. `listing_ts` = 首次全量见到的 epoch **seconds**（冷启动会把存量全记入）  
4. 默认交易所：`CCXT_EXCHANGES` 或 `binance,okx,kucoin`；单所失败 warning 跳过  
5. **不写** `ccxt.db`；只写 `prior.db.cex_listing_events`  
6. matched / 上线后 1h 表现：本步周报填 **N/A**（深挖未启）

---

## 4. 运行

```bash
cd /workspace/dexscreener-crawler

# ① 对照（当前 1 行预期 accruing）
.venv/bin/python scripts/cex_scope_compare.py

# ② 精确上线（本机 Binance 常 451 → 冒烟用 okx/kucoin）
.venv/bin/python scripts/ccxt_listing_watch.py --once --exchanges okx,kucoin
# 常驻（≥1800s，对齐上线公告节奏）
.venv/bin/python scripts/ccxt_listing_watch.py --loop --interval 1800 --exchanges okx,kucoin
```

---

## 5. 周报

| 节 | 数据 | 缺数据 |
|----|------|--------|
| §6e | 最新 `cex_scope_compare_*.md` | 「数据积累中」不失败 |
| §6f | `cex_listing_events` 近 7 天 | 「暂无数据」不失败；matched/1h = N/A |

---

## 6. 验收

| # | 检查 | 期望 |
|---|------|------|
| 1 | compare（1 行） | exit 0 + `rows=1 < min_rows=2 (data accruing)` |
| 2 | compare（≥2 行） | 出 `cex_scope_compare_*.md` 含对照表 |
| 3 | listing_watch `--once` | 首跑全量入库（n 大，冷启动预期） |
| 4 | 幂等 | 立刻重跑 0 新增 |
| 5 | 隔离 | 本脚本不写 `ccxt.db`；不调权/不进决策 |
| 6 | 周报 §6e/§6f | 已挂；缺数据不失败 |
| 7 | 分支 A 深挖 | **未启**（只建事件表；脚注激活条件） |

---

## 7. 不做

- 不启 `cex_chain_deepdive` / 不建 `cex_prior_linkage`  
- 不改 `.env` / 密钥 / 交易主流程 / `INDICATORS_WEIGHT_*`  
- 不提交 `*.db` / `.env` / keys；不 git push / 不开 PR  
- 不把 listing 事件接入评分或告警
