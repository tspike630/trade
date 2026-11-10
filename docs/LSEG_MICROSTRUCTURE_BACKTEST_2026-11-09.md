# LSEG 做市商盘口微结构与 SmartEconomics 极值门控历史回测报告（2026-11-09）

> **数据底座**：`data/lseg/lseg_macro.db`（330,603 条跨资产做市商盘口微结构记录 + 5,106 条 SmartEconomics 预测极值记录）
> **验证框架**：4 组递进式策略门控对比 + 4 折 Purged & Embargoed Walk-Forward 样本外滚动验证（`walk_forward.py`）+ 偏度/峰度修正概率夏普率 `PSR` 与多重检验去偏夏普率 `DSR`（`metrics.py`, `K=4`）。

## 1. 单因子归因：LSEG 做市商盘口微结构与 SmartEconomics 预测分歧

### 1.1 高频 5m 破下轨均值回归（2026-10-04 至 2026-11-08，`BTC=` + `ETH=` 共 20,000 根 5m K 线）

| 盘口微结构状态分桶（5m 破下轨） | 样本数 `n` | 60m 胜率 | 60m 均值 | 60m MAE 均值 | 10% 尾部 MAE |
| :--- | ---: | ---: | ---: | ---: | ---: |
| All 5m Lower-Band Breaks (Baseline) | 1188 | 49.0% | -0.113% | -0.459% | -1.063% |
| BTCc1 Dealer Spread Normal (< 8.0 bps) | 958 | 48.6% | -0.128% | -0.483% | -1.068% |
| BTCc1 Dealer Spread Blowout (>= 10.0 bps, p95) | 51 | 43.1% | -0.094% | -0.568% | -1.459% |
| NQc1 Quote Movement Toxicity (quote_imbalance <= -0.04) | 126 | 42.1% | -0.186% | -0.537% | -1.244% |
| BTCc1 Institutional VWAP Breakdown (<= -0.35% below VWAP) | 500 | 47.2% | -0.175% | -0.618% | -1.553% |
| IBIT.O ETF Dealer Spread Widening (>= 6.0 bps) | 124 | 47.6% | -0.079% | -0.512% | -1.134% |
| JPY= FX Carry Dealer Spread Widening (>= 2.5 bps) | 349 | 44.4% | -0.135% | -0.511% | -1.101% |

### 1.2 全年 1h 破下轨均值回归（2025-11-07 至 2026-11-08，`BTC=` + `ETH=` 共 17,562 根 1h K 线）

| 宏观极值与盘口微结构分桶（1h 破下轨，12 个月样本） | 样本数 `n` | 4h 胜率 | 1h 均值 | 4h 均值 | 4h MAE 均值 | 4h 深跌(`<= -2%`)占比 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| All 1h Lower-Band Breaks (12M Baseline) | 1094 | 55.8% | -0.043% | +0.002% | -0.997% | 12.8% |
| Calm Macro (Outside Rel 2/3 & CB Windows) | 622 | 62.4% | +0.030% | +0.133% | -0.905% | 11.3% |
| Rel=3 Out-of-Bounds Shock (Actual > Max or < Min) | 335 | 51.6% | -0.116% | -0.072% | -1.070% | 16.1% |
| Rel=3 High SmartEconomics Predicted Surprise (>= 15%) | 118 | 50.0% | -0.218% | -0.043% | -1.201% | 20.3% |
| BTCc1 1h Dealer Spread Blowout (>= 12.0 bps) | 29 | 37.9% | +0.013% | -0.117% | -0.741% | 6.9% |
| NQc1 1h Quote Movement Toxicity (quote_imbalance <= -0.02) | 111 | 48.6% | +0.030% | -0.100% | -0.930% | 11.7% |
| ESc1 1h Quote Movement Toxicity (quote_imbalance <= -0.02) | 113 | 49.6% | -0.023% | -0.075% | -0.809% | 8.8% |
| BTCc1 1h Institutional VWAP Breakdown (<= -0.80%) | 588 | 55.3% | -0.057% | +0.016% | -1.154% | 16.7% |
| JPY= 1h Dealer Spread Widening (>= 3.0 bps) | 89 | 47.2% | -0.085% | -0.064% | -1.068% | 13.5% |

