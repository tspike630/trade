# CCXT × Prior Step 3 — CEX 新币视野监控（分支 B only）

> 状态：落地 2026-10-12  
> 对应：QUANT/方案2 Step 3 **仅分支 B**  
> **严格旁路**：只读 `ccxt.db`；只写 `prior.db.cex_scope_snapshots`  
> **默认关 / 不调权**：不进评分、决策、告警、业务四库

---

## 1. 范围

| 分支 | 状态 | 说明 |
|------|------|------|
| **B** CEX 视野监控快照 | ✅ 本步落地 | 日快照 → `cex_scope_snapshots` |
| **A** 传导链 deepdive | ⏸ **挂起** | Step2 匹配脏（symbol 撞车），传导链暂无法验证；**不写/不跑** `cex_chain_deepdive`；**不建** `cex_prior_linkage` |

挂起原因（Step2 验收）：`prior_cex_listing_join` 同 ticker 多 mint 撞车严重，matched 语义脏，无法支撑「链上新币 → CEX → 拉升」per-token 时间线。待干净事件源或消歧后再启 A。

---

## 2. 交付物

| 路径 | 作用 |
|------|------|
| `scripts/cex_scope_watch.py` | 日快照（`--once` / `--loop`） |
| `models/cex_scope_tables.sql` | 表结构文档副本（脚本内联建表） |
| `review/data.py` → `load_cex_scope` | 只读加载最近 7 日快照 |
| `review/report.py` → §6d | 周报「CEX 新币视野」小节（缺数据「暂无数据」不失败） |
| 本文 | 口径 / 用法 / 验收 |

---

## 3. 口径

1. **进入视野** ≈ 旁路 `ccxt_listings` 首次见到（进入 top50 成交量近似），**≠** 交易所官方上线日  
2. **时间单位**：`ccxt_listings.first_seen_ts` / `ccxt_ticks.ts` 为 epoch **ms**；窗口 = `now_ms − 86400000`；`snapshot_ts` 存 epoch seconds  
3. **指标**（每日 UTC `snap_date` 1 行，upsert 幂等）  
   - `n_total`：累计 listings  
   - `n_new_24h`：近 24h 新进入  
   - `n_still_visible`：近 24h 仍有 tick 的 DISTINCT symbol  
   - `med_delta24h_new`：新进入者 `change_24h_pct` 中位  
4. **只观察**：与链上 `self_survival` 对照用；不调权、不算 uplift

---

## 4. 运行

```bash
cd /workspace/dexscreener-crawler
.venv/bin/python scripts/cex_scope_watch.py --once
# 可选常驻（每小时一轮，同日 upsert 不翻倍）
.venv/bin/python scripts/cex_scope_watch.py --loop --interval 3600
```

路径默认：`ccxt.db`（仓根）、`data/prior/db/prior.db`；可用 `--db-ccxt` / `--db-prior` 覆盖。

---

## 5. 验收

| # | 检查 | 期望 |
|---|------|------|
| 1 | `--once` | exit 0；日志含 date / n_total / n_new_24h / visible / med |
| 2 | 幂等 | 同日重跑仍 1 行（`UNIQUE(snap_date)` + `ON CONFLICT`） |
| 3 | 隔离 | `ccxt.db` mtime 未因本脚本写入变；权重/闸门未动 |
| 4 | 周报 | §6d 出现；无快照时「暂无数据」不失败 |
| 5 | 分支 A | **不实现、不跑**（Step2 脏匹配挂起） |

---

## 6. 不做

- 不实现 / 不跑 `cex_chain_deepdive.py`  
- 不建 `cex_prior_linkage`  
- 不改 `.env` / 密钥 / 交易主流程 / `INDICATORS_WEIGHT_*`  
- 不把 `*.db` 当交付物提交；不 git push / 不开 PR

---

## 7. 后续

Step 4（对照分析 + 精确上线事件表）见 `CCXT_PRIOR_STEP4.md`：`cex_scope_compare` / `ccxt_listing_watch` → §6e/§6f；分支 A 深挖仍未启。
