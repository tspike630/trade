# 新闻旁路（News Sidechannel）

> 状态：只读门控已接入五态路由；历史阈值**未校准**（2026-11-10）  
> 范围：公开 RSS → 独立库；白名单危机词 / Fed 鹰派只读叠加  
> **不写** `decision_log`，不下单，不把 `require_perp_beta_hedge` 当成已经对冲

---

## 1. 为什么独立库

| 选项 | 结论 |
|------|------|
| 主库 `dexscreener.db` | 会与 crawl / JEV / indicators 争用 WAL；误写风险更高 |
| **独立 `news_events.db`** | **选用**：零侵入、可单独删/备份、失败不影响主链路 |

默认路径：仓库根目录 `news_events.db`（可用 `--db` 覆盖）。

---

## 2. 模块与表

| 文件 / 对象 | 作用 |
|-------------|------|
| `sources/news.py` | RSS 拉取 + 解析（stdlib `xml.etree` + 已有 `httpx`，无 feedparser） |
| `news_events.db` → `news_events` | 观察事件表；`url` UNIQUE 幂等去重 |
| `NEWS_SIDECHANNEL.md` | 本说明 |

### 字段

`id, source, source_tier, title, url, published_at, fetched_at, summary, raw_json, onchain_status, notes`

`onchain_status`：`pending`（默认）/ `aligned` / `divergent` / `none`  
脚手架只写入 `pending`；链上验证器尚未接入。

### 源分级（写死表，可配置）

`SOURCE_CATALOG` in `sources/news.py`：

| tier | 含义 | 当前示例 |
|------|------|----------|
| `whitelist` | 主流公开媒体 RSS 与官方 Fed | CoinDesk / The Block / Cointelegraph / DL News / Decrypt / `fed_monetary` / `fed_speeches` |
| `rumor` | 低置信度观察源 | NewsBTC（只记，不得单独升格） |

- DefiLlama 主站无公开 RSS；同系 **DL News**（`dlnews.com`）RSS 作为 DefiLlama 新闻旁路入口。
- `--list-sources` 可打印；`--sources id1,id2` 可子集拉取。

---

## 3. CLI

```bash
# 单次拉取入库（验收）
.venv/bin/python -m sources.news --once

# 列出源
.venv/bin/python -m sources.news --list-sources

# 指定库 / 子集
.venv/bin/python -m sources.news --once --db news_events.db --sources coindesk,theblock
```

输出示例：`written=N seen=M dup=K ok_sources=… fail_sources=… db_total=…`  
单源网络失败 → 该源降级记入 `failures`，其它源继续（优雅降级）。

幂等：同一 `url` 再次拉取 → `INSERT OR IGNORE`，计为 `dup`，不重复写。

---

## 4. 事件流水线（设计；本脚手架只做第 1 步）

```
新闻 RSS 入库 (onchain_status 仍写 pending)
        │
        ▼
   只读验证（无校准配置时不改标签）
   · 热度演变 + obi + taker + whale 齐全才允许 aligned / divergent
        │
        ├─ 证据不全或未校准 → pending
        ├─ 显式校准且同向 → aligned（只参与已有龙头排序）
        └─ 显式校准且反向 → divergent（仅在状态 3 拒买）
```

### 三机制（跨门槛前一律观察）

1. **候选（candidate）**  
   whitelist 新闻 + 链上 `aligned` → 记为候选事件；**仍不写 decision_log**，仅备日后人工/门槛闸后接决策。

2. **疑似出货（suspected dump）**  
   利好叙事但链上 `divergent`（如同步大额卖出/流动性抽离）→ 标记疑似出货，供风控旁路查看。

3. **只记（record-only）**  
   rumor 源、无法验证、或信息噪声 → `onchain_status=none` / notes 说明；永久观察，不升格。

---

## 5. 与 JEV / 决策的关系

| 组件 | 关系 |
|------|------|
| JEV (`sources.jev` / `jev_signals`) | **并行不冲突**；新闻旁路不读不写 JEV 表 |
| Indicators 旁路 | 互不依赖；新闻不写 `token_indicators` |
| Decision engine / `decision_log` | 拉取仍不写 `decision_log`。五态路由只读消费近 90 分钟白名单标题；缺库、锁库、超时、坏表都放行，并在 `news_gate` 里留下原因 |
| paper / live / execution | 本改动不启动交易、不读密钥、不把对冲标签当成已成交 |
| 排序权重 | `aligned_rank_weight` 默认关闭。链上 `aligned` / `divergent` 在没有显式校准对象时保持 `pending` |

原则：**先观察、再验证、后候选；跨门槛后再接决策。**

---

## 6. 验收要点

- [x] `python -m sources.news --once` 可跑，打印 `written=` 条数
- [x] `news_events.db` 可查（`SELECT COUNT(*) FROM news_events`）
- [x] url 去重幂等
- [x] 单源失败不拖垮整轮
- [x] 未改 JEV / paper / live 执行器；决策侧只增加只读快照
- [ ] 历史收益、误报率、置信区间**尚未**用生产库校准，禁止把描述统计打开成权重

官方源：`fed_monetary` = `https://www.federalreserve.gov/feeds/press_monetary.xml`，`fed_speeches` = `https://www.federalreserve.gov/feeds/speeches.xml`。现有入口仍是 `python -m sources.news --once`，不带 `--sources` 时会拉这两路。不要再挂第二个定时器。

每次轮询把见到的新闻行写到独立的 `news_feature_observation_log.db`（可用 `NEWS_FEATURE_OBS_LOG` 改路径）。源库只读查询，不改 `onchain_status`，不改权重。没有币种就不猜。没有 X 新闻库就记 `unavailable`，不造 X 事件。`orderbook_imbalance_20` 与 `perp_taker_buy_sell_ratio` 各写各的列。两周只是积样，不自动开参数。

可交易时间是 `fetched_at`。`published_at` 比入库早超过 90 分钟的迟到旧稿不触发。链上判断要本 tick 的 `obi_top5`（没有这一列时不能用 `orderbook_imbalance_20` 顶替）、`taker_buy_ratio`（不用 `perp_taker_buy_sell_ratio`）和 `whale_retail_divergence`，再加上热度演变。缺任何一项保持 `pending`。

2026-11-10 14:37:32 HKT 的本机只读盘点（云端没有这些库，数字是引用不是重算）：`news_events` 1869 条，whitelist 1645 / rumor 224，`onchain_status` 全部 pending，所以误报率不算。没有独立 X 新闻库。`x_watch` 64 条且停在 2026-10-31。币安 5m 只能对齐 375 条，OKX 窗口另有 723 条，两个场所不拼接。币安 BTCUSDT 描述均值约 +5m -0.069%、+1h -0.116%（n=372）、+24h -0.395%（n=245）。可对齐窗口里危机词 0 条、鹰派/Fed 词 1 条。这些数字不启用阈值。

审计（只出报告，不写阈值）：

```bash
python3 -m decision_engine.core.news_shock_audit --news-db news_events.db
```

时间按 `published_at` 切 60/20/20，url 与 6 小时内同题去重，Wilson 区间，短语族 × 15/60/240 分钟要做多重检验校正。特征时间必须不晚于新闻，价格开盘时间必须不早于新闻。没有币种映射时不猜测标的。