## 2. 端到端策略生命周期回测对比（4 组递进式门控）

### 2.1 5m 实盘入场区策略（`live_bnb_entry_zone` 坐标：`alpha ∈ [0.08, 0.40]`, `odds >= 1.5`）

*注：初始资金 `$10,000`，单笔固定名义仓位 `$1,000`（即 10% 仓位/笔），止盈锚定 `boll_mid`，止损锚定 `low - 0.10 * span`，最长持仓 60m 时间止损，多重检验次数 `K = 4`。*

| 策略实验组（5m Entry Zone） | 成交笔数 | 胜率 | 盈亏比 `Payoff` | 获利因子 `PF` | 累计收益 | 最大回撤 `MDD` | 单笔 `Sharpe` | `PSR` | `DSR (K=4)` |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Arm 0: Baseline Entry Zone (No LSEG Gate) | 246 | 35.8% | 1.78 | 0.99 | -0.03% | 0.97% | -0.003 | 48.2% | 13.7% |
| Arm 1: + Static Macro Calendar Blackout | 202 | 36.1% | 2.07 | 1.17 | +0.49% | 0.37% | +0.063 | 82.2% | 43.5% |
| Arm 2: + SmartEconomics Dynamic Bounds (OOB 90m / In-Line 5m Fast-Clear) | 188 | 37.2% | 2.11 | 1.25 | +0.65% | 0.38% | +0.090 | 90.4% | 57.8% |
| Arm 3: + LSEG Dealer Microstructure Gate (BTCc1/NQc1/HYG.P) | 159 | 39.0% | 2.14 | 1.37 | +0.78% | 0.37% | +0.126 | 95.5% | 71.6% |

### 2.2 1h 全年周期下轨均值回归（2025-11-07 至 2026-11-08，12 个月完整周期）

*注：初始资金 `$10,000`，单笔固定名义仓位 `$1,000`，最长持仓 4h 时间止损，多重检验次数 `K = 4`。*

| 策略实验组（1h Full-Year Boll Fade） | 成交笔数 | 胜率 | 盈亏比 `Payoff` | 获利因子 `PF` | 累计收益 | 最大回撤 `MDD` | 单笔 `Sharpe` | `PSR` | `DSR (K=4)` |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Arm 0: Baseline 1h Lower-Band Break (No Gate) | 593 | 52.1% | 0.87 | 0.95 | -1.27% | 3.93% | -0.020 | 31.8% | 6.5% |
| Arm 1: + Static Macro Calendar Blackout | 333 | 56.5% | 0.75 | 0.98 | -0.28% | 2.60% | -0.008 | 43.9% | 11.5% |
| Arm 2: + Cross-Asset Price Contagion (NQc1/US2Y/JPY/HYG) | 283 | 57.6% | 0.75 | 1.02 | +0.16% | 2.03% | +0.006 | 54.0% | 17.1% |
| Arm 3: + LSEG Dealer Microstructure & SmartEconomics Bounds v2 | 162 | 64.2% | 0.85 | 1.52 | +2.06% | 1.06% | +0.161 | 97.7% | 83.4% |

## 3. 机构级 4 折 Purged Walk-Forward 样本外滚动验证（`walk_forward.py`）

### 3.1 5m Entry Zone（4 折滚动，Embargo 隔离带 = `3600s`）

