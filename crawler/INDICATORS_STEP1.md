# Indicators Step 1 — 技术指标旁路层

## 原则

- **非侵入旁路**：只从现有表（`tokens` / `pairs` / `pair_history`）**读取**；只向新建表 `token_indicators` / `indicator_history` **写入**。
- **独立进程**：`python -m indicators.run`，与 `main.py` 爬虫解耦；指标异常不影响主爬取。
- **只计算、不交易、不做告警决策**。
- Step 1 仅实现 **technical**；`funding` / `sentiment` / `composite` 为 stub。

## 新增文件

```
indicators/
  __init__.py
  base.py          # IndicatorStore：建表、读 pair_history、upsert、短缓存
  technical.py     # 涨跌幅 / 波动率 / 成交量强度 + 分档计算
  funding.py       # stub（JEV 预留）
  sentiment.py     # stub
  composite.py     # stub
  run.py           # CLI：--full / --once / --loop
models/
  indicator_tables.sql
scripts/run_indicators.py
```

**对主链路的改动**：仅 `config.py` 末尾增加 `INDICATORS_*` 开关与间隔；`store.py` / `main.py` / `pipeline.py` / `history.py` **未改**。

## 表结构

- `token_indicators`：PK `(chain_id, token_address)`  
  技术字段：`change_5m/15m/1h/4h/24h`、`volatility_24h`、`volume_strength`  
  预留可空：`jev_*`、`sentiment_*`、`composite_*`  
  元数据：`tier`、`primary_pair_address`、`sample_count`、`updated_at`
- `indicator_history`：热档快照归档（Step 1 对 **hot** 档写入）

建表：打开 `IndicatorStore` 时执行 `models/indicator_tables.sql`（`CREATE IF NOT EXISTS`）。

## 指标定义

| 字段 | 定义 |
|------|------|
| `change_*` | 主 pair 最新价相对 lookback 前快照：`(p_now - p_then) / p_then * 100`。`p_then` 取目标时刻附近快照（允许偏差 `min(max(lookback×0.5, 120s), 6h)`）。 |
| `volatility_24h` | 相对最新快照回看 **24h**（`INDICATORS_VOLATILITY_WINDOW_SEC`）内，相邻价格简单收益率的 **样本标准差 × 100**（百分点）。至少需 3 个有效价格。 |
| `volume_strength` | `current_vol / mean(vol over 24h window)`。优先 `volume_h1`，否则 `volume_h24/24`。 |

主交易对选择：该 token 在 `pair_history` 中 **最新流动性最高**，其次快照数最多的 `pair_address`。

地址规范：与爬虫一致，`norm_chain` / `norm_address`（小写）。

## 分档轮询（复用 `history.history_tier_for_token`）

| 档 | 刷新间隔（config） | 计算字段 |
|----|-------------------|----------|
| hot | 5 min（`INDICATORS_HOT_INTERVAL_SEC`） | 全套 technical |
| warm | 30 min | `change_15m/1h/4h/24h` + `volatility_24h` + `volume_strength` |
| cold | 4 h | `change_1h/24h` + `volume_strength` |

`--full` 忽略分档间隔，按 **hot 全套** 重算所有有历史的 token。

分档 `--once`/`--loop` 只重算该档字段，**保留**库中其它技术列（避免 warm/cold 把 `change_5m` 刷成 null）。

## 配置（`config.py` / 环境变量）

| 变量 | 默认 | 含义 |
|------|------|------|
| `INDICATORS_ENABLED` | `1` | 总开关（`--full` 始终可跑） |
| `INDICATORS_HOT_INTERVAL_SEC` | `300` | hot 刷新 |
| `INDICATORS_WARM_INTERVAL_SEC` | `1800` | warm |
| `INDICATORS_COLD_INTERVAL_SEC` | `14400` | cold |
| `INDICATORS_LOOP_INTERVAL_SEC` | `300` | `--loop` 睡眠 |
| `INDICATORS_VOLATILITY_WINDOW_SEC` | `86400` | 波动率窗口 |
| `INDICATORS_WRITE_HISTORY_HOT` | `1` | hot 写入 `indicator_history` |
| `INDICATORS_WEIGHT_*` | 0.50/0.25/0.25 | 合成权重占位（Step 1 未用） |

## 如何运行

在仓库根目录 `/workspace/dexscreener-crawler`：

```bash
# 全量重算一次
python -m indicators.run --full
# 或
python scripts/run_indicators.py --full

# 按分档 due 单次
python -m indicators.run --once

# 常驻循环（默认每 300s）
python -m indicators.run --loop --interval 300
```

与主爬虫并行即可；**不要**杀 `main.py --loop` stabilize 进程。

## 验证结果（2026-10-10 03:12 HKT / 数据时间约 2026-10-09T19:12Z）

- `--full`：`seen=645 ok=645 err=0`，`token_indicators` 行数 **645**；hot 归档 `indicator_history` **583** 行。
- 包可导入：`indicators` `0.1.0-step1`。
- 人工重算 vs 库内值（绝对误差均为 **0**）：

### 1) `solana/bttoken000022222222222222222222222222222222`（回测密历史，92 点，warm）

| 字段 | stored | manual |
|------|--------|--------|
| change_5m | -3.951862512 | -3.951862512 |
| change_15m | 1.485726529 | 1.485726529 |
| change_1h | -14.35928187 | -14.35928187 |
| change_4h / 24h | null（历史跨度不足） | null |
| volatility_24h | 1.976484907 | 1.976484907 |
| volume_strength | 1.082906858 | 1.082906858 |

### 2) `solana/2j5sas7xy776qcbpypqbzjyqsatkifgrwjferthnw2zm`（实盘，6 点，hot）

| 字段 | stored | manual |
|------|--------|--------|
| change_5m | -5.472197705 | -5.472197705 |
| change_15m | 3.879728419 | 3.879728419 |
| change_1h | 7.844124459 | 7.844124459 |
| volatility_24h | 4.667997629 | 4.667997629 |
| volume_strength | 1.190107729 | 1.190107729 |

### 3) `solana/21kdsfe9bxc9a3yyjzpfurvhg7rinebffershmg3ydeg`（实盘，价格几乎不变）

| 字段 | stored | manual |
|------|--------|--------|
| change_5m/15m/1h | 0 | 0 |
| volatility_24h | 0 | 0 |
| volume_strength | 0 | 0 |

说明：当前实盘 `pair_history` 跨度多在约 1h 内，故多数 token 的 `change_4h` / `change_24h` 为 null，属预期；随爬虫积累会逐步填上。

## 后续（非本 Step）

- `funding.py`：JEV / funding bias → `jev_*`
- `sentiment.py`：社交情绪 → `sentiment_*`
- `composite.py`：按 `INDICATORS_WEIGHT_*` 合成 → `composite_score`
