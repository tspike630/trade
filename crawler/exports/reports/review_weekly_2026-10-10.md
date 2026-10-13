# 复盘周报 · R1 信号层绩效 + R2 归因

**生成时间:** 2026-10-11 00:25 HKT
**期间:** `2026-10-03T16:25:07Z` ≤ ts < `2026-10-10T16:25:08Z` (UTC)
**范围:** 信号 → 校验/执行留痕 →（可选）成交；只读四库，零侵入

## 1. 期间与数据覆盖

| 项 | 值 |
|---|---|
| decision 链路行数 | **40** |
| 有 exec_id | **40** (100.0%) |
| 有 position | 0 |
| 有 fwd_1h / fwd_4h | 40 / 37 |
| 策略集合 | `jev_strength_v1` |
| exec 重复 decision 数 | 0 |

## 2. 全策略信号绩效（1h / 4h）

前向收益口径：`pair_history` 就近价格，容差默认 ±1800s（对齐 `jev_validity`）。

| 集合 | n | unique_token | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h | 校验通过 | executed | dry_run |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 全策略 | 40 | 26 | 40 | 37.5% | +0.00% | 37 | 29.7% | -0.66% | 100.0% | 0.0% | 100.0% |

## 3. 分策略对比

| 集合 | n | unique_token | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h | 校验通过 | executed | dry_run |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| jev_strength_v1 | 40 | 26 | 40 | 37.5% | +0.00% | 37 | 29.7% | -0.66% | 100.0% | 0.0% | 100.0% |

## 4. JEV 过滤切片（对齐周检过滤后）

切片：`jev_strength_v1` ∩ buy ∩ strength≥0.15 ∩ liq≥50,000 ∩ coverage≥10 ∩ price≥1e-06。
数据来源：liq=history@t0±tol（缺则 raw_json 快照）；coverage=`jev_signals.coverage_pct`（缺则 `token_indicators.jev_confidence*100`）。

| 集合 | n | unique_token | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h | 校验通过 | executed | dry_run |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| JEV≥0.15 | 14 | 9 | 14 | 71.4% | +0.96% | 14 | 57.1% | +5.16% | 100.0% | 0.0% | 100.0% |

对账提示：周检过滤后 1h/4h 上涨率见 `DECISION_ENGINE_JEV_VALIDITY_FILTERED.md`（约 71.4% / 57.1%，n=14）；本切片 up_1h=71.4%，up_4h=57.1%，n=14。

## 5. 执行层状态

| 指标 | 值 |
|---|---|
| 有执行留痕 | 40 / 40 |
| 校验通过率 | 100.0% |
| executed 率 | 0.0% |
| dry_run 占比 | 100.0% |
| exec_dup_count | 0 |

### executor 分布

| executor | n |
|---|---:|
| `step_a_noop` | 40 |

_本期无校验失败分桶（或 validation_ok 全通过）。_

## 6. 市场基准（同期 hot）

`indicator_history` 中 `tier='hot'` 的 `change_1h` / `change_4h`（已为百分比单位）。截面波动大，优先看中位数与 winsor(|x|≤500)。

| 指标 | 原始 | winsor(|x|≤500) |
|---|---:|---:|
| n / n_1h / n_4h | 124892 / 86857 / 48637 | — / 86408 / 48119 |
| median change_1h | +0.00% | +0.00% |
| median change_4h | +0.86% | +0.81% |
| avg change_1h | +6484.05% | -3.41% |
| avg change_4h | +2076.61% | +5.34% |

## 7. 缺口与下一步

