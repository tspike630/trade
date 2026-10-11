# 价格行为先验（拉升） 20261011

> **只读** `dexscreener.db.pair_history`；可选 `decision_log` + `prior.db.decision_prior_tags`。
> **口径**：每 token 取点数最多的 pair；t0=首条 price_usd>0；ret=p_h/p0−1；拉升=窗内 max/min≥1.05；mdd_4h=max(0,(p0−min)/p0)。
> crawler 观测收益 ≠ 交易所完美 OHLCV；短窗见 SHORT 标记。

## 0. 样本与窗口
- DB: `dexscreener.db`（mode=ro）
- pair_history 行数: 547,678
- recorded_at 范围: `2026-10-09T12:00:00Z` → `2026-10-11T08:11:08Z`
- crawler 总窗口: **44.2h** → **SHORT**
- 分析 token 数: **15,575**（跳过 ticks<2: 69）
- 价格字段: `price_usd`（无 close 列）
- 容差 TOL: 1200s；horizon: 15m / 1h / 4h

## A. 收益分布（crawler-observation returns）
| horizon | n | median | P10 | P25 | P75 | P90 | nearest/last |
|---|---:|---:|---:|---:|---:|---:|---|
| 15m | 15493 | 0.0% | -81.6% | -18.8% | 0.0% | 9.6% | 15493/0 |
| 1h | 14985 | -0.0% | -89.2% | -34.3% | 0.0% | 6.1% | 14601/384 |
| 4h | 13912 | -0.3% | -92.4% | -44.8% | 0.0% | 5.7% | 12271/1641 |

说明：仅 span≥horizon 的 token 计入；优先 t0+h±TOL 最近点，否则 last 价。

## B. 拉升特征（向上样本 max/min ≥ 1.05）
| 窗口 | n | multiple P25/P50/P75/P90 | duration_min P25/P50/P75 |
|---|---:|---|---|
| 1h | 3055 | 1.163/1.470/2.296/4.600 | 6.0/12.7/31.9 |
| 4h | 3249 | 1.158/1.442/2.407/5.281 | 6.5/26.6/76.4 |
| full | 4079 | 1.149/1.422/2.326/5.187 | 10.5/56.7/328.2 |

duration = 窗内（低点之后的高点时间 − 低点时间），分钟。

## C. 回撤（入场代理 MDD）
- 定义: `mdd_4h = max(0, (p0 - min_price_in_[t0,t0+4h]) / p0); entry proxy from first observed price; positive = depth of loss`
| 指标 | n | P50 | P90 | P25 | P75 |
|---|---:|---:|---:|---:|---:|
| mdd_4h (span≥4h) | 13912 | 1.2% | 92.0% | 0.0% | 45.5% |
| mdd_可用窗 (span<4h, SHORT) | 1663 | 0.0% | 91.9% | 0.0% | 26.8% |

## D. JEV 交叉（可选）
- age_bucket 1-24h ret_1h n=24 — still small; tables shown, avoid strong claims vs all-token baseline (selection bias)
- signals: 40；有序列: 40

### 全部 JEV 信号
| 指标 | n | median | P10 | P90 |
|---|---:|---:|---:|---:|
| ret_1h | 40 | 0.0% | -64.0% | 4.8% |
| ret_4h | 40 | -0.4% | -98.9% | 25.1% |
| mdd_4h | 40 | 1.6% | 0.0% | 99.1% |

### 按 age_bucket
| bucket | ret_1h n/med | ret_4h n/med | mdd_4h n/P50 |
|---|---|---|---|
| 1-24h | 24/0.0% | 24/0.0% | 24/0.7% |
| 1-7d | 2/-0.5% | 2/3.9% | 2/17.4% |
| <1h | 10/-12.5% | 10/-93.7% | 10/94.9% |
| >7d | 4/0.2% | 4/-0.4% | 4/24.8% |

## 反假象
- ⚠ crawler 总窗口仅约 44.2h → 标记 SHORT；4h 收益/回撤可用但勿外推为长期市场先验
- ⚠ 窗口 <7d：无 7d 收益先验（本 step 也不计算）
- 勿把 crawler 观测收益当成可交易 alpha；抽样偏 hot/warm 池。
- 多 pair token 只取最密 pair，避免跨池价差伪拉升。
- n 以实际计入为准，不夸大。

## 不做
- 不改 INDICATORS_WEIGHT_* / trading / alerts / 业务四库
- 默认不进风控（RISK_USE_PRIOR=0）

