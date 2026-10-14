# 因子进化周报 2026-W41

过门槛仅 status=accepted（待激活）。本循环不自动启用策略、不自动调参。

## jev_liq50k_cov10_s20_v1 — fail

- 假设: JEV 强度≥0.2 且流动性≥$50000 且覆盖≥10 的新币，1h 上涨率显著>50%
- 来源: rule_grid
- 回测: {"n_raw": 3, "n_filt": 2, "n_1h": 2, "n_4h": 2, "unique_token": 1, "up_1h": 1.0, "up_4h": 1.0, "med_1h": 0.04838767972079512, "med_4h": 0.44156388907554134, "ic_mean": null, "ir": null, "win_rate": null, "pnl": null, "note": "jev_validity.run_analysis"}
- 判定: 观察:n=3, unique=1, up_1h=1.0; 评估:样本不足（n<15 或 unique<10）; 判定:样本不足不入库
- 状态: rejected

## jev_liq100k_cov10_s20_v1 — fail

- 假设: JEV 强度≥0.2 且流动性≥$100000 且覆盖≥10 的新币，1h 上涨率显著>50%
- 来源: rule_grid
- 回测: {"n_raw": 3, "n_filt": 1, "n_1h": 1, "n_4h": 1, "unique_token": 1, "up_1h": 1.0, "up_4h": 1.0, "med_1h": 0.047900262467191555, "med_4h": 0.6325459317585304, "ic_mean": null, "ir": null, "win_rate": null, "pnl": null, "note": "jev_validity.run_analysis"}
- 判定: 观察:n=3, unique=1, up_1h=1.0; 评估:样本不足（n<15 或 unique<10）; 判定:样本不足不入库
- 状态: rejected

## jev_liq50k_cov15_s25_v1 — fail

- 假设: JEV 强度≥0.25 且流动性≥$50000 且覆盖≥15 的新币，1h 上涨率显著>50%
- 来源: rule_grid
- 回测: {"n_raw": 0, "n_filt": 0, "n_1h": 0, "n_4h": 0, "unique_token": 0, "up_1h": null, "up_4h": null, "med_1h": null, "med_4h": null, "ic_mean": null, "ir": null, "win_rate": null, "pnl": null, "note": "jev_validity.run_analysis"}
- 判定: 观察:n=0, unique=0, up_1h=None; 评估:样本不足（n<15 或 unique<10）; 判定:样本不足不入库
- 状态: rejected

## jev_liq100k_cov15_s15_v1 — pass

- 假设: JEV 强度≥0.15 且流动性≥$100000 且覆盖≥15 的新币，1h 上涨率显著>50%
- 来源: rule_grid
- 回测: {"n_raw": 106, "n_filt": 23, "n_1h": 23, "n_4h": 23, "unique_token": 13, "up_1h": 0.5652173913043478, "up_4h": 0.5217391304347826, "med_1h": 0.0017528483786151022, "med_4h": 0.0023710729104920655, "ic_mean": null, "ir": null, "win_rate": null, "pnl": null, "note": "jev_validity.run_analysis"}
- 判定: 观察:n=106, unique=13, up_1h=0.5652173913043478; 评估:过滤后 1h 上涨率 56.5%≥55%（首次达标；连续两周达标才激活）; 判定:候选有效，待连续两周确认
- 状态: accepted