| 策略实验组 | OOS 样本笔数 | 样本内均值 `IS SR` | 样本外均值 `OOS SR` | 汇总 `Pooled OOS SR` | `WFE` 效率比 | `OOS PSR` | `OOS DSR` | 过拟合判定 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| Arm 0: Baseline Entry Zone (No LSEG Gate) | 202 | +0.0308 | +0.0443 | +0.0243 | 1.44 | 63.6% | 23.8% | FAIL (deflated_sharpe_below_confidence) |
| Arm 1: + Static Macro Calendar Blackout | 166 | +0.0318 | +0.0926 | +0.0916 | 2.91 | 89.2% | 55.3% | **PASS** |
| Arm 2: + SmartEconomics Dynamic Bounds (OOB 90m / In-Line 5m Fast-Clear) | 153 | +0.0520 | +0.1250 | +0.1230 | 2.40 | 94.8% | 69.2% | **PASS** |
| Arm 3: + LSEG Dealer Microstructure Gate (BTCc1/NQc1/HYG.P) | 135 | +0.1268 | +0.1239 | +0.1278 | 0.98 | 94.4% | 67.8% | **PASS** |

### 3.2 1h 全年周期（4 折滚动，Embargo 隔离带 = `14400s`）

| 策略实验组 | OOS 样本笔数 | 样本内均值 `IS SR` | 样本外均值 `OOS SR` | 汇总 `Pooled OOS SR` | `WFE` 效率比 | `OOS PSR` | `OOS DSR` | 过拟合判定 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| Arm 0: Baseline 1h Lower-Band Break (No Gate) | 414 | -0.0345 | +0.0364 | +0.0355 | — | 76.6% | 37.0% | FAIL (deflated_sharpe_below_confidence) |
| Arm 1: + Static Macro Calendar Blackout | 226 | -0.0828 | +0.0784 | +0.0679 | — | 84.9% | 48.8% | FAIL (deflated_sharpe_below_confidence) |
| Arm 2: + Cross-Asset Price Contagion (NQc1/US2Y/JPY/HYG) | 190 | -0.0907 | +0.1102 | +0.0952 | — | 90.8% | 60.4% | **PASS** |
| Arm 3: + LSEG Dealer Microstructure & SmartEconomics Bounds v2 | 106 | +0.0726 | +0.1953 | +0.2582 | 2.69 | 99.6% | 94.6% | **PASS** |

## 4. 关键实证结论与实盘参数建议

1. **SmartEconomics 预期差极值（`Min` / `Max` / `Predicted Surprise`）显著优于固定日历静默窗**：
   - 在 5m 实盘入场区策略中，启用 `DECISION_MACRO_SURPRISE_DYNAMIC=1`（对落入 `[Min, Max]` 区间且贴近共识的温和发布在 `+5m` 后提前解封、对突破全市场极值的 `Out-of-Bounds` 冲击延长封锁至 `+90m`）使获利因子 `PF` 从静态日历的 `1.17` 提升至 `1.25`，`DSR` 从 `43.5%` 跃升至 `57.8%`（越过 50% 置信门槛）。
2. **做市商盘口微结构（`spread_bps` / `quote_imbalance` / `VWAP`）精准识别隐性流动性抽干**：
   - 当 CME 比特币期货 `BTCc1` 盘口价差走阔（5m `>= 10~20 bps`、1h `>= 12 bps`）、纳指/标普期货做市商频繁下撤买单（`NQc1` / `ESc1` `quote_imbalance < 0`）、或高收益信用债 ETF `HYG.P` 盘口价差异常放大（`>= 35 bps`）时，加密市场下轨均值回归胜率暴跌（1h `BTCc1 spread >= 12 bps` 时胜率仅 `37.9%`）。
   - 叠加 `DECISION_MACRO_MICROSTRUCTURE_GATE=1` 后，5m 入场区策略 `PF` 进一步升至 **`1.37`**，最大回撤从基线的 `0.97%` 压降至 **`0.37%`**（降幅 **62%**），4 折样本外 `WFE = 0.98`、`Pooled OOS PSR = 94.4%`；1h 全年策略胜率从 `52.1%` 提升至 **`64.2%`**，`PF` 从 `0.95` 跃升至 **`1.52`**，最大回撤从 `3.93%` 降至 **`1.06%`**（降幅 **73%**），样本外 `Pooled OOS PSR = 99.6%`、`DSR = 94.6%`。
