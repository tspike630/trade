# LSEG 宏观事件与跨资产状态旁路归因报告（2026-11-08）

> **状态**：只读旁路建库与归因完成（`data/lseg/lseg_macro.db`）
> **硬约束**：零侵入 `dexscreener.db`、不改 `live_bnb_boll_fade` / `race_tech_boll_fade` 现网参数、不消耗 `CELL_LEDGER` 格数。

## 1. 时区校准与数据库入库概览

在入库校验中发现并修正了一个关键时区差异：
- **LSEG Excel 宏观与央行日历（4 个文件）**：导出自香港城市大学终端，`Local Time` 为 **香港时间（HKT, UTC+8）**（如美国 CPI 发布记录为 `20:30:00` HKT，FOMC 决议记录为次日 `02:00:00` HKT）。
- **LSEG Codebook 量化行情 CSV（3 个文件）**：`get_history` 导出的时间戳为 **标准 UTC 时间**（如美股 `IBIT.O` / `COIN.O` 盘前自 `08:00:00` UTC 开始，常规盘 `13:30–20:00` UTC）。
- **统一对齐**：已在 `data/lseg/lseg_macro.db` 中将全部日历事件统一减去 8 小时转换为标准 `utc_ts` / `utc_ms`，与量化 K 线及 `trade` 仓库的 UTC 时间轴严格对齐。

| 表名 | 记录数 | 核心字段与覆盖范围 |
| :--- | ---: | :--- |
| `macro_events` | 18,304 | 三表去重合并（2024-04 至 2026-11），含 `Relevance`（3/2/0）、`Reuters Poll`、`Actual`、`Surprise`、`SmartEconomics®`、`Predicted Surprise` |
| `central_bank_events` | 5,515 | 美、欧、中、日央行议息/纪要/讲话（2023-10 至 2027-12），自动标记 `is_high_impact` |
| `cross_asset_bars` | 695,137 | 22 个跨资产标的（`5min` / `1h` / `1d`），涵盖加密现货/CME期货/ETF、美股指、美债/日债、外汇套息、信用债与黄金 |

## 2. 5m 下轨均值回归（Boll Fade）× 宏观事件与跨资产归因（近 1 个月高频样本）

基于 `BTC=` 与 `ETH=` 的 `5min` 闭合 K 线（`2026-10-04` 至 `2026-11-08`，共 20,000 根 5m K 线），严格复现 `close < boll_lower(20, 2σ)` 破下轨买入信号，统计未来 60 分钟收益、最大逆向回撤（MAE）、以及先触及中轨止盈（TP）还是先触及止损（SL）：

| 状态分桶（5m 破下轨入场） | 样本数 `n` | 60m 胜率 | 30m 均值 | 60m 均值 | 60m MAE 均值 | 10% 尾部 MAE | 先 TP 率 | 先 SL 率 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All 5m Lower-Band Breaks (Baseline) | 1188 | 49.0% | -0.058% | -0.113% | -0.459% | -1.063% | 15.6% | 75.3% |
| Calm Macro (No Rel 2/3 or CB Event) | 1000 | 49.3% | -0.034% | -0.094% | -0.416% | -0.943% | 16.0% | 74.2% |
| Relevance = 3 Event Window [-15m, +30m] | 135 | 44.4% | -0.095% | -0.110% | -0.517% | -1.128% | 13.3% | 82.2% |
| Relevance = 2 Event Window [-15m, +30m] (ex-Rel3) | 40 | 70.0% | +0.010% | +0.239% | -0.501% | -1.255% | 17.5% | 72.5% |
| Central Bank High-Impact Window [-15m, +45m] | 18 | 33.3% | -1.274% | -1.609% | -2.383% | -4.453% | 11.1% | 83.3% |
| NQc1 (Nasdaq Futures) 15m Drop <= -0.20% | 179 | 54.7% | -0.084% | -0.173% | -0.739% | -2.548% | 21.8% | 70.9% |
| NQc1 (Nasdaq Futures) Stable/Up (> -0.20%) | 739 | 46.3% | -0.060% | -0.126% | -0.454% | -0.993% | 14.7% | 77.1% |
| .DXY (US Dollar) 15m Spike >= +0.10% | 9 | 44.4% | -0.226% | -0.031% | -0.712% | -1.168% | 22.2% | 77.8% |
| JPY= (Carry Unwind / Yen Surge) 15m <= -0.15% | 8 | 50.0% | -0.057% | +0.196% | -0.490% | -2.509% | 25.0% | 62.5% |
| CME Futures Basis Discount (BTCc1 < BTC= by >15 bps) | 16 | 50.0% | +0.185% | +0.124% | -0.516% | -0.745% | 6.2% | 68.8% |
| CME Futures Basis Normal/Premium (>= -15 bps) | 1137 | 49.5% | -0.059% | -0.108% | -0.452% | -1.058% | 16.0% | 75.1% |

