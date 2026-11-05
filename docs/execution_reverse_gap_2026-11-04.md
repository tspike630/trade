# 反做缺口登记（−90% 条款·反做侧）— 修订 2026-11-05 12:24 HKT

口径：杨 2026-11-03「亏到负90」= 纸面成本权益收益率 ≤ −90%（起本 $10,000 → eq ≤ $1,000）
→ 废除 + 反做开新一策一盘。**本文已按决策源码头与群内互核修订；旧「真缺 3 / 5 本」作废。**

## 结论（现行）

**反做侧已用尽，执行不开任何新反做盘、不写绿、不交建库。**

| 账面 | 对应反做 | 处置 |
|---|---|---|
| and_vol_squeeze_break | `vol_squeeze_fade`（源码首行 `Invert and_vol_squeeze_break`，peer 在跑，≈−12.9%） | 已有，不开第二本同策 |
| cs_fade_1w | `cs_mom_1w_r90`（11-03 立，peer 在跑） | 已有 |
| rsi_bounce_fade | `rsi_bounce_r90`（11-03 立，peer 在跑） | 已有 |
| rs_fade_10d | 反做方向 = TOP20% ret>0 ≈ 已死的 `rel_strength_10d` / OUTLIER `rel_strength_m` | **不写绿**（复刻死盘，违一策一盘） |
| rsi_boll_fade | 反做方向 = 母盘 `rsi_boll_and`（已 ≤−99% 且停） | **不写绿** |

废除侧：ret≤−90% 共 12 本（**MTM 含未平**口径），launcher 全部 ABOLISHED、无在跑 peer。

## 下一步（等杨点头，不是反做）

尾部三件套（入场前池深下限 / 单笔名义上限 / rug 权益硬帽）写成**新 strategy_id 新盘**，不改现有 69 本。无决策源的盘不建库、不占竞速分母。

## 不动项

零改闸；未 SIGTERM 任何 peer；未开新盘；live/#71 关；真盘不注资。
