# Indicators Step 3 — X 情绪面（sentiment）指标

## 原则

- **非侵入旁路**：只读现有 `x_watch` / `alerts`（`social_spike` / `sentiment_anomaly`）以及可选的 `indicator_history` 快照；**只写** `token_indicators` 的 `sentiment_*` 字段，以及（热档）`indicator_history` 中的情绪列。
- **不发 HTTP**：不调用 X API；拉取仍由 `main.py --sources x` / `sources.x_api` 负责。
- **不接线评分/告警**：不改 `scoring.py`、告警规则；`INDICATORS_WEIGHT_SENTIMENT` 默认 **0**（composite 仍为 stub）。
- **异常只记日志**：单 token / pass 失败不影响 technical / funding / 主爬虫。
- **勿停**已有 `main.py` / `indicators.run --loop` / `sources.jev --loop`。

## 新增 / 修改文件

```
indicators/sentiment.py           # 填充 stub：mention / growth / heat / polarity
indicators/base.py                # 读 x_watch/alerts；列迁移；history 扩展
indicators/run.py                 # --full/--loop 调用 sentiment
indicators/__init__.py            # 0.3.0-step3
models/indicator_tables.sql       # sentiment_* 扩展列
config.py                         # SENTIMENT_*；WEIGHT_SENTIMENT 默认 0
env.example                       # SENTIMENT_* 注释
INDICATORS_STEP3.md               # 本文
```

未改：`store.py` 主 schema、`scoring.py`、告警默认值、爬虫节奏。

## 数据源现状（重要）

| 表 | 可用字段 | 说明 |
|----|----------|------|
| `x_watch` | `mention_count_last/prev/total`、`last_searched_at`、`created_at` | X 搜索只持久化计数与 `since_id`，**不存推文正文** |
| `alerts` | `social_spike` / `sentiment_anomaly` + message/payload | 用于 spike 加权与弱 polarity 偏置 |
| `indicator_history` | 热档情绪快照 | 有历史后用于 1h/6h/24h growth 基线 |

因此：mention 窗口与 growth 是对计数器的**近似**；keyword polarity 在有正文时可用（见下方单元函数），线上目前主要靠 spike/anomaly + growth 启发式。

## 表结构（Step 3 扩展，均可空）

### `token_indicators`

| 列 | 含义 |
|----|------|
| `sentiment_mentions_1h` | ≈ `mention_count_last`（最近一次搜索窗口） |
| `sentiment_mentions_6h` | 年龄 ≤6h 用 total，否则 `max(last, total×6/age_h)` |
| `sentiment_mentions_24h` | 年龄 ≤24h 用 total，否则按 24h 比例缩放 |
| `sentiment_growth_1h` | 有 ~1h 历史则相对基线；否则 `(last−prev)/prev`（prev=0 且 last>0 → 1.0） |
| `sentiment_growth_6h` / `_24h` | 相对历史基线，或相对窗口内平均速率 |
| `sentiment_heat_score` | 热度 0–100（见公式） |
| `sentiment_polarity` | 极性 \[−1, 1\]（关键词 + 告警类型 + growth） |
| `sentiment_score` | `(heat/100) × polarity`，约 \[−1, 1\]（供日后 composite） |
| `sentiment_updated_at` | 来自 `last_searched_at` / `updated_at` |

`IndicatorStore.upsert_token_indicators` 仍为**部分更新**：sentiment 与 technical / funding 互不覆盖。

### `indicator_history`（热档）

新增可空：`sentiment_mentions_1h`、`sentiment_heat_score`、`sentiment_polarity`（另有既有 `sentiment_score`）。

## 公式

### 热度 `sentiment_heat_score`

```
base         = log10(mentions_1h + 1) / 2.5 * 60
growth_boost = clip(growth_1h, 0, 3) * 10
spike_boost  = 20 (anomaly) | 12 (spike) | 0
heat         = clip(base + growth_boost + spike_boost, 0, 100)
```

### 极性 `sentiment_polarity`

1. **关键词**（`score_polarity_from_texts`）：bull/bear 词表命中差比 → \[−1,1\]  
   bull 例：ape / parabolic / moon / pump / bullish / lfg …  
   bear 例：dump / rug / scam / bearish / honeypot …
2. **告警偏置**：近 `SENTIMENT_ALERT_LOOKBACK_SEC`（默认 6h）内  
   `sentiment_anomaly` → +0.55；`social_spike` → +0.35
