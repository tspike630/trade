# 指标加工层·周观察（只读）· 2026-11-02

> 补正口径：§10 指标→fwd≈1h 分层（2026-10-31～11-02 HKT（≈1.7d 文本窗；非满7d；10-31换库后））  
> 机器可读：`indicator_week_observe_20261102.json`  
> DB：`/workspace/dexscreener-crawler/dexscreener.db`（mode=ro）  
> **钉死：** 只观察不调权；「建议落地」≠自动开权；裁决=权重保持全 0，待 R4。

## 窗口与样本

- 小时抽样 31788 → 有效配对 30279 / token 10554
- 全体上涨率 28.1%（短窗偏空；上周≈42.7%）
- 可用文本窗（Asia/Hong_Kong）：2026-10-31T14:52:00+08:00 → 2026-11-02T08:11:00+08:00（≈1.7d，**非满 7d**）
- 接合：`indicator_history.primary_pair_address` + `chain_id` → `pair_history`；t0 容差±15m，t+1h 容差±20m；仅 `typeof(recorded_at)='text'`
- change_7d / sentiment_heat_score / spike* 文本史全 NULL（不假填）；jev_strength 文本史全 0
- INDICATORS_WEIGHT_* 全 0
- 高档 mean 常被左尾拖累，禁止本周接闸

## 分层摘要

| 类别 | 指标 | 要点 | 状态 |
|---|---|---|---|
| 旁路可上调 | rsi_14 | lift≈1.57；差≈+24.5pp；近似单调；missing≈70% | 建议落地·旁路（不进权重） |
| 降权/维持0 | volume_strength | lift≈0.71；差≈−12.9pp；高档警示；n≈2.8万 | 建议落地（维持 0） |
| 可上调观察 | change_4h / change_24h | lift≈1.40 / 1.30；缺测高；短窗 | 观察中 |
| 弱正区分 | change_1h | lift≈1.91 但 Q 结构不稳；missing≈54% | 观察中 |
| 降权/维持0 | change_5m/15m、macd_hist | Q2 塌陷 / 弱 lift | 观察中 |
| 风险旁路 | volatility_24h | 非单调（Q2 最好） | 观察中 |
| 旁路非开仓臂 | jev_strength | 文本史全 0（JEV 403） | 观察中 |
| 覆盖不可算 | change_7d、sentiment_heat、spike* | 文本史 NULL | 观察中 |
| 新候选旁路 | （无） | 本周无超出清单可分层新字段 | 观察中 |

## 建议条目

| 指标 | 方向 | 状态 | 一句话证据 |
|---|---|---|---|
| rsi_14 | 可上调（旁路） | 建议落地 | lift_up≈1.57；Q4–Q1≈+24.5pp；n=8992；单调；不上权 |
| volume_strength | 可降权 | 建议落地 | lift_up≈0.71；差≈−12.9pp；n=28070；高档警示 |
| change_4h | 可上调观察 | 观察中 | lift≈1.40；差≈+17.8pp；n=8448；missing≈72%；短窗 |
| change_24h | 可上调观察 | 观察中 | lift≈1.30；差≈+14.0pp；n=3460；missing≈89%；短窗 |
| change_1h | 弱正区分/观察 | 观察中 | lift≈1.91 但分位不稳；n=14035；短窗 |
| change_5m/15m | 可降权 | 观察中 | Q2 塌陷；非单调；短窗勿线性上调 |
| macd_hist | 可降权 | 观察中 | lift≈1.05；Q2 最差；missing≈78% |
| volatility_24h | 风险旁路 | 观察中 | 非单调；不宜做多信号 |
| jev_strength | 旁路非开仓臂 | 观察中 | 文本史全 0；JEV 403；不作 BULLISH 门槛 |
| change_7d | 观察中 | 观察中 | 文本史全 NULL（规格短史，禁假填） |
| sentiment_heat_score | 观察中 | 观察中 | history 文本 NULL；当前快照非空≈620 |
| spike/spike_ratio | 观察中 | 观察中 | history 文本 NULL；当前快照非空≈620 |

## 硬约束提醒

**INDICATORS_WEIGHT_*=0 直至人工/周检裁决。** 本周不对任何权重改值；「建议落地」≠自动开权；未改 `.env` / 未开 live/#71 / 未写 `*.db`。

## 脚注

self_survival ≈23.50d n=114377 存活 1h/4h/24h/7d=99.4%/98.0%/59.7%/20.7%；权仍0；口径=pair_history观测跨度（含blob旧戳），与本周文本窗fwd分层不同只脚注；见 data/prior/exports/self_survival_20261102.md

## 裁决

权重保持全 0，待 R4；禁止本周接闸。「建议落地」≠允许自动开权。短窗（≈1.7d）满 7d 前不冠、不接闸。
