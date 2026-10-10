# 新闻旁路（News Sidechannel）

> 状态：脚手架落地（2026-10-10）  
> 范围：低成本公开 RSS 拉取 → 独立库观察入库  
> **严格旁路**：不接决策引擎、不改 JEV、不碰 paper/live、不写 `decision_log`

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
| `whitelist` | 主流公开媒体 RSS | CoinDesk / The Block / Cointelegraph / DL News / Decrypt |
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
新闻 RSS 入库 (onchain_status=pending)
        │
        ▼
   链上验证（未实现）
   · 价格/成交量/流动性/大额转出等与标题方向对照
        │
        ├─ aligned   → 候选（candidate）
        ├─ divergent → 疑似出货（suspected dump）
        └─ none      → 只记（record-only）
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
| Decision engine / `decision_log` | **禁止写入**；本模块无任何交易信号出口 |
| paper / live / execution | **不碰** |
| 复盘周检门槛 | 历史与信号质量**跨门槛后**，才考虑把「候选」接到决策输入（另立 RFC，不在本脚手架） |

原则：**先观察、再验证、后候选；跨门槛后再接决策。**

---

## 6. 验收要点

- [x] `python -m sources.news --once` 可跑，打印 `written=` 条数
- [x] `news_events.db` 可查（`SELECT COUNT(*) FROM news_events`）
- [x] url 去重幂等
- [x] 单源失败不拖垮整轮
- [x] 未改 JEV / decision / paper / live 代码路径