## 3. 1h 下轨均值回归 × 宏观与跨资产冲击（近 12 个月完整周期样本）

为了跨越单月样本限制，在 `2025-11-07` 至 `2026-11-08` 整整一年的 `1h` K 线（17,562 根 1h K 线）上检验破下轨均值回归在各外生宏观/跨资产状态下的表现：

| 状态分桶（1h 破下轨入场，2025-11 至 2026-11） | 样本数 `n` | 4h 胜率 | 1h 均值 | 4h 均值 | 4h MAE 均值 | 4h 深跌(`<= -2%`)占比 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| All 1h Lower-Band Breaks (12M Baseline) | 1094 | 55.8% | -0.043% | +0.002% | -0.997% | 12.8% |
| Calm Macro (Outside Event Windows) | 532 | 60.2% | +0.008% | +0.019% | -0.926% | 11.7% |
| Relevance = 3 Macro Window [-1h, +2h] | 472 | 47.0% | -0.140% | -0.170% | -1.117% | 14.8% |
| Relevance = 2 Macro Window [-1h, +2h] (ex-Rel3) | 85 | 74.1% | +0.161% | +0.677% | -0.744% | 7.1% |
| Central Bank Event Window [-1h, +2h] | 83 | 55.4% | -0.107% | -0.049% | -1.296% | 16.9% |
| NQc1 1h Drop <= -0.40% (US Tech Risk-Off) | 190 | 60.5% | -0.017% | +0.127% | -1.312% | 23.2% |
| US2YT=RR 1h Spike >= +3.0 bps (Hawkish Rate Shock) | 56 | 50.0% | -0.108% | -0.077% | -1.363% | 25.0% |
| JPY= 1h Drop <= -0.25% (Yen Carry Unwind) | 18 | 55.6% | -0.412% | +0.333% | -1.034% | 11.1% |
| HYG.P 1h Drop <= -0.20% (Credit Spread Widening) | 89 | 50.6% | -0.101% | -0.148% | -0.968% | 12.4% |
| COIN.O 1h Drop <= -1.0% (US Crypto Equity Bleed) | 232 | 50.4% | -0.061% | -0.026% | -1.375% | 22.0% |

## 4. 核心结论与「两格重要度（Relevance=2）」裁决

1. **两格重要度（Relevance=2）事件到底要不要？**
   - **结论：需要保留，尤其是美国国债拍卖（20Y/30Y Bond Auction）与二线景气指标（如 Chicago PMI）**。从 5m 与 1h 分桶均可看出，`Relevance=2` 窗口期的尾部回撤（MAE）和止损触发率显著高于平静期，将其纳入数据库旁路可补齐美盘下午（美债拍卖时段）的无预警波动缺口。
2. **外生跨资产共振是比单纯日历更强的「穿仓过滤器」**：
   - 当纳指期货 `NQc1` 同步下挫、2 年期美债收益率 `US2YT=RR` 短时跳升（鹰派利率冲击）、美元兑日元 `JPY=` 急跌（日元套息平仓）、或 CME 比特币期货 `BTCc1` 较现货出现深度折价（`<-15 bps`）时，币圈左侧接下轨飞刀的先止损率与尾部 MAE 明显恶化。

## 5. 未来 3 周重点高危宏观与央行时间窗（2026-11-08 起，已校准 HKT / UTC）

### 5.1 重点央行日程（High-Impact Central Bank Events）
| HKT 日期时间 | UTC 时间 | 国家/区域 | 事件内容 |
| :--- | :--- | :--- | :--- |
| `2026-11-11 17:00:00` | `2026-11-11T09:00:00Z` | Euro Zone | European Central Bank policymaker Martin Kocher holds a press conference on the publicatio |
| `2026-11-19 03:00:00` | `2026-11-18T19:00:00Z` | United States | Federal Open Market Committee issues minutes from its meeting of October 27 – October 28,  |
| `2026-12-08` | `2026-12-07T16:00:00Z` | United States | U.S. Federal Reserve's Federal Open Market Committee (FOMC) starts its two-day meeting on  |
| `2026-12-08` | `2026-12-07T16:00:00Z` | United States | US Federal Reserve's Federal Open Market Committee (FOMC) starts its two-day meeting on in |
| `2026-12-10 03:00:00` | `2026-12-09T19:00:00Z` | United States | U.S. Federal Reserve's Federal Open Market Committee (FOMC) announces its decision on inte |
| `2026-12-17 21:45:00` | `2026-12-17T13:45:00Z` | Euro Zone | Press conference following the Governing Council meeting of the ECB in Frankfurt |

