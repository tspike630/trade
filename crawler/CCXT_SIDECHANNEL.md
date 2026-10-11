# CCXT 行情旁路（CCXT Sidechannel）

> 状态：Step 1 + Step 2（联合归因观察）落地（2026-10-12）  
> 范围：CCXT 公开行情（tickers / OHLCV）→ 独立库 `ccxt.db` 观察入库  
> **严格旁路**：不接决策引擎、不改 JEV/评分/告警、不碰 paper/live、不写业务四库  
> 体例对齐：`sources.jev`（旁路 → 入库 → 验证相关性后再加权）

---

## 1. 为什么独立库

| 选项 | 结论 |
|------|------|
| 主库 `dexscreener.db` | 会与 crawl / JEV / indicators 争用 WAL；误写风险更高 |
| **独立 `ccxt.db`** | **选用**：零侵入、可单独删/备份、失败不影响主链路 |

默认路径：仓库根目录 `ccxt.db`（可用 `--db` 或 `CCXT_DB_PATH` 覆盖）。

公开行情无需交易所 API Key；本旁路不打印任何密钥。

---

## 2. 模块与表

| 文件 / 对象 | 作用 |
|-------------|------|
| `sources/ccxt/config.py` | env + 默认（独立配置，不碰根 `config.py`） |
| `sources/ccxt/core.py` | 抓取 + 幂等写入 + listings 发现 |
| `sources/ccxt/run.py` | CLI（`--once` / `--loop` / `--interval` / `--limit` / `--exchanges`） |
| `models/ccxt_tables.sql` | 表结构文档副本（core 已内联建表） |
| `ccxt.db` | 旁路库 |

### 表

| 表 | 作用 | 幂等键 |
|----|------|--------|
| `ccxt_ticks` | 每轮 top 行情快照 | `UNIQUE(exchange, symbol, ts)` |
| `ccxt_ohlcv` | K 线 | `UNIQUE(exchange, symbol, timeframe, ts)` |
| `ccxt_listings` | 新 symbol 首次发现（CEX 上线事件雏形） | `UNIQUE(exchange, symbol)` |

说明：`ccxt_ticks` 每轮一个新 `ts`，重跑会产生新快照行（时序预期）；幂等验证看 `ccxt_ohlcv` 与 `ccxt_listings`。

---

## 3. CLI

```bash
cd /workspace/dexscreener-crawler

# 单跑验证（小范围）
.venv/bin/python -m sources.ccxt --once --exchanges binance --limit 20

# 全量单跑（默认 binance,okx,kucoin top 50）
.venv/bin/python -m sources.ccxt --once

# 常驻（对齐 sources.jev --loop）
nohup .venv/bin/python -m sources.ccxt --loop --interval 300 >> logs/ccxt_sidechannel.log 2>&1 &
```

### 环境变量（可选）

| 变量 | 默认 | 含义 |
|------|------|------|
| `CCXT_DB_PATH` | `ccxt.db`（仓库根） | 旁路库路径 |
| `CCXT_EXCHANGES` | `binance,okx,kucoin` | 交易所列表 |
| `CCXT_TIMEFRAME` | `5m` | K 线周期 |
| `CCXT_TOP_LIMIT` | `50` | 每所按 24h quote 成交量取 top N |
| `CCXT_INTERVAL_SEC` | `300` | `--loop` 间隔秒 |
| `CCXT_MIN_QUOTE_VOLUME` | `100000` | 过滤阈值 |
| `CCXT_OHLCV_LIMIT` | `60` | 每 symbol 拉多少根 K |

---

## 4. 默认关

- 根 `config.py` / `main.py` **无** CCXT 引用；主爬虫不强制启动本旁路
- 不接决策 / 评分 / 告警 / Telegram
- 需要时人工 `--once` 或 `--loop` 启用

---

## 5. 查数

```bash
sqlite3 ccxt.db "SELECT exchange, count(*) FROM ccxt_ticks GROUP BY 1;"
sqlite3 ccxt.db "SELECT count(*) FROM ccxt_ohlcv;"
sqlite3 ccxt.db "SELECT * FROM ccxt_listings ORDER BY first_seen_ts DESC LIMIT 10;"
```

---

## 6. 周报旁挂（复盘只读）

`review/data.load_ccxt_market_baseline` → `review/report.py` **§6b**「CEX 市场基准第二参照」：只读汇总 ticks/listings/ohlcv，最近一轮 `change_24h_pct` 中位对照 §6 hot。**只观察不调权**；缺库跳过。验收：`.venv/bin/python -m review.run --period 7d --report`。

## 7. Step 2：CEX listings × 链上先验联合归因（观察 only）

已落地 one-shot：`scripts/prior_cex_listing_join.py` → `data/prior/exports/cex_prior_join_YYYYMMDD.{md,json}`；可选 merge `baseline_params.json["cex_listing_join"]`。

- **口径 / 默认关 / 不调权**：见 `CCXT_PRIOR_STEP2.md`
- 只读 `ccxt.db` + `prior.db` + `dexscreener.db`；不写业务四库 / 权重 / 告警
- **周报不自动挂**：§6b 仍只做 CEX 市场基准；join 摘要是否旁挂留给复盘
- 仍须更大 n + 真上线事件源，验证「链上新币 → CEX 上线 → 拉升」后再谈评分/决策

---

## 8. 本机注意（2026-10-11 冒烟）

- 本执行环境访问 Binance 公共 API 返回 **451 geo-restricted**；`--exchanges binance` 会 warning 后跳过，不影响其它所。
- 验收冒烟用：`.venv/bin/python -m sources.ccxt --once --exchanges okx,kucoin --limit 20`
- 默认 `CCXT_EXCHANGES=binance,okx,kucoin` 仍按方案保留；常驻可按环境改 env / `--exchanges`。