- **成交层空：** paper `positions`/`fills` 仍为 0；`pnl_usd` / fill 字段全 None，属预期（开闸前）。
- **executed=0：** 现网 `executor=step_a_noop`，校验可通过但未实成交；R1 报告仍完整。
- **策略覆盖：** 当前库仅见 `jev_strength_v1`；B/C（`tech_momentum_v1` / `combined_jev_tech_v1`）待信号写入后分桶自动出现。
- **R2 半块：** 第 8 节信号层归因已落地（只读分桶）；样本不足，不作为调参依据，不触发自动动作。
- **回测对照：** 第 8 节旁挂最近 done run 摘要（只读）；失败则跳过并注明。
- **R3 寻优：** 第 9 节为脚手架占位；历史≥7d 且周检跨门槛后再正式寻优；不自动改参（R4）。
- **进化建议：** 第 10 节只观察/建议，不自动改参或开闸；落地走 R4。

## 8. 信号层归因

> **Caveat（钉死）：** 归因观察、样本不足（当前约 40 信号 / 回测 ~0.51d）、不作为调参依据；不触发任何自动动作

全样本 n=40。以下为观察性分桶（有 fwd 才计上涨率/中位），**不**改策略参数或闸门。
token_age 分桶源=`prior_tagger`（prior_tags=40/40）

### strength（jev_strength）

分桶：`<0.15 / 0.15–0.17 / 0.17–0.19 / 0.19+ / unknown`

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| <0.15 | 0 | 0 | — | — | 0 | — | — |
| 0.15-0.17 | 24 | 24 | 29.2% | +0.00% | 23 | 26.1% | -0.66% |
| 0.17-0.19 | 12 | 12 | 33.3% | -0.24% | 10 | 30.0% | -14.86% |
| 0.19+ | 4 | 4 | 100.0% | +2.75% | 4 | 50.0% | +12.31% |
| unknown | 0 | 0 | — | — | 0 | — | — |

### coverage（jev_coverage，0–100）

分桶：`<10 / 10–20 / 20+ / unknown`

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| <10 | 0 | 0 | — | — | 0 | — | — |
| 10-20 | 11 | 11 | 36.4% | +0.00% | 10 | 20.0% | -1.14% |
| 20+ | 29 | 29 | 37.9% | +0.00% | 27 | 33.3% | -0.43% |
| unknown | 0 | 0 | — | — | 0 | — | — |

### liq（liquidity_at_signal，USD）

分桶：`<50k / 50–500k / ≥500k / unknown`

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| <50k | 23 | 23 | 17.4% | -2.29% | 20 | 15.0% | -8.05% |
| 50-500k | 8 | 8 | 50.0% | -0.15% | 8 | 75.0% | +19.46% |
| ≥500k | 6 | 6 | 100.0% | +1.25% | 6 | 33.3% | -14.98% |
| unknown | 3 | 3 | 33.3% | +0.00% | 3 | 0.0% | +0.00% |

### token_age（pairs.pair_created_at）

分桶：`<1d / 1–7d / >7d / unknown`
说明：分桶源=prior_tagger（prior.db.decision_prior_tags.age_bucket 细→粗；本期 40/40）；与现算 token_age 交叉校验；pairs.pair_created_at via chain_id+pair_address (fallback token_address)

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| <1d | 34 | 34 | 35.3% | +0.00% | 31 | 29.0% | -0.66% |
| 1-7d | 2 | 2 | 0.0% | -3.68% | 2 | 50.0% | -8.28% |
| >7d | 4 | 4 | 75.0% | +0.14% | 4 | 25.0% | -11.26% |
| unknown | 0 | 0 | — | — | 0 | — | — |

### market_regime（同期 hot 基准）

分桶：`bear / flat / bull / unknown`
规则：period baseline median_change_1h_winsor (fallback median_change_1h), units=%: bear < -2; flat [-2, +2]; bull > +2; else unknown
本期赋值：`flat`（baseline med_1h=+0.00%）

| bucket | n | n_1h | up_1h | med_1h | n_4h | up_4h | med_4h |
|---|---:|---:|---:|---:|---:|---:|---:|
| bear | 0 | 0 | — | — | 0 | — | — |
| flat | 40 | 40 | 37.5% | +0.00% | 37 | 29.7% | -0.66% |
| bull | 0 | 0 | — | — | 0 | — | — |
| unknown | 0 | 0 | — | — | 0 | — | — |

