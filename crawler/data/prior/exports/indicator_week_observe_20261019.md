# 指标加工层·周观察（只读）· 2026-10-19

> 补正口径：§10 指标→fwd≈1h 分层（2026-10-12～19 HKT）  
> 机器可读：`indicator_week_observe_20261019.json`  
> **钉死：** 只观察不调权；「建议落地」≠自动开权；裁决=权重保持全 0，待 R4。

## 样本

- 分层快照 19338 → 有效配对 13328 / token 3066
- 全体上涨率 43.1%
- INDICATORS_WEIGHT_* 全 0
- 高档 mean 常被左尾拖累，禁止本周接闸

## 分层摘要

| 类别 | 指标 | 要点 | 状态 |
|---|---|---|---|
| 可上调观察 | change_4h / change_24h / change_1h / jev_strength | lift≈1.36 / 1.37 / 1.19 / 1.15 | 建议落地（仍须 R4，维持 0） |
| 旁路可上调 | rsi_14 | lift≈1.45 | 建议落地·旁路（不进权重） |
| 降权/维持0 | change_5m/15m、volume_strength、volatility_24h、macd_hist | 混乱 / 高档警示 / 高波差 / 混乱 | 建议落地（维持 0） |
| 观察中 | sentiment_heat_score、spike、spike_ratio | 离散弱 lift / 突热 | 观察中 |

## 脚注

self_survival 9.50d n=65094 存活 99.4/98.2/57.7/8.6%；baseline=UNSTABLE_OR_SHORT。

## 裁决

权重保持全 0，待 R4；禁止本周接闸。
