# live_bnb Entry Zone（2026-11-05）

杨令 17:36 HKT：改 `live_bnb_boll_fade` 这一处后改进实盘，确认解除冻结。本变更只改这一条 live 臂的入场和止损/止盈意图。纸面 `race_tech_boll_fade` 仍是跌 2、量能 0.5、破下轨。不下单。`LIVE_TRADING_DISABLED` 与 `BINANCE_SPOT_LIVE_ENABLED` 不动。真盘仍关。

#164 开着，没进 main。它有当前 5m 的 `avg_trade_size`，没有 `trade_size_ratio`。决策层在行上已有合法 `trade_size_ratio` 时直接用；否则用 `avg_trade_size / 过去24h均额`；再否则用已收盘 K 线的 `quote_volume / trades`。缺拥挤度或 L/M/P 就拒绝，不退回旧的 -2% 破下轨。

`trade_size_ratio` 是单笔成交额的大小比，不是庄家或散户身份。日志档叫 `large_trade` / `neutral` / `retail_crowded`。

## 坐标

同一根已收盘 5m：`L` = low，`M` = boll_mid，`P` = close，`S = M - L`，`alpha = (P - L) / S`。

只在 `S > 0` 且 alpha 有限时继续。`P >= M` 记 `price_above_mid`。`P` 低于该档地板记 `price_below_floor`（大单地板是 alpha 0.08）。止损 `>=` 入场价记 `stop_not_below_price`，赔率不算。比较带 1e-9 的浮点容差，不把区间放宽。

止损参考 `L - 0.1*S`。止盈是决策当时的 `M`（绝对价）。没有另扣手续费，也没有把 1.5 / 2.5 改掉。执行 adapter 收下这个绝对价；之后 boll_mid 变了，不改这笔止盈。现货 OCO / STOP_LOSS_LIMIT 没接，dry-run 写 `protective_order=not_supported_not_sent`，不把没发出的保护单说成已挂。

旧的跌 2、量能、破下轨写在 `legacy_*`，`legacy_gate=diagnostic_only`。

## 三档

| 档 | ratio | 入场 | 仓位 |
|---|---|---|---|
| `large_trade` | `>= 1.3` | alpha ∈ [0.08, 0.40] 且 `(M-P)/(P-stop) >= 1.5` 才市价。0.40 在区间内，但赔率约 1.2，会被 `odds_below_min` 拒掉 | 现有单笔计划仓位，乘数 1。不改名义外壳 |
| `neutral` | `0.85 <= ratio < 1.3` | alpha ∈ [0.12, 0.28] 且赔率 `>= 2.5` 才市价。价弹太高则只在 `L+0.20*S` 挂限价，且限价必须低于现价、限价自己的赔率也要 `>= 2.5`。接不到不追。alpha 低于 0.12 不挂更高的价 | 现有风险系统没有「按止损宽度反比」。本档用半个计划单笔仓位，`position_size_multiplier=0.5`。其它 purpose 不动 |
| `retail_crowded` | `< 0.85` | `retail_crowded_skip`。第一锤不市价。本轮不挂 `L-0.5*S`。`deep_zone_price` 只记审计 | 无单 |

过去 24h 的分母不含当前这根。当前这根收盘时刻往前正好 24h 的那根算在分母里。更早的不算。当前或分母的笔数不是正数 → `trade_size_ratio_missing`。

## 还要本机做的

单测不代表已经实盘。杀闸仍关。复盘③和本机 dry-run 还要在能读到 K 线库的机器上再跑一遍，确认 extras 里的绝对止损/止盈进了 dry-run 回执，且没有 `newOrder`。