### JEV×币龄×先验（细桶联合表）

> **Caveat：** 联合归因观察：n 仍小；监控假说「1–24h 段质量最高」（过滤后 JEV n=7 up_1h=85.7%；过滤增益最大；>7d n=4 且 4h 差），不作为调参依据；不触发任何自动动作 / 不调权

分桶源=`prior_tagger`
prior_tags 本期覆盖：`40/40`（表总行=40）
细桶：`<1h / 1-24h / 1-7d / >7d / unknown`
假说：监控假说「1–24h 段质量最高」（过滤后 JEV n=7 up_1h=85.7%；过滤增益最大；>7d n=4 且 4h 差）
说明：分桶源=prior_tagger（prior.db.decision_prior_tags；本期覆盖 40/40）；与现算 token_age 交叉校验，不一致记 note；细桶：<1h / 1-24h / 1-7d / >7d / unknown；JEV 切片与周检同口径（strength/liq/coverage/price）；先验旁挂 self_db 观测跨度存活（非链上寿命）
先验旁挂：报告=`data/prior/exports/self_survival_20261010.md` · 源=`baseline_params.json#self_db` · crawler窗≈27.75h · self_db n=9317

#### 过滤后 JEV 切片（周检同口径）（n=14）

| token_age | n | up_1h | med_1h | up_4h | 全网存活率(先验) | 备注 |
|---|---:|---:|---:|---:|---:|---|
| <1h | 2 | 50.0% | +0.81% | 50.0% | 92.7% | 全网观测跨度>1h（活过本段） |
| 1-24h | 7 | 85.7% | +2.04% | 71.4% | 75.3% | 全网观测跨度>4h（本段中位参考） |
| 1-7d | 1 | 0.0% | -0.48% | 100.0% | N/A | 观测跨度 max≈21.4h < 24h，24h 存活不可得（覆盖上限，≠链上真值） |
| >7d | 4 | 75.0% | +0.14% | 25.0% | N/A | crawler 窗口≈27.8h < 168h，168h 存活受覆盖上限约束（≠链上真值） |
| unknown | 0 | — | — | — | N/A | unknown 桶无对应存活窗 |

#### 全样本（n=40）

| token_age | n | up_1h | med_1h | up_4h | 全网存活率(先验) | 备注 |
|---|---:|---:|---:|---:|---:|---|
| <1h | 10 | 20.0% | -38.24% | 20.0% | 92.7% | 全网观测跨度>1h（活过本段） |
| 1-24h | 24 | 41.7% | +0.00% | 33.3% | 75.3% | 全网观测跨度>4h（本段中位参考） |
| 1-7d | 2 | 0.0% | -3.68% | 50.0% | N/A | 观测跨度 max≈21.4h < 24h，24h 存活不可得（覆盖上限，≠链上真值） |
| >7d | 4 | 75.0% | +0.14% | 25.0% | N/A | crawler 窗口≈27.8h < 168h，168h 存活受覆盖上限约束（≠链上真值） |
| unknown | 0 | — | — | — | N/A | unknown 桶无对应存活窗 |

口径提醒：全网存活率来自 self_db 观测跨度（`last_seen−first_seen > t`），不是链上真实寿命；窗不足时写 N/A + 原因，勿把 0% 当真死亡。

### 成交归因

待模拟盘开闸（当前 paper fills/positions 为空，`note=no_fills`）。

### 回测对照摘要（只读，可选）

最近 done run：`ca410ca9-d23c-4f29-be1b-fe23e4c98d39` · strategies=`combined_jev_tech_v1,jev_strength_v1,tech_momentum_v1` · closed=124 · equity 10000.00 → 10898.60
notes: requested --days=7 but available≈0.51d (2026-10-09T19:12:00Z → 2026-10-10T07:21:26Z); using available window

