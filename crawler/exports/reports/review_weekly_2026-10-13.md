# 复盘周报 · R1 信号层绩效 + R2 归因

**生成时间:** 2026-10-14 00:50 HKT
**期间:** `2026-10-06T16:50:43Z` ≤ ts < `2026-10-13T16:50:44Z` (UTC)
**范围:** 信号 → 校验/执行留痕 →（可选）成交；只读四库，零侵入

## 1. 期间与数据覆盖

| 项 | 值 |
|---|---|
| decision 链路行数 | **134** |
| 有 exec_id | **134** (100.0%) |
| 有 position | 0 |
| 有 fwd_1h / fwd_4h | 91 / 92 |
| 策略集合 | `factor_jev_strength_v1`, `factor_macd_cross_v1`, `factor_rsi_bounce_v1`, `jev_strength_v1` |
| exec 重复 decision 数 | 0 |

## 2. 全策略信号绩效（1h / 4h）

前向收益口径：`pair_history` 就近价格，容差默认 ±1800s（对齐 `jev_validity`）。这是信号层 **mark-to-market** 前向收益，不是第 8 节已实现 PnL。

| 集合 | n | unique_token | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h | 校验通过 | executed | dry_run |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 全策略 | 134 | 75 | 91 | 38.5% | -0.05% | 92 | 40.2% | -0.35% | 100.0% | 0.0% | 100.0% |

## 3. 分策略对比

| 集合 | n | unique_token | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h | 校验通过 | executed | dry_run |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| factor_jev_strength_v1 | 16 | 16 | 14 | 35.7% | -1.28% | 15 | 40.0% | -0.47% | 100.0% | 0.0% | 100.0% |
| factor_macd_cross_v1 | 9 | 9 | 8 | 50.0% | +0.37% | 9 | 55.6% | +0.18% | 100.0% | 0.0% | 100.0% |
| factor_rsi_bounce_v1 | 3 | 3 | 2 | 0.0% | -6.15% | 3 | 33.3% | +0.00% | 100.0% | 0.0% | 100.0% |
| jev_strength_v1 | 106 | 66 | 67 | 38.8% | +0.00% | 65 | 38.5% | -0.35% | 100.0% | 0.0% | 100.0% |

## 4. JEV 过滤切片（对齐周检过滤后）

切片：`jev_strength_v1` ∩ buy ∩ strength≥0.15 ∩ liq≥50,000 ∩ coverage≥10 ∩ price≥1e-06。
数据来源：liq=history@t0±tol（缺则 raw_json 快照）；coverage=`jev_signals.coverage_pct`（缺则 `token_indicators.jev_confidence*100`）。

| 集合 | n | unique_token | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h | 校验通过 | executed | dry_run |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| JEV≥0.15 | 32 | 20 | 32 | 53.1% | +0.14% | 32 | 59.4% | +0.88% | 100.0% | 0.0% | 100.0% |

对账提示：与当次周检 `DECISION_ENGINE_JEV_VALIDITY_FILTERED.md` 对照，不引用冻结样本。本切片 up_1h=53.1%，up_4h=59.4%，n=32，unique_token=20。

## 5. 执行层状态

| 指标 | 值 |
|---|---|
| 有执行留痕 | 134 / 134 |
| 校验通过率 | 100.0% |
| executed 率 | 0.0% |
| dry_run 占比 | 100.0% |
| exec_dup_count | 0 |

### executor 分布

| executor | n |
|---|---:|
| `step_a_noop` | 134 |

_本期无校验失败分桶（或 validation_ok 全通过）。_

## 6. 市场基准（同期 hot）

`indicator_history` 中 `tier='hot'` 的 `change_1h` / `change_4h`（已为百分比单位）。截面波动大，优先看中位数与 winsor(|x|≤500)。

| 指标 | 原始 | winsor(|x|≤500) |
|---|---:|---:|
| n / n_1h / n_4h | 156233 / 113447 / 69383 | — / 112899 / 68696 |
| median change_1h | +0.00% | +0.00% |
| median change_4h | +0.62% | +0.57% |
| avg change_1h | +92967.31% | -2.99% |
| avg change_4h | +1521.29% | +4.59% |

## 7. 缺口与下一步