3. **growth 偏置**：`clip(growth_1h × 0.15, −0.35, 0.35)`
4. 混合：有关键词命中时 `0.6·kw + 0.25·alert + 0.15·growth`；否则 `0.65·alert + 0.35·growth`

### 跳过规则

- `SENTIMENT_ENABLED=0` → 整批 no-op  
- 无 `x_watch` 行 → skip  
- 从未搜索且 total/last/prev 全 0 → skip（避免空壳）

## 配置

| 变量 | 默认 | 含义 |
|------|------|------|
| `SENTIMENT_ENABLED` | `1` | 计算开关（默认开） |
| `SENTIMENT_WRITE_HISTORY` | `1` | 热档写 `indicator_history` |
| `SENTIMENT_ALERT_LOOKBACK_SEC` | `21600` | social 告警回看（6h） |
| `INDICATORS_WEIGHT_SENTIMENT` | `0` | composite 权重占位（Step 3 不接线） |

## 如何运行

仓库根目录 `/workspace/dexscreener-crawler`：

```bash
# 全量（technical + funding + sentiment）
python -m indicators.run --full

# 或仅依赖已在跑的 loop（需重启 loop 后才加载本 Step 代码；本轮用 --full 旁路验证）
# 不要 pkill 已有 main / indicators / jev
```

## 验证结果（2026-10-10 约 04:09 HKT）

### 进程（未中断）

| 进程 | 状态 |
|------|------|
| `main.py --helius-listen --sources x --loop` | 仍在运行 |
| `indicators.run --loop --interval 300` | 仍在运行（旧进程；本轮用独立 `--full` 写入） |
| `sources.jev --loop --interval 900` | 仍在运行 |

### `--full` 统计

| 阶段 | seen | ok | skip | err |
|------|------|----|------|-----|
| technical | 1346 | 1346 | 0 | 0 |
| funding | 73 | 73 | 0 | 0 |
| **sentiment** | **62** | **22** | **40** | **0** |

- `x_watch` 共 62 行；40 行为从未搜索的空壳（skip）  
- 22 行写入 `sentiment_*`；其中 **8** 行 `sentiment_heat_score > 0`（有 mention）

### 样本（有 X mention 的 token）

| token 前缀 | symbol | m1h | heat | polarity | score | g1h |
|------------|--------|-----|------|----------|-------|-----|
| `apnswruavu1e…` | Speed | 10 | 46.99 | 0.28 | 0.132 | 1.0 |
| `26o13evr3zwc…` | STONK | 10 | 46.99 | 0.28 | 0.132 | 1.0 |
| `qy5thanggdjq…` | Reinvest | 10 | 46.99 | 0.28 | 0.132 | 1.0 |
| `dwrrbyp65oo3…` | CEZ | 10 | 46.99 | 0.28 | 0.132 | 1.0 |
| `borqazweansq…` | James | 10 | 46.99 | 0.28 | 0.132 | 1.0 |
| `czeyh6zkrwrx…` | SOLBULL | 2 | 21.45 | 0.05 | 0.011 | 1.0 |
| `ajhipmqyazcs…` | — | 1 | 17.22 | 0.05 | 0.009 | 1.0 |

说明：上述 polarity≈0.28 来自 `social_spike` 告警偏置 + growth（推文正文未入库，关键词贡献为 0）。  
fixture `fixtures/x_search_recent.json` 关键词单测 polarity = **1.0**（aped / parabolic）。

## 后续可选

- 若需更准的 keyword polarity：在 `x_api` 侧可选持久化少量 tweet 正文（另开任务；本 Step 只读）。  
- 重启 `indicators.run --loop` 后常驻进程才会跑新 sentiment 代码（本轮故意未杀旧 loop）。  
- composite / scoring 接线前保持 `INDICATORS_WEIGHT_SENTIMENT=0`。

## Grok DD 报告接入（只读输出）

`scripts/grok_dd_report.py` 从 `token_indicators` 读取 `sentiment_*`（mention / growth / heat / polarity / score），写入 markdown 的 **舆情面 Sentiment** 段；缺字段时显示「暂无数据」，不导致报告失败。`prompts/dd_report.md` 已增加对应章节。不改评分 / 告警 / 循环。

## 突热 spike 标记（observe-only）

见 **[SENTIMENT_SPIKE.md](SENTIMENT_SPIKE.md)**：`spike_ratio = mentions_1h / max(mentions_24h/24, 1)`；`spike=1` iff `>= SENTIMENT_SPIKE_MIN`（默认 3）。只写指标表；突热=滞后确认，非买入信号；先验 `peak_window` 见顶极快。