| strategy_id | trades | win_rate | total_pnl_usd |
|---|---:|---:|---:|
| `combined_jev_tech_v1` | 44 | 70.5% | 1423.30 |
| `jev_strength_v1` | 55 | 65.5% | -567.96 |
| `tech_momentum_v1` | 25 | 56.0% | 1840.47 |

## 9. R3 参数寻优 — 待历史≥7天且周检跨门槛

占位：历史不足（现 ~0.51d）或周检尚未跨门槛前不正式寻优；脚手架见 `review/optimizer.py`（`--optimize` 仅 dry_run / skip）。**不自动写配置**；正式改参需 R4 人工确认。

## 10. 进化建议

> **钉死：** 本节只建议，**不**自动改参 / 开闸 / 写配置；任何落地须走 **R4 人工确认**。

| 层 | 项 | 状态 | 证据 | 行动提示 |
|---|---|---|---|---|
| 采集 | token_age 覆盖率观察 | **观察中** | 归因 token_age 已接 pairs.pair_created_at；unknown=0/40（0.0%），相对此前全 unknown 已下降；仍观察中 | 继续观察 unknown 占比与分桶稳定性；不改桶边界，落地须走 R4 |
| 采集 | news_events 旁路积累 | **观察中** | news_events.db 可读，累计 163 条 | 旁路继续 RSS 入库即可；不接决策/JEV；链上验证器接入前只记 pending |
| 指标 | 合成权重仍全 0 | **观察中** | INDICATORS_WEIGHT_*: TECHNICAL=0 FUNDING=0 SENTIMENT=0 （sum=0，全 0 → composite no-op） | 保持关闸至明确接线验收；开权走 R4，不由复盘自动改 config |
| 指标 | liq 分层区分度（候选关注） | **观察中** | <50k: n=23 up_1h=17.4%；50-500k: n=8 up_1h=50.0%；≥500k: n=6 up_1h=100.0%；unknown: n=3 up_1h=33.3%；跨桶 up_1h 极差=82.6% | 仅作候选关注：样本不足时不据此调参；若跨桶极差持续且 unique_token 过闸，再交 R3/R4 评估过滤权重 |
| 指标 | pandas-ta 旁路 RSI/MACD/BOLL | **观察中** | token_indicators: rsi_14=3836/9604（39.9%）；macd=1583/9604（16.5%）；boll_mid=3134/9604（32.6%）；indicator_history: rsi_14=4657/124892（3.7%）；macd=4103/124892（3.3%）；boll_mid=4470/124892（3.6%） | 有分层区分度再建议上调权重；裁决前权重保持 0 |
| 指标 | 先验存活过滤（PRIOR_STEP1） | **观察中** | 毕业率 0.2% / 当天死 68.67%(CoinGecko) / SolRPDs 池移除>添加 60.1%；unique token 本库 33358（提示≈22195）；旧存活曲线/寿命分位口径不可用（RED-PUMP 24h TIMEOUT 窗口）；报告=data/prior/exports/survival_20261010.md | 裁决前不调权、不进策略；与现网 JEV 过滤对照后再谈落地 |
| 指标 | GMGN 见顶分布（STEP2 完整 n=956） | **观察中** | 见顶 P25/P50/P75=0.4m/1.0m/1.4m（见顶样本 510） / 涨幅 P50=1.35x 可作先验雏形；监控跨度 P25=1.1m / P50=1.6m / P75=2.4m / max=8.5m；death_reason（监控终止≠归零）price_stable 75.4%、volume_low 18.6%、candle_gap 3.6%、timeout 2.4%；n=956；kagglehub `moussaouallaf/gmgn-solana-token-charts` 官方 Tokens=956 即完整数据集（Kaggle v3 全量，无更大 pack），unique>1000 验收线本源无法满足；1h/4h/24h 存活因监控跨度短全 0，需另找长 OHLCV 数据源才换挂真实存活曲线；报告=data/prior/exports/survival_20261010_v2.md | 另找长 OHLCV 数据源后再换挂真实存活曲线；裁决前不调权 |
| 指标 | 自库存活曲线（self_survival） | **观察中** | n=9317；1h/4h/24h/7d=92.7%/75.3%/0.0%/0.0%；跨度 P50≈12.64h；max≈21.39h → 24h/7d=0% 是 crawler 短窗覆盖上限；只读 pair_history；假象扫描 PASS；报告=data/prior/exports/self_survival_20261010.md | 与 JEV 周检并排对照；裁决前不调权 |
| 决策 | JEV unique_token / 门槛距离 | **观察中** | 过滤后 n=14（距≥15：差 1）；unique_token=9（距≥10：差 1）；up_1h=71.4%（通过线≥55.0% / 弱线<50.0%） | 继续周检攒样本；连续 2 周过滤后 1h≥55% 且 unique_token≥10 （且 n≥15）后再启动 Step2；本层只观察 |
| 决策 | 策略退役规则提醒 | **观察中** | 规则（决策表）：连续 N=2 周过滤后 1h<50% 且 unique_token≥10 → 建议降级/停用，勿进 R3 无限调参；现样本未跨门槛，规则只提醒不裁决 | 周检写入决策表时核对此条；退役=止损，调参=修补；落地需 R4 |
| 执行 | executed=0 / paper 未开闸 | **观察中** | executed_rate=0.0%；dry_run=100.0%；with_exec=40/40；executor分布=[step_a_noop=40]；paper fills note=no_fills | 开闸前属预期；开闸与换 executor 属执行层/R4，复盘不自动开闸 |
| 执行 | 拒绝分布 | **观察中** | 本期无 validation_errors 分桶（或全 validation_ok） | 保持观察；若后续出现拒绝集中，再交执行层排查 |
| 复盘 | JEV×币龄×先验联合归因 | **观察中** | 分桶源=prior_tagger；JEV过滤 n=14 / 全样本 n=40；细桶=<1h / 1-24h / 1-7d / >7d / unknown；prior_tags=40/40；<1h: n=2 up_1h=50.0%；1-24h: n=7 up_1h=85.7%；1-7d: n=1 up_1h=0.0%；>7d: n=4 up_1h=75.0%；联合归因已出，不调权 | 联合归因已出，不调权；n 仍小；监控假说「1–24h 段质量最高」（过滤后 JEV n=7 up_1h=85.7%；过滤增益最大；>7d n=4 且 4h 差）；落地须走 R4 |
| 复盘 | prior_tagger 旁路标签 | **观察中** | prior_tagger 已回填 40/40（decision_prior_tags）；本期信号覆盖 40/40；§8 分桶源=prior_tagger，可自动吃标签 | 只读 prior.db；§8/联合表优先吃 age_bucket；不进决策/不调权；落地须走 R4 |
| 复盘 | 候选分层过滤 token_age∈[1h,24h] | **观察中** | 过滤后 JEV 1–24h 段 n=7 up_1h=85.7%，过滤增益最大；>7d n=4 且 4h 差；监控假说「1–24h 段质量最高」 | status=观察中；等 unique≥10 再攒几周才谈建议落地；裁决前不加币龄闸；落地须走 R4 |
| 复盘 | R3 skip 原因 | **观察中** | 未跑 --optimize；按闸门预检将 skip：历史不足：0.14d < 7d（现约 0.14d，需 ≥7d）；周检尚未跨门槛（连续 2 周过滤后达标前不正式寻优） | 历史≥7d 且周检跨门槛后再正式寻优；本节只建议，不自动改参/开闸；落地走 R4 |

共 16 条；状态词汇：`观察中` / `建议落地` / `裁决`。自动填充项默认为「观察中」。

---

*R1+R2+R3(scaffold)+§10进化建议 · review/ · 只读 · 报告仅写 exports/reports/*
