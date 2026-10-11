# CCXT Prior Step 4 — 视野对照（分支 B）+ 精确上线事件（分支 A 的前置，未深挖）

> 状态：①② 已落地（observe-only，默认关）。分支 A per-token 深挖仍不启。  
> 范围：对照脚本只读 `prior.db`；上线事件只写 `prior.db.cex_listing_events`。  
> 不读不写 `ccxt.db`。不调权、不进决策、不写业务四库。不提交 `*.db` / `.env` / 密钥。

## 和 Step 2 / Step 3 的关系

- **Step 2** 的 `ccxt_listings` × 链上 join 匹配率脏（符号撞车）。本步不重做那次 join，也不把 matched 当因果。
- **Step 3 分支 B** 把 top50 视野收成 `cex_scope_snapshots`（周报 §6d）。本步在它后面加对照（§6e）。快照不足 2 行时对照脚本不落盘，周报写「数据积累中」。
- **Step 3 分支 A**（`cex_chain_deepdive` / `cex_prior_linkage` / per-token 深挖）仍然挂起。Step 4 只把「全量 symbol diff」记进 `cex_listing_events`，给以后的干净 join 当事件源。激活条件：本表与链上按同一 symbol 归一能对上之后，再单独立项。在那之前 matched 与上线后 1h 表现在周报里是 **N/A**。

## ① 对照分析

| 文件 | 作用 |
|------|------|
| `scripts/cex_scope_compare.py` | 读 `cex_scope_snapshots`（`mode=ro`）。`<2` 行 exit 0，日志 `data accruing`，不写报告。`≥2` 行写 `exports/reports/cex_scope_compare_YYYYMMDD.md` |
| `review/data.py` `load_cex_scope_compare` | 读最新那份 md 的首表和对照表。读不到 `available=False` |
| `review/report.py` **§6e** | 「CEX 视野对照」。无报告「数据积累中」；文件在但表空「暂无数据」。不失败、不调权 |
| `review/run.py` | 出周报时把 loader 传进 §6e |

默认路径（脚本在 `crawler/scripts/`，`_ROOT` 是 crawler）：

- 读：`crawler/data/prior/db/prior.db`，`--db-prior` 可覆盖。baseline：`data/prior/exports/baseline_params.json` 的 `self_db.survive_1h_pct` / `survive_4h_pct`。文件缺失或坏 JSON 写 N/A，不失败。
- 写：`crawler/exports/reports/cex_scope_compare_YYYYMMDD.md`，`--out` 可覆盖目录。

对照表把最新快照的新进者 Δ24h 中位、视野留存（`n_still_visible / n_total`）和链上 1h/4h 存活并列。CEX 视野偏主流/高流动性，与 memecoin prior 不是同一总体；结论等 ≥5 行快照。

## ② 精确上线事件

| 文件 | 作用 |
|------|------|
| `scripts/ccxt_listing_watch.py` | `load_markets()` 全量 symbol 集（无 topN）对 `cex_listing_events` 做差集 |
| `models/cex_listing_events.sql` | 表结构文档副本（脚本内联建表） |
| `review/data.py` `load_cex_listing_events` | 近 7 天按交易所计数。无事件 `available=False`。matched / 1h = N/A |
| `review/report.py` **§6f** | 「CEX 精确上线事件」。有行出表，无事件「暂无数据」 |

```bash
cd crawler
.venv/bin/python scripts/ccxt_listing_watch.py --once
.venv/bin/python scripts/ccxt_listing_watch.py --loop --interval 1800
.venv/bin/python scripts/ccxt_listing_watch.py --once --exchanges okx,kucoin
```

`--exchanges` 缺省时用环境变量 `CCXT_EXCHANGES`，再缺省 `binance,okx,kucoin`。只从 `.env` 读这一键，不打印密钥。单所 `load_markets` 失败打 warning 并跳过，不杀掉其它所、也不让整轮非 0 退出。脚本不 `import sources`（该包初始化会拉起 Helius）。

`listing_ts` 是 **unix 秒**。`ccxt.db` 里的 `ts` / `first_seen_ts` 是 epoch 毫秒，不要混比。

幂等键：`UNIQUE(exchange, symbol)` + `INSERT OR IGNORE`。差集相对已入库 symbol。

### 冷启动全量入库（预期）

第一次 `--once` 会把每所 `load_markets()` 返回的**全部**交易对插进去，行数大约等于所方交易对总数（经常是几千），`inserted` 等于 `symbols`。这不是「当天真实新上线」，只是「首次全量见到」的时间戳。

第二次跑同一所：已知集合覆盖当前全量，`inserted=0`。从下一次开始，只有交易所真正多出来的 symbol 会新增一行。

USDT/TRY 这类 quote-like base **保留**。和链上对齐时再滤，不在入库时丢掉。

## 验收

| # | 检查 | 期望 |
|---|------|------|
| 1 | `cex_scope_compare.py`，快照 1 行 | exit 0，日志含 `rows=1 < min_rows=2 (data accruing)`，不写 md |
| 2 | 快照 ≥2 行 | 写出 `cex_scope_compare_*.md`，含对照表；baseline 缺失则存活率为 N/A |
| 3 | `ccxt_listing_watch.py --once` | exit 0；首次把该所全量 symbol 入库 |
| 4 | 幂等 | 同一 symbol 集重跑新增 0 |
| 5 | 隔离 | 不打开 `ccxt.db`；不改权重 / 闸门 / 决策 |
| 6 | 周报 | §6e / §6f 出现；缺 compare md「数据积累中」，无上线事件「暂无数据」，不失败 |
| 7 | 分支 A | **未启**。等 `cex_listing_events` 与链上 join（同 symbol 归一）匹配后再做 per-token 深挖 |

## 怎么跑（人工，默认关）

```bash
cd crawler
.venv/bin/python scripts/cex_scope_compare.py
.venv/bin/python scripts/ccxt_listing_watch.py --once
.venv/bin/python -m review.run --period 7d --report
```

主爬虫、决策引擎、评分、告警都不会拉起这两个脚本。
