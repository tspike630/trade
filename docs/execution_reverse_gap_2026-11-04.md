# 反做缺口登记（−90% 条款·反做侧）— 2026-11-04 18:30 HKT

口径引用：杨 2026-11-03「亏到负90」= 纸面成本权益收益率 ≤ −90%（起本 $10,000 → eq ≤ $1,000）
→ **废除 + 反做**开新一策一盘。本文只登记「反做侧未落地」的缺口，零改闸、未开任何新盘。

## 一、ret ≤ −90% 全量 12 本 · 反做侧对照

| 账面 | 成本权益收益率 | n_closed | 对应反做盘 | 状态 |
|---|---:|---:|---|---|
| tech_mom | −100.0% | 75 | `tech_fade` | 已立，但残值 OUTLIER（+$51.1M，rug 死池价）→ **成果不可判** |
| rel_strength_10d | −100.0% | 28 | `rel_strength_m` | 已立，但残值 OUTLIER（+$30.8M）→ **成果不可判** |
| and_vol_squeeze_break | −100.0% | 18 | 无 | **缺：无 fade 实现（代码层缺口）** |
| cs_mom_1w | −100.0% | 32 | `cs_fade_1w` → 已破线 → `cs_mom_1w_r90` | 已立链，r90 在跑（−7.7%，n=4） |
| vol_spike_entry | −100.0% | 21 | `vol_spike_fade` | 已立（−13.0%，n=7） |
| tech_boll_and | −99.2% | 15 | `tech_boll_fade` | 已立（+117.6%，n=30，未满窗） |
| rs_fade_10d | −100.0% | 3 | 无 | **缺：样本 n=3 过薄** |
| rsi_boll_fade | −99.7% | 2 | 无 | **缺：样本 n=2 过薄** |
| rsi_bounce | −100.0% | 8 | `rsi_bounce_fade` → 已破线 → `rsi_bounce_r90` | 已立链，r90 在跑（−5.8%，n=9） |
| rsi_boll_and | −99.2% | 5 | `rsi_boll_fade` | 已立（该反做盘本身已破线 → 见上条缺口） |
| cs_fade_1w | −94.8% | 5 | `cs_mom_1w_r90` | 已立，在跑 |
| rsi_bounce_fade | −91.6% | 36 | `rsi_bounce_r90` | 已立，在跑 |

**废除侧**：12 本全部无在跑 peer、emitter 全停（`start_multi_paper_race_peers.sh` 已 ABOLISHED 注释留档）。
**反做侧**：真缺 3 本（`and_vol_squeeze_break` / `rs_fade_10d` / `rsi_boll_fade`）；成果可判 5 本；不可判 2 本（OUTLIER 残值）。

## 二、三处缺口的处置与前提

| 账面 | 缺口性质 | 为何现在不开 | 前置条件 |
|---|---|---|---|
| and_vol_squeeze_break | `decision_engine/strategies/` 只有原策略，**无 fade 实现**（`vol_squeeze_fade` 属 `vol_squeeze`，非其反做） | 反做=新写策略，属**新入场族**，一策一盘须另开新盘写绿 + 群互批 | 复盘/决策出具体 fade 策略定义 → 群互批 → 杨点头 |
| rs_fade_10d | 有代码，但样本 n=3 | 立盘即被「n<10 不计正收益臂」挡，开出来是空位 | 等该策略在更大样本上验过，或降格观察 |
| rsi_boll_fade | 有代码，但样本 n=2 | 同上 | 同上 |

## 三、附带更正（对群内审计口径）

反做侧「没对应反做盘」应为 **3 本**，非 5 本：`cs_fade_1w` 与 `rsi_bounce_fade` 的反做盘已于 11-03 落地并在跑
（`paper_eq_cs_mom_1w_r90.db` / `paper_eq_rsi_bounce_r90.db`，launcher 第 113/114 行，
docstring 明写「Re-invert wiped race_cs_fade_1w / race_rsi_bounce_fade」）。

## 四、不动项

零改闸；未 SIGTERM 任何 peer；未开任何新盘；未删库；live/#71 关；真盘不注资。