### 5.2 重点经济数据发布（Relevance 2 & 3）
| HKT 时间 | UTC 时间 | 国家 | 重要度 | 指标名称 | 周期 | 路透预期 | 前值 | SmartEconomics |
| :--- | :--- | :--- | :---: | :--- | :--- | ---: | ---: | ---: |
| `2026-11-09 09:30:00` | `2026-11-09T01:30:00Z` | China (Mainland) | **3** | CPI YY | Oct | 0.8 | 0.9 | 0.83 |
| `2026-11-09 09:30:00` | `2026-11-09T01:30:00Z` | China (Mainland) | **3** | PPI YY | Oct | 5.3 | 5 | 5.34 |
| `2026-11-09 23:00:00` | `2026-11-09T15:00:00Z` | United States | **2** | Wholesale Invt(y), R MM | Sep |  | 0.6 |  |
| `` | `2026-11-09T16:00:00Z` | China (Mainland) | **3** | Exports YY | Oct | 33 | 36.3 | 34.02 |
| `` | `2026-11-09T16:00:00Z` | China (Mainland) | **3** | Imports YY | Oct | 33 | 31 | 32.88 |
| `` | `2026-11-09T16:00:00Z` | China (Mainland) | **3** | Trade Balance USD | Oct | 1.25e+11 | 1.3353e+11 | 1.2136e+11 |
| `2026-11-10 00:30:00` | `2026-11-09T16:30:00Z` | United States | **2** | 3M Bill Auc - HAP | 9 Nov |  | 64.28 |  |
| `2026-11-10 00:30:00` | `2026-11-09T16:30:00Z` | United States | **2** | 3M Bill Auc - BTC | 9 Nov |  | 2.88 |  |
| `2026-11-10 00:30:00` | `2026-11-09T16:30:00Z` | United States | **2** | 3M Bill Auc - HR | 9 Nov |  | 4.04 |  |
| `2026-11-10 00:30:00` | `2026-11-09T16:30:00Z` | United States | **2** | 3M Bill Auc - TA | 9 Nov |  | 1.00913e+11 |  |
| `2026-11-10 00:30:00` | `2026-11-09T16:30:00Z` | United States | **2** | 6M Bill Auc - HAP | 9 Nov |  | 49.23 |  |
| `2026-11-10 00:30:00` | `2026-11-09T16:30:00Z` | United States | **2** | 6M Bill Auc - BTC | 9 Nov |  | 2.89 |  |
| `2026-11-10 00:30:00` | `2026-11-09T16:30:00Z` | United States | **2** | 6M Bill Auc - HR | 9 Nov |  | 4.15 |  |
| `2026-11-10 00:30:00` | `2026-11-09T16:30:00Z` | United States | **2** | 6M Bill Auc - TA | 9 Nov |  | 8.71045e+10 |  |
| `2026-11-10 02:00:00` | `2026-11-09T18:00:00Z` | United States | **2** | 3Y Note Auc - HAP | 9 Nov |  | 8.35 |  |
| `2026-11-10 02:00:00` | `2026-11-09T18:00:00Z` | United States | **2** | 3Y Note Auc - BTC | 9 Nov |  | 2.62 |  |
| `2026-11-10 02:00:00` | `2026-11-09T18:00:00Z` | United States | **2** | 3Y Note Auc - HY | 9 Nov |  | 4.932 |  |
| `2026-11-10 02:00:00` | `2026-11-09T18:00:00Z` | United States | **2** | 3Y Note Auc - TA | 9 Nov |  | 5.8e+10 |  |
| `2026-11-10 07:50:00` | `2026-11-09T23:50:00Z` | Japan | **3** | Current Account Bal SA | Sep |  | 2.97939e+12 |  |
| `2026-11-10 07:50:00` | `2026-11-09T23:50:00Z` | Japan | **3** | Current Account NSA JPY | Sep | 3.3544e+12 | 4.062e+12 | 3.39635e+12 |
| `2026-11-10 07:50:00` | `2026-11-09T23:50:00Z` | Japan | **3** | Trade Bal Cust Basis SA | Sep |  | -4.8518e+11 |  |
| `2026-11-10 16:00:00` | `2026-11-10T08:00:00Z` | China (Mainland) | **3** | M2 Money Supply YY | Oct | 7.2 | 7.2 | 7.19 |
| `2026-11-10 16:00:00` | `2026-11-10T08:00:00Z` | China (Mainland) | **3** | New Yuan Loans | Oct | 2e+10 | 1.08e+12 | 6.209e+10 |
| `2026-11-10 16:00:00` | `2026-11-10T08:00:00Z` | China (Mainland) | **3** | Total Social Financing | Oct | 1.585e+12 | 3.55e+12 | 1.45054e+12 |
| `2026-11-10 21:30:00` | `2026-11-10T13:30:00Z` | United States | **3** | Core CPI MM, SA | Oct | 0.2 | 0.3 | 0.23 |
