# CCXT Prior Step 3 — 只做分支 B（CEX 新币视野）

> 状态：分支 B 已落地（observe-only，默认关）。分支 A 挂起。  
> 范围：只读 `ccxt.db`，只写 `prior.db` 快照表 `cex_scope_snapshots`。  
> 不调权、不进决策、不写业务四库。不提交 `*.db` / `.env` / 密钥。

## 和 Step 2 的关系

Step 2 已验收通过，但带偏差：匹配率脏（符号撞车），传导链暂无法验证。

执行单原意是「分支 B 不依赖 Step 2；分支 A 仅在 matched>0 时启用」。按这次验收：

- **分支 B 照常做**：把 `ccxt_listings` 收成每日视野快照，给 CEX 新币表现基线。
- **分支 A 挂起**：不实现、不运行 `scripts/cex_chain_deepdive.py`，不建 `cex_prior_linkage`，不拿脏 matched 做 per-token 深挖。

仓库里没有 `CCXT_PRIOR_STEP2.md`。旁路索引在 `CCXT_SIDECHANNEL.md` §7，已注明「B 已落地、A 挂起」。

## 分支 B 做了什么

| 文件 | 作用 |
|------|------|
| `scripts/cex_scope_watch.py` | 日快照。`--once` 单跑；`--loop --interval 3600` 可选常驻（本步不启动） |
| `models/cex_scope_tables.sql` | 表结构文档副本（脚本内联建表） |
| `review/data.py` `load_cex_scope` | 只读最近 7 条快照；缺库/缺表不抛错 |
| `review/report.py` **§6d** | 「CEX 新币视野」只观察。有行出表，无数据写「暂无数据」 |
| `review/run.py` | 出周报时把 `load_cex_scope` 传进 §6d。不改指标、闸门、权重 |

默认路径：

- 读：与 `sources/ccxt/config.py` 相同的默认路径（环境变量 `CCXT_DB_PATH`，否则 `crawler/ccxt.db`），可用 `--db-ccxt` 覆盖。脚本不 `import sources`（该包初始化会拉起 Helius）。连接 `mode=ro` + `PRAGMA query_only`。
- 写：`crawler/data/prior/db/prior.db` 的 `cex_scope_snapshots`，可用 `--db-prior` 覆盖。

`ccxt_ticks.ts` 与 `ccxt_listings.first_seen_ts` 在 Step 1 里是 **epoch 毫秒**（`sources/ccxt/core.py` 用 `time.time()*1000`）。24h 窗口按毫秒比较。快照列 `snapshot_ts` 仍是 unix 秒。

指标：

- `n_total`：`ccxt_listings` 累计行数
- `n_new_24h`：`first_seen_ts` 落在近 24h 的 listings
- `n_still_visible`：近 24h `ccxt_ticks` 的 distinct symbol
- `med_delta24h_new`：近 24h 新进入者的 `change_24h_pct` 中位（排序后取 `len//2`；无样本为 NULL）

「进入视野」= 进入 top50 成交量，是近似，不是精确上线时刻。

## `--once` 幂等

`snap_date`（UTC `YYYY-MM-DD`）有 `UNIQUE`。写入是：

```sql
INSERT INTO cex_scope_snapshots (...) VALUES (...)
ON CONFLICT(snap_date) DO UPDATE SET
  snapshot_ts=excluded.snapshot_ts,
  n_total=excluded.n_total,
  ...
```

同一 UTC 日重跑覆盖当日那一行，行数不翻倍。跨天新插一行。

## 验收（代码侧）

| # | 检查 | 状态 |
|---|------|------|
| 1 | `--once` 日志含 date / n_total / n_new_24h / visible / med | 脚本已实现；`python -m py_compile` 通过 |
| 2 | `cex_scope_snapshots` 当日 1 行，重跑不翻倍 | `UNIQUE(snap_date)` + `ON CONFLICT` |
| 3 | 只读 ccxt；不改权重/闸门/决策/业务库 | `mode=ro`；无决策/评分引用 |
| 4 | 周报 §6d：有数据出表，无数据「暂无数据」且不失败 | `load_cex_scope` 吞掉 SQLite 错误 |
| 5 | 分支 A | **未做**（见下） |
| 6 | `--loop` | 脚本支持；本步不挂常驻 |

## 分支 A：未做

以下都没有落地，也不要补跑：

- 没有 `scripts/cex_chain_deepdive.py`
- 没有 `cex_prior_linkage` 表，也没有建表 SQL
- 不读 Step 2 的 matched 行，不出传导链时间线卡片
- 周报不加传导链深挖小节

传导链要等匹配口径干净之后再单独立项。在那之前 §6d 只提供 CEX 视野基线，不解释「链上新币 → CEX → 拉升」。

## 怎么跑（人工，默认关）

```bash
cd crawler
.venv/bin/python scripts/cex_scope_watch.py --once
.venv/bin/python -m review.run --period 7d --report
```

主爬虫、决策引擎、评分、告警都不会拉起这个脚本。

## Step 4

Step 4 已落地，见 `CCXT_PRIOR_STEP4.md`：`scripts/cex_scope_compare.py`（周报 §6e；快照不足 2 行则「数据积累中」）与 `scripts/ccxt_listing_watch.py`（`prior.db.cex_listing_events`，周报 §6f）。分支 A per-token 深挖仍不启，要等 `cex_listing_events` 与链上按 symbol 归一 join 之后。