- **成交层：** 本期匹配 position=0，fills=0。已实现 PnL 见第 8 节；无匹配时成交字段为 None。
- **executed：** 本期 executed=0（分布见第 5 节）。
- **策略覆盖：** 本期见 `factor_jev_strength_v1`, `factor_macd_cross_v1`, `factor_rsi_bounce_v1`, `jev_strength_v1`。
- **R2 半块：** 第 8 节信号层归因已落地（只读分桶）；样本不足，不作为调参依据，不触发自动动作。
- **回测对照：** 第 8 节旁挂最近 done run 摘要（只读）；失败则跳过并注明。
- **R3 寻优：** 第 9 节为脚手架占位；历史≥7d 且周检跨门槛后再正式寻优；不自动改参（R4）。
- **进化建议：** 第 10 节只观察/建议，不自动改参或开闸；落地走 R4。

## 8. 信号层归因

> **Caveat（钉死）：** 归因观察、样本不足时不作为调参依据；不触发任何自动动作

全样本 n=134。以下为观察性分桶（有 fwd 才计上涨率/中位），**不**改策略参数或闸门。

### strength（jev_strength）

分桶：`<0.15 / 0.15–0.17 / 0.17–0.19 / 0.19+ / unknown`

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| <0.15 | 0 | 0 | — | — | 0 | — | — |
| 0.15-0.17 | 78 | 51 | 37.3% | -0.05% | 52 | 42.3% | +0.00% |
| 0.17-0.19 | 32 | 21 | 33.3% | -0.48% | 19 | 31.6% | -0.90% |
| 0.19+ | 12 | 9 | 55.6% | +0.10% | 9 | 33.3% | -4.19% |
| unknown | 12 | 10 | 40.0% | +0.00% | 12 | 50.0% | +0.09% |

### coverage（jev_coverage，0–100）

分桶：`<10 / 10–20 / 20+ / unknown`

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| <10 | 0 | 0 | — | — | 0 | — | — |
| 10-20 | 60 | 39 | 46.2% | +0.00% | 42 | 50.0% | +0.09% |
| 20+ | 74 | 52 | 32.7% | -0.57% | 50 | 32.0% | -0.63% |
| unknown | 0 | 0 | — | — | 0 | — | — |

### liq（liquidity_at_signal，USD）

分桶：`<50k / 50–500k / ≥500k / unknown`

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| <50k | 42 | 41 | 22.0% | -0.67% | 39 | 17.9% | -1.86% |
| 50-500k | 34 | 34 | 41.2% | -0.33% | 34 | 58.8% | +1.39% |
| ≥500k | 13 | 13 | 84.6% | +0.70% | 13 | 53.8% | +0.24% |
| unknown | 45 | 3 | 33.3% | +0.00% | 6 | 50.0% | +9.12% |

### token_age（raw_json 创建时间）

分桶：`<1d / 1–7d / >7d / unknown`
说明：from signal raw_json creation fields; missing → unknown

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| <1d | 0 | 0 | — | — | 0 | — | — |
| 1-7d | 0 | 0 | — | — | 0 | — | — |
| >7d | 0 | 0 | — | — | 0 | — | — |
| unknown | 134 | 91 | 38.5% | -0.05% | 92 | 40.2% | -0.35% |

### market_regime（同期 hot 基准）

分桶：`bear / flat / bull / unknown`
规则：period baseline median_change_1h_winsor (fallback median_change_1h), units=%: bear < -2; flat [-2, +2]; bull > +2; else unknown
本期赋值：`flat`（baseline med_1h=+0.00%）

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| bear | 0 | 0 | — | — | 0 | — | — |
| flat | 134 | 91 | 38.5% | -0.05% | 92 | 40.2% | -0.35% |
| bull | 0 | 0 | — | — | 0 | — | — |
| unknown | 0 | 0 | — | — | 0 | — | — |

### 成交归因

本期无匹配成交（`note=no_fills`）。已实现 PnL 只在 closed `positions.pnl_usd`；信号层 mark-to-market 见第 2 节前向收益，两者不要混读。

### 回测对照摘要（只读，可选）

最近 done run：`b94ac99d-959c-42a2-83b5-a72c916a9c79` · strategies=`factor_jev_strength_v1` · closed=5 · equity 10000.00 → 10000.00
notes: ok
PnL 口径：下表为回测已实现（closed `positions.pnl_usd`），不是第 2 节信号 mark-to-market 前向收益。

