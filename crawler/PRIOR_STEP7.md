# PRIOR_STEP7 — 指标加工层：价格行为先验（拉升）

> 时间：2026-10-11（Asia/Hong_Kong）
> 定位：**指标加工层旁路**；补齐「见顶 + 归零 + **拉升**」中的拉升特征先验
> 只碰：`scripts/prior_price_behavior.py`、`data/prior/exports/price_behavior*`、
> `baseline_params.json["price_behavior"]`（merge，不删其它键）、本文件
> **零策略/权重影响**：不改 INDICATORS_WEIGHT_*、trading、alerts、业务四库；`RISK_USE_PRIOR` 仍默认关

## 做什么

从只读 `pair_history`（+ 可选 `decision_log` / `decision_prior_tags`）产出机器可读的价格行为先验：

| 块 | 内容 |
|---|---|
| A | 15m / 1h / 4h **观测收益**分布（median / P10–P90 / n） |
| B | **拉升**特征：低→高倍数、历时（分钟） |
| C | **入场代理回撤** mdd_4h（自首价起 4h 最大回撤） |
| D | 可选 **JEV 交叉**（信号后收益/回撤 vs 全量；按 age_bucket） |

## Schema 实勘（勿假设 close）

| 项 | 实际 |
|---|---|
| 价格列 | `price_usd`（**无** `close` / OHLCV 列） |
| 时间 | `recorded_at` ISO TEXT（`…Z`） |
| 键 | `chain_id` + `token_address`；多 pair 时取 **点数最多** 的 `pair_address`（防跨池混价假拉升） |
| 采样 | hot 约 5–7min/点；15m 收益可算 |

## 口径（反假象）

1. **单位**：`(chain_id, token_address)` → densest `pair_address`
2. **t0 / p0**：该 pair 上第一条 `price_usd > 0`
3. **收益**：`ret = p(t0+h) / p0 − 1`；仅 `span ≥ h`；优先 `t0+h ± TOL(20min)` 最近点，否则 last 价
4. **≠** 交易所完美 OHLCV；是 crawler 观测收益
5. **拉升样本**：窗内 `max/min ≥ 1.05`；`multiple = max/min`；`duration_min = t(高点)−t(低点)`（高点在低点之后）
6. **mdd_4h**：`max(0, (p0 − min_[t0,t0+4h]) / p0)`（正数=跌幅深度）；主表要求 span≥4h
7. **SHORT**：crawler 总窗口 `<48h` 时全局标记；n 以计入为准，不夸大

## 用法

```bash
cd /workspace/dexscreener-crawler
python3 scripts/prior_price_behavior.py
```

### 产出

| 文件 | 说明 |
|---|---|
| `data/prior/exports/price_behavior.json` | 全量机器可读 |
| `data/prior/exports/price_behavior_YYYYMMDD.md` | 中文报告 |
| `baseline_params.json["price_behavior"]` | 摘要键 merge（保留 redpump/gmgn/self_db 等） |

**不**写入 `baseline_history` 行（与 step6 存活快照分离；价格行为独立 JSON）。

## Smoke（2026-10-11）

| 指标 | 值 |
|---|---|
| crawler 窗 | ≈44.2h → **SHORT** |
| n tokens | ≈15.6k |
| ret 15m / 1h / 4h median | ≈0% / ≈0% / ≈−0.3%（P10 深负，右尾薄） |
| 拉升 4h n / multiple P50 / duration P50 | ≈3.2k / ≈1.44× / ≈27min |
| mdd_4h P50 / P90 | ≈1.2% / ≈92%（n≈13.9k） |
| JEV | 40 信号均可算；`1-24h` n=24 → 表保留、勿强读 |

## 旁路原则

- **周报 §10 已挂载观察**：`review/evolution.py` 读本 json，建议表+小节只观察不调权（见 `EVOLUTION_SECTION10.md`）

- 主库 **mode=ro**；只写 `data/prior/exports/`
- 不改权重、不交易、默认不进决策
- 与 GMGN early-peak / 自库存活 / 见顶标签 **并排**，勿单独当可交易 alpha

## 不做

- 不碰业务四库 / INDICATORS_WEIGHT_* / trading / alerts
- 不建与「自库存活·满窗重跑」冲突的 cron
- 不把 SHORT 窗下的分布写成长期市场真值