| strategy_id | trades | win_rate | realized_pnl_usd |
|---|---:|---:|---:|
| `factor_jev_strength_v1` | 5 | 0.0% | 0.00 |

## 9. R3 参数寻优 — 待历史≥7天且周检跨门槛

占位：历史未达 ≥7d 或周检尚未跨门槛前不正式寻优；脚手架见 `review/optimizer.py`（`--optimize` 仅 dry_run / skip）。**不自动写配置**；正式改参需 R4 人工确认。

## 10. 进化建议

> **钉死：** 本节只建议，**不**自动改参 / 开闸 / 写配置；任何落地须走 **R4 人工确认**。

| 层 | 项 | 状态 | 证据 | 行动提示 |
|---|---|---|---|---|
| 采集 | token_age 字段缺口 | **观察中** | 归因 token_age 桶 unknown=134/134（创建时间字段缺口，全样本 unknown） | 在 crawl/raw_json 补齐 pairCreatedAt（或等价创建时间）；复盘侧仅观察 unknown 占比下降，不改桶边界 |
| 采集 | news_events 旁路积累 | **观察中** | news_events.db 可读，累计 280 条 | 旁路继续 RSS 入库即可；不接决策/JEV；链上验证器接入前只记 pending |
| 指标 | 合成权重仍全 0 | **观察中** | INDICATORS_WEIGHT_*: TECHNICAL=0 FUNDING=0 SENTIMENT=0 （sum=0，全 0 → composite no-op） | 保持关闸至明确接线验收；开权走 R4，不由复盘自动改 config |
| 指标 | liq 分层区分度（候选关注） | **观察中** | <50k: n=42 up_1h=22.0%；50-500k: n=34 up_1h=41.2%；≥500k: n=13 up_1h=84.6%；unknown: n=45 up_1h=33.3%；跨桶 up_1h 极差=62.7% | 仅作候选关注：样本不足时不据此调参；若跨桶极差持续且 unique_token 过闸，再交 R3/R4 评估过滤权重 |
| 决策 | JEV unique_token / 门槛距离 | **观察中** | 过滤后 n=32（距≥15：已达）；unique_token=20（距≥10：已达）；up_1h=53.1%（通过线≥55.0% / 弱线<50.0%） | 继续周检攒样本；连续 2 周过滤后 1h≥55% 且 unique_token≥10 （且 n≥15）后再启动 Step2；本层只观察 |
| 决策 | 策略退役规则提醒 | **观察中** | 规则（决策表）：连续 2 周过滤后 1h<50% 且 unique_token≥10 → 建议降级/停用，勿进 R3 无限调参。本期：过滤后 n=32（距≥15：已达）；unique_token=20（距≥10：已达）；up_1h=53.1%（通过线≥55.0% / 弱线<50.0%）。规则只提醒不裁决 | 周检写入决策表时核对此条；退役=止损，调参=修补；落地需 R4 |
| 执行 | paper / executed 状态 | **观察中** | executed_rate=0.0%；dry_run=100.0%；with_exec=134/134；executor分布=[step_a_noop=134]；paper fills note=no_fills | 开闸与换 executor 属执行层/R4，复盘不自动开闸 |
| 执行 | 拒绝分布 | **观察中** | 本期无 validation_errors 分桶（或全 validation_ok） | 保持观察；若后续出现拒绝集中，再交执行层排查 |
| 复盘 | R3 skip 原因 | **观察中** | 未跑 --optimize；按闸门预检将 skip：历史不足：2.68d < 7d（现约 2.68d，需 ≥7d）；周检尚未跨门槛（连续 2 周过滤后达标前不正式寻优） | 历史≥7d 且周检跨门槛后再正式寻优；本节只建议，不自动改参/开闸；落地走 R4 |

共 9 条；状态词汇：`观察中` / `建议落地` / `裁决`。自动填充项默认为「观察中」。

## 12. 因子进化（候选，不自动激活）

本周候选因子、回测与门槛见 `python -m review.factor_loop` 产物：`review/factor_candidates.db` 与 `exports/reports/factor_loop_*.md`。过门槛只记 status=accepted（待人工激活），不自动启用策略、不自动调参。

---

*R1+R2+R3(scaffold)+§10进化建议 · review/ · 只读 · 报告仅写 exports/reports/*
